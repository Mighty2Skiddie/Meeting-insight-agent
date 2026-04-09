"""
Resilience primitives — circuit breaker, retry, transient error tagging.

We own this code instead of using aiobreaker because the library's API
broke between minor versions. A circuit breaker is simple enough to
implement correctly and critical enough to fully control.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.config import get_settings
from src.observability.logging import get_logger
from src.observability.metrics import circuit_breaker_state, fallback_activations_total

log = get_logger(__name__)
settings = get_settings()


# ── Circuit Breaker State ─────────────────────────────────────────────────────

class CBState(str, Enum):
    CLOSED = "closed"        # Normal operation — requests pass through
    OPEN = "open"            # Tripped — requests fail immediately
    HALF_OPEN = "half_open"  # Testing — one probe request allowed


@dataclass
class CircuitBreaker:
    """Async circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED."""
    name: str
    fail_max: int = 3
    reset_timeout_s: float = 120.0

    _state: CBState = field(default=CBState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @property
    def current_state(self) -> str:
        if self._state == CBState.OPEN:
            if time.monotonic() - self._opened_at >= self.reset_timeout_s:
                # Timeout has passed — allow a probe
                return CBState.HALF_OPEN
        return self._state

    def _trip(self) -> None:
        self._state = CBState.OPEN
        self._opened_at = time.monotonic()
        log.warning("circuit_breaker_opened", name=self.name, failures=self._failure_count)

    def _reset(self) -> None:
        self._state = CBState.CLOSED
        self._failure_count = 0
        log.info("circuit_breaker_closed", name=self.name)

    async def call(
        self,
        fn: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        async with self._lock:
            state = self.current_state

            if state == CBState.OPEN:
                raise CircuitOpenError(f"Circuit '{self.name}' is OPEN — request rejected")

            # CLOSED or HALF_OPEN: attempt the call
            try:
                result = await fn(*args, **kwargs)
                # Success — reset the breaker
                if state == CBState.HALF_OPEN:
                    self._reset()
                else:
                    self._failure_count = 0
                return result

            except Exception as exc:
                self._failure_count += 1
                if self._failure_count >= self.fail_max or state == CBState.HALF_OPEN:
                    self._trip()
                log.warning(
                    "circuit_breaker_failure",
                    name=self.name,
                    failure_count=self._failure_count,
                    error=str(exc),
                )
                raise


class CircuitOpenError(Exception):
    """Raised when a circuit breaker blocks a request."""


# One breaker per external service────

breakers: dict[str, CircuitBreaker] = {
    "openai": CircuitBreaker(
        name="openai",
        fail_max=settings.circuit_breaker_fail_max,
        reset_timeout_s=float(settings.circuit_breaker_reset_timeout),
    ),
    "groq": CircuitBreaker(
        name="groq",
        fail_max=settings.circuit_breaker_fail_max,
        reset_timeout_s=float(settings.circuit_breaker_reset_timeout),
    ),
    "gemini": CircuitBreaker(
        name="gemini",
        fail_max=settings.circuit_breaker_fail_max,
        reset_timeout_s=float(settings.circuit_breaker_reset_timeout),
    ),
}


def update_breaker_metrics() -> None:
    """Push current circuit breaker states to Prometheus gauges."""
    state_map = {CBState.CLOSED: 0, CBState.HALF_OPEN: 1, CBState.OPEN: 2}
    for name, breaker in breakers.items():
        state_val = state_map.get(CBState(breaker.current_state), 0)
        circuit_breaker_state.labels(service=name).set(state_val)


# ── Retry Helper ───────────────────────────────────────────────────────────────

class TransientError(Exception):
    """Tag an exception as safe to retry (rate limits, timeouts, 5xx)."""


async def call_with_retry(
    fn: Callable[..., Coroutine[Any, Any, Any]],
    *args: Any,
    service_name: str = "unknown",
    **kwargs: Any,
) -> Any:
    """
    Wraps an async callable with:
      1. Circuit breaker gate (outermost — fail fast when service is down)
      2. Exponential-backoff retry with jitter (inner — handles transient blips)
    """
    breaker = breakers.get(service_name)
    update_breaker_metrics()

    async def _attempt() -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(settings.retry_max_attempts),
            wait=wait_exponential_jitter(initial=1, max=8),
            retry=retry_if_exception_type((TransientError, asyncio.TimeoutError)),
            reraise=True,
        ):
            with attempt:
                if breaker:
                    return await breaker.call(fn, *args, **kwargs)
                return await fn(*args, **kwargs)
        return None  # unreachable — keeps mypy happy

    try:
        return await _attempt()
    except CircuitOpenError:
        log.warning("circuit_open_rejected", service=service_name)
        update_breaker_metrics()
        fallback_activations_total.labels(service=service_name, tier="circuit_open").inc()
        raise
    except RetryError:
        log.error("retries_exhausted", service=service_name)
        fallback_activations_total.labels(service=service_name, tier="retry_exhausted").inc()
        raise
