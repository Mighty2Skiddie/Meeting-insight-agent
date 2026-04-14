"""
Playwright-based browser bot for Google Meet caption scraping.

Uses Playwright SYNCHRONOUS API in a dedicated daemon thread.

CRITICAL DESIGN RULE:
    Every method that touches self._page, self._browser, or any Playwright
    object MUST run inside the browser thread (_sync_browser_thread).
    The async public API communicates with the thread ONLY through:
      - threading.Event  (join/admit/stop signals)
      - self._callback   (set once before thread starts polling)
      - self._active     (bool flag, written by thread, read by async)
    Calling any Playwright method from a different thread causes:
        greenlet.error: Cannot switch to a different thread
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
import traceback as tb_module
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.observability.logging import get_logger

log = get_logger(__name__)

# ── Selector groups ───────────────────────────────────────────────────────

# Caption container selectors — ordered by reliability.
# Google updates class names frequently; multiple fallbacks prevent breakage.
CAPTION_SELECTORS = [
    ".VbkSUe",                     # Caption text only — confirmed 2026-04 diagnostic
    "[jsname='dsyhDe']",          # Primary (2025-2026) — includes speaker name
    ".iOzk7",                     # Subtitle container — includes speaker name
    ".a4cQT",                     # Alternative class (captions settings bar)
    "[data-message-text]",        # Data attribute fallback
    ".CNuzmb",                    # Known fallback
    "[jsname='YSg7wd']",         # 2024 variant
    "[aria-live='polite']",       # Accessibility fallback (broad)
]

CAPTION_BUTTON_SELECTORS = [
    "button[jsname='RrG0hf']",        # Confirmed 2026-04 diagnostic
    "[aria-label*='Turn on captions' i]",
    "[aria-label*='captions' i]",
    "[data-tooltip*='captions' i]",
    "[aria-label*='subtitle' i]",
    "[data-tooltip*='subtitle' i]",
    "[aria-label*='closed caption' i]",
    "button[jsname='r8qRAd']",
    "button[jsname='Ax5TH']",
]

CONTINUE_AS_GUEST_SELECTORS = [
    "a:has-text('continue without signing in')",
    "button:has-text('Continue without')",
    "a:has-text('without signing in')",
    "[jsname='tJHJj']",
    "[data-action='continue-as-guest']",
    "button:has-text('Use without an account')",
    "a:has-text('Join from a browser')",
    "button:has-text('Continue as guest')",
]

JOIN_BUTTON_SELECTORS = [
    "button[jsname='Qx7uuf']",        # "Ask to join"
    "button[jsname='Nebqdb']",        # "Join now"
    "[aria-label*='Ask to join' i]",
    "[aria-label*='Join now' i]",
    "button:has-text('Ask to join')",
    "button:has-text('Join now')",
    "button:has-text('Join')",
]

NAME_INPUT_SELECTORS = [
    "input[jsname='YPqjbf']",
    "input[aria-label*='name' i]",
    "input[placeholder*='name' i]",
    "input[autocomplete='name']",
    "input[type='text']:visible",
    "input[type='text']",
]

IN_MEETING_SELECTORS = [
    "button[jsname='CQylAd']",        # Leave call button (confirmed diagnostic)
    "div[data-meeting-title]",        # Meeting title container (confirmed)
    ".crqnQb",                        # Meeting UI container (confirmed)
    "[aria-label*='Leave call' i]",
    "[data-tooltip*='Leave call' i]",
    "[aria-label*='Turn off microphone' i]",
    "[aria-label*='Microphone' i]",
    "[data-call-ended='false']",
    "[jsname='AdzeRb']",
]

MEETING_ENDED_TEXTS = [
    "The call has ended",
    "You've left the call",
    "You left the meeting",
    "Return to home screen",
]

# Texts shown when Google Meet blocks the join attempt entirely
MEETING_REJECTED_TEXTS = [
    "You can't join this video call",
    "This meeting has ended",
    "Meeting not found",
    "Check the meeting code",
    "This video call has ended",
    "No one can join a meeting unless",
    "You are not allowed to join",
    "Invalid meeting code",
]

# ── Project root for debug screenshots ───────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent


@dataclass
class CaptionEvent:
    speaker: str
    text: str
    timestamp: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


CaptionCallback = Callable[[CaptionEvent], Awaitable[None]]


class MeetingBot:
    """
    Headless Chromium bot that joins Google Meet and scrapes live captions.

    Architecture (single-thread ownership):

        Async caller (uvicorn loop)        Browser thread
        ──────────────────────────         ────────────────────────────
        join_meeting()                    _sync_browser_thread():
          → starts thread                   ├─ launch Chromium
          → waits _joined event             ├─ navigate to Meet URL
                                            ├─ handle sign-in redirect
                                            ├─ enter name, click join
                                            ├─ _joined.set()
        wait_for_admission()                ├─ detect in-meeting UI
          → waits _admitted event           ├─ _admitted.set()
                                            ├─ enable captions (retries)
        enable_captions() → no-op           │
        start_caption_capture(cb)           │  (callback registered)
          → stores callback                 │
                                            ├─ POLL LOOP (every 500ms):
        is_meeting_active()                 │   ├─ page.evaluate(caption JS)
          → reads _active flag only         │   ├─ run_coroutine_threadsafe(cb)
                                            │   ├─ check meeting-ended text
                                            │   └─ set _active=False if ended
        leave_meeting()                     │
          → _stop.set()                     ├─ click hang-up button
          → thread.join()                   └─ browser.close()
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._joined = threading.Event()
        self._admitted = threading.Event()
        self._callback_ready = threading.Event()  # Signals callback is registered
        self._error: Exception | None = None
        self._callback: CaptionCallback | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._active = False
        # Playwright objects — ONLY touched by browser thread
        self._page = None
        self._browser = None

    # ── Public async API ──────────────────────────────────────────────────
    # NONE of these methods touch Playwright objects directly.

    async def join_meeting(self, meeting_url: str, bot_name: str = "Transcriber") -> None:
        """Start the browser thread and wait for the join button to be clicked."""
        if not re.match(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", meeting_url):
            raise ValueError(
                f"Invalid Google Meet URL: {meeting_url!r}. "
                "Expected format: https://meet.google.com/xxx-xxxx-xxx"
            )

        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Playwright not installed. Run:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            ) from exc

        self._async_loop = asyncio.get_running_loop()
        self._active = True

        self._thread = threading.Thread(
            target=self._sync_browser_thread,
            args=(meeting_url, bot_name),
            daemon=True,
            name="PlaywrightBotThread",
        )
        self._thread.start()
        log.info("bot_thread_started", url=meeting_url, bot_name=bot_name)

        joined = await asyncio.get_running_loop().run_in_executor(
            None, self._joined.wait, 45.0
        )

        if self._error:
            raise self._error
        if not joined:
            raise RuntimeError("Browser failed to reach the Google Meet join screen within 45s.")

        log.info("bot_join_requested", url=meeting_url)

    async def wait_for_admission(self, timeout_seconds: float = 120.0) -> bool:
        """Wait for the browser thread to signal admission."""
        admitted = await asyncio.get_running_loop().run_in_executor(
            None, self._admitted.wait, timeout_seconds
        )
        # Check if thread set an error before signaling admission
        if self._error:
            raise self._error
        if admitted:
            log.info("bot_admitted_to_meeting")
        else:
            log.warning("bot_admission_timed_out", timeout_seconds=timeout_seconds)
        return admitted

    async def enable_captions(self) -> bool:
        """No-op — captions are enabled by the browser thread internally.

        This method exists to preserve the session_manager interface.
        The browser thread calls _sync_enable_captions() from within
        its own greenlet context, avoiding cross-thread greenlet crashes.
        """
        return True

    async def start_caption_capture(self, callback: CaptionCallback) -> None:
        """Register the callback. Actual polling runs in the browser thread.

        IMPORTANT: The browser thread WAITS for this method to be called
        before starting the caption polling loop. This prevents the race
        condition where captions are captured but silently discarded
        because the callback hasn't been registered yet.
        """
        self._callback = callback
        self._callback_ready.set()  # Unblock the browser thread's polling loop
        log.info("caption_callback_registered_and_signaled")

    async def is_meeting_active(self) -> bool:
        """Check flags only — NO Playwright calls (prevents greenlet crash)."""
        if not self._active:
            return False
        if self._thread and not self._thread.is_alive():
            self._active = False
            return False
        return True

    async def leave_meeting(self) -> None:
        """Signal the thread to stop. Thread handles Playwright cleanup."""
        self._active = False
        self._stop.set()
        if self._thread and self._thread.is_alive():
            await asyncio.get_running_loop().run_in_executor(
                None, self._thread.join, 10.0
            )
        log.info("bot_left_meeting")

    # ── Browser thread (ONLY thing that touches Playwright) ───────────────

    def _sync_browser_thread(self, url: str, bot_name: str) -> None:
        """
        Full Playwright lifecycle in a dedicated daemon thread.
        This is the ONLY code that interacts with Playwright objects.
        """
        # Force ProactorEventLoop on Windows before Playwright creates its loop
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        page = None  # local ref for safety

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                # ── EXACT same config as debug_captions.py (which WORKS) ──
                # Use system Edge (v141+). Google blocks bundled Chromium.
                # headless=False opens a real browser window — this is
                # REQUIRED because Google Meet doesn't render captions
                # properly in any headless mode.
                browser = pw.chromium.launch(
                    channel="msedge",
                    headless=False,
                    args=[
                        "--no-sandbox",
                        "--use-fake-ui-for-media-stream",
                        "--use-fake-device-for-media-stream",
                        "--disable-blink-features=AutomationControlled",
                        "--mute-audio",  # Silence the browser — no beep sounds
                    ],
                )
                self._browser = browser

                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
                    ),
                    permissions=["camera", "microphone"],
                    locale="en-US",
                    no_viewport=True,
                )
                # Minimal anti-detection — exactly what debug_captions.py uses
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                """)
                page = context.new_page()
                self._page = page

                # ── Navigate ──────────────────────────────────────────
                log.info("bot_navigating", url=url)
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(5)
                self._screenshot(page, "01_initial")

                # ── Dismiss popups / banners that block the join button ──
                self._dismiss_overlays(page)

                # ── Handle sign-in redirect ───────────────────────────
                if "accounts.google.com" in page.url:
                    log.info("google_signin_redirect", current_url=page.url)
                    self._screenshot(page, "02_signin_redirect")
                    clicked = False
                    for sel in CONTINUE_AS_GUEST_SELECTORS:
                        try:
                            if page.locator(sel).count() > 0:
                                page.locator(sel).first.click()
                                log.info("clicked_continue_as_guest", selector=sel)
                                time.sleep(4)
                                clicked = True
                                break
                        except Exception:
                            continue
                    if not clicked:
                        self._error = RuntimeError(
                            "Google requires sign-in. No 'continue as guest' link found. "
                            "The meeting may require a Google account to join."
                        )
                        self._joined.set()
                        self._admitted.set()
                        return
                    self._screenshot(page, "03_after_guest")
                    time.sleep(2)
                    self._dismiss_overlays(page)

                log.info("meet_page_loaded", url=page.url, title=page.title())

                # ── Check if Google rejected the join immediately ─────
                rejection = self._check_rejected(page)
                if rejection:
                    # self._screenshot(page, "02_rejected")
                    self._error = RuntimeError(
                        f"Google Meet rejected the join: '{rejection}'. "
                        "Make sure the meeting is active, the host is present, "
                        "and the meeting allows guests. Create a new meeting "
                        "and use the fresh URL."
                    )
                    self._joined.set()
                    self._admitted.set()
                    return

                # self._dump_page_state(page, "pre-join")

                # ── Enter name ────────────────────────────────────────
                name_entered = False
                for sel in NAME_INPUT_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            el.fill(bot_name)
                            log.info("bot_name_entered", selector=sel)
                            name_entered = True
                            break
                    except Exception:
                        continue
                if not name_entered:
                    log.warning("name_input_not_found")
                # self._screenshot(page, "04_name_filled")

                time.sleep(1)

                # ── Mute microphone & camera before joining ─────────────
                # The fake media stream generates a beep tone. We must
                # turn off mic and camera on the pre-join screen so the
                # bot enters silently without broadcasting noise.
                for mute_sel, label in [
                    ("[aria-label*='Turn off microphone' i]", "microphone"),
                    ("[aria-label*='Turn off camera' i]", "camera"),
                ]:
                    try:
                        el = page.locator(mute_sel).first
                        if el.count() > 0:
                            el.click()
                            log.info(f"pre_join_{label}_muted", selector=mute_sel)
                            time.sleep(0.5)
                    except Exception:
                        pass

                # self._screenshot(page, "04b_muted")

                # ── Click join button ─────────────────────────────────
                join_clicked = False
                for sel in JOIN_BUTTON_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            el.click()
                            log.info("bot_join_clicked", selector=sel)
                            join_clicked = True
                            break
                    except Exception:
                        continue
                if not join_clicked:
                    log.warning("join_button_not_found", url=page.url)
                # self._screenshot(page, "05_after_join")

                # Give the meeting UI 3 seconds to render after join click
                time.sleep(3)
                self._dismiss_overlays(page)
                # self._screenshot(page, "06_meeting_ui")
                # self._dump_page_state(page, "after-join")

                # Unblock join_meeting()
                self._joined.set()

                # ── Wait for admission ────────────────────────────────
                log.info("bot_waiting_for_admission")
                deadline = time.monotonic() + 130.0
                admitted = False
                while time.monotonic() < deadline and not self._stop.is_set():
                    # Check for rejection (meeting ended, kicked out, etc.)
                    rejection = self._check_rejected(page)
                    if rejection:
                        # self._screenshot(page, "06_rejected_during_wait")
                        self._error = RuntimeError(
                            f"Google Meet rejected the join: '{rejection}'. "
                            "The meeting may have ended or the host denied access."
                        )
                        self._admitted.set()
                        return

                    for sel in IN_MEETING_SELECTORS:
                        try:
                            if page.locator(sel).count() > 0:
                                admitted = True
                                log.info("bot_in_meeting", selector=sel)
                                break
                        except Exception:
                            pass
                    if admitted:
                        break
                    time.sleep(2)

                if not admitted:
                    # self._screenshot(page, "06_admission_failed")
                    # self._dump_page_state(page, "admission-failed")
                    self._error = RuntimeError(
                        "Bot was not admitted within 2 minutes. "
                        "The host must click Admit in Google Meet."
                    )
                    self._admitted.set()
                    return

                self._admitted.set()
                # self._screenshot(page, "07_inside_meeting")

                # ── Enable captions (with retries) ────────────────────
                time.sleep(2)  # Let meeting UI fully render
                captions_on = False
                for attempt in range(5):
                    log.info("caption_enable_attempt", attempt=attempt + 1)
                    captions_on = self._try_enable_captions(page)
                    if captions_on:
                        log.info("captions_confirmed_on", attempt=attempt + 1)
                        break
                    log.warning("caption_enable_retry", attempt=attempt + 1)
                    time.sleep(2)

                # Double-verify: also try keyboard shortcut as backup
                if not captions_on:
                    try:
                        page.keyboard.press("c")
                        time.sleep(1)
                        log.info("captions_keyboard_backup_sent")
                    except Exception:
                        pass

                # self._screenshot(page, "08_captions_state")

                # ── Caption polling loop ──────────────────────────────
                # CRITICAL: Wait for session_manager to register the callback
                # before we start polling. Without this, captured captions
                # are silently discarded (self._callback is None).
                log.info("caption_polling_waiting_for_callback")
                callback_ok = self._callback_ready.wait(timeout=30.0)
                if not callback_ok:
                    log.warning("caption_callback_never_registered_proceeding_anyway")
                log.info("caption_polling_started", callback_registered=callback_ok)
                last_text = ""
                poll_count = 0
                error_count = 0
                MAX_LOGGED_ERRORS = 10

                while not self._stop.is_set():
                    poll_count += 1

                    # Check meeting ended (same thread — safe)
                    try:
                        for text in MEETING_ENDED_TEXTS:
                            if page.locator(f"text={text}").count() > 0:
                                log.info("meeting_ended_detected", text=text)
                                self._active = False
                                self._stop.set()
                                break
                    except Exception:
                        pass

                    if self._stop.is_set():
                        break

                    # Extract captions
                    try:
                        current_text = page.evaluate(self._caption_js())

                        # Periodic debug dump (every 60 polls = ~30 seconds)
                        if poll_count % 60 == 0:
                            log.info(
                                "caption_poll_heartbeat",
                                poll_count=poll_count,
                                last_text_len=len(last_text),
                                current_text_preview=current_text[:80] if current_text else "(empty)",
                            )

                        if (
                            current_text
                            and current_text != last_text
                            and len(current_text.strip()) > 2
                        ):
                            event = CaptionEvent(
                                speaker="Speaker",
                                text=current_text.strip(),
                                timestamp=datetime.now(timezone.utc).timestamp(),
                            )
                            log.debug("caption_captured", text=current_text.strip()[:60])
                            if self._callback and self._async_loop:
                                asyncio.run_coroutine_threadsafe(
                                    self._callback(event),
                                    self._async_loop,
                                )
                                # Only update last_text AFTER callback fires
                                # This prevents the dedup bug where text is
                                # captured before callback exists, stored in
                                # last_text, and then never re-sent.
                                last_text = current_text
                            else:
                                # Callback not ready — DON'T update last_text
                                # so the text will be retried next poll
                                log.warning(
                                    "caption_captured_but_no_callback",
                                    text=current_text.strip()[:40],
                                )

                    except Exception as exc:
                        error_count += 1
                        if error_count <= MAX_LOGGED_ERRORS:
                            log.warning(
                                "caption_poll_error",
                                error=repr(exc),
                                poll_count=poll_count,
                                errors_so_far=error_count,
                            )

                    time.sleep(0.5)

                # ── Cleanup ───────────────────────────────────────────
                self._active = False
                log.info(
                    "caption_polling_stopped",
                    total_polls=poll_count,
                    total_errors=error_count,
                )
                self._sync_leave(page)
                browser.close()

        except Exception as exc:
            full_tb = tb_module.format_exc()
            log.error("browser_thread_fatal", error=repr(exc), traceback=full_tb)
            self._error = exc
            self._active = False
            self._joined.set()
            self._admitted.set()

    # ── Thread-internal helpers (ONLY called from browser thread) ─────────

    @staticmethod
    def _try_enable_captions(page) -> bool:
        """Try to enable captions — click CC button or press 'c' key."""
        for sel in CAPTION_BUTTON_SELECTORS:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    el.click()
                    time.sleep(1)
                    log.info("captions_enabled_via_button", selector=sel)
                    return True
            except Exception:
                continue

        # Keyboard shortcut fallback
        try:
            page.keyboard.press("c")
            time.sleep(1)
            log.info("captions_enabled_via_keyboard")
            return True
        except Exception:
            pass

        log.warning("captions_enable_failed_this_attempt")
        return False

    @staticmethod
    def _dismiss_overlays(page) -> None:
        """Dismiss popups and banners that block join button interaction.

        Known overlays:
        - "Sign in with your Google account" → "Got it" button
        - "This browser version is no longer supported" → X close button
        - Cookie consent
        """
        dismiss_selectors = [
            "button:has-text('Got it')",
            "button:has-text('Dismiss')",
            "button:has-text('OK')",
            "[aria-label='Close' i]",
            "[aria-label='Dismiss' i]",
            "button.VfPpkd-LgbsSe[data-mdc-dialog-action='close']",
        ]
        for sel in dismiss_selectors:
            try:
                el = page.locator(sel)
                if el.count() > 0:
                    el.first.click()
                    log.info("overlay_dismissed", selector=sel)
                    time.sleep(0.5)
            except Exception:
                continue

        # Close "unsupported browser" banner (the top blue bar with X)
        try:
            close_btns = page.locator("button").all()
            for btn in close_btns[:20]:
                try:
                    aria = btn.get_attribute("aria-label") or ""
                    text = btn.inner_text(timeout=200).strip()
                    if any(k in aria.lower() or k in text.lower()
                           for k in ["close", "dismiss", "×"]):
                        btn.click()
                        log.info("banner_close_clicked", aria=aria, text=text[:20])
                        time.sleep(0.3)
                except Exception:
                    continue
        except Exception:
            pass

    @staticmethod
    def _check_rejected(page) -> str | None:
        """Check if Google Meet is showing a rejection/error page.

        Returns the rejection text if found, None otherwise.
        This enables fail-fast instead of waiting 2 minutes for admission.
        """
        try:
            body_text = page.inner_text("body", timeout=2000)
            for text in MEETING_REJECTED_TEXTS:
                if text.lower() in body_text.lower():
                    log.warning("meeting_rejected", rejection_text=text)
                    return text
        except Exception:
            pass
        return None

    @staticmethod
    def _sync_leave(page) -> None:
        """Click the leave button. Called from browser thread only."""
        if not page:
            return
        leave_selectors = [
            "[aria-label*='Leave call' i]",
            "[data-tooltip*='Leave call' i]",
            "button[jsname='CQylAd']",
        ]
        for sel in leave_selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    el.click()
                    time.sleep(1)
                    log.info("leave_button_clicked", selector=sel)
                    return
            except Exception:
                continue

    @staticmethod
    def _screenshot(page, name: str) -> None:
        """Save a debug screenshot to the project root."""
        try:
            path = _PROJECT_ROOT / f"debug_{name}.png"
            page.screenshot(path=str(path))
            log.info("screenshot_saved", name=name)
        except Exception:
            pass

    @staticmethod
    def _dump_page_state(page, label: str) -> None:
        """Log visible buttons, inputs, and page URL for debugging."""
        try:
            url = page.url
            title = page.title()
            log.info(f"page_state_{label}", url=url, title=title)

            # Log visible buttons
            buttons = page.locator("button").all()
            for i, btn in enumerate(buttons[:10]):
                try:
                    text = btn.inner_text(timeout=300).strip()
                    aria = btn.get_attribute("aria-label") or ""
                    if text or aria:
                        log.info(
                            f"page_button_{label}",
                            index=i,
                            text=text[:40],
                            aria_label=aria[:40],
                        )
                except Exception:
                    pass

            # Log visible inputs
            inputs = page.locator("input").all()
            for i, inp in enumerate(inputs[:5]):
                try:
                    ph = inp.get_attribute("placeholder") or ""
                    al = inp.get_attribute("aria-label") or ""
                    log.info(f"page_input_{label}", index=i, placeholder=ph, aria_label=al)
                except Exception:
                    pass

        except Exception as exc:
            log.warning("page_dump_failed", error=repr(exc))

    @staticmethod
    def _caption_js() -> str:
        """JS that extracts the latest visible caption text from the DOM.

        Confirmed selectors from 2026-04 diagnostic:
        - .VbkSUe   → Pure caption text (no speaker name)
        - [jsname='dsyhDe'] → Speaker name + caption text (newline separated)
        - .iOzk7    → Same as dsyhDe (wrapper)
        """
        return """
        (() => {
            // Strategy 1: .VbkSUe — cleanest caption text (no speaker name)
            try {
                const els = document.querySelectorAll('.VbkSUe');
                if (els.length > 0) {
                    const el = els[els.length - 1];
                    const text = el.textContent;
                    if (text && text.trim().length > 2) {
                        return text.trim();
                    }
                }
            } catch(e) {}

            // Strategy 2: [jsname='dsyhDe'] — contains "Speaker Name\\nCaption text"
            try {
                const els = document.querySelectorAll("[jsname='dsyhDe']");
                if (els.length > 0) {
                    const el = els[els.length - 1];
                    const text = el.textContent;
                    if (text && text.trim().length > 2) {
                        // Split by newline — second part is the caption text
                        const parts = text.trim().split('\\n');
                        return parts.length > 1 ? parts.slice(1).join(' ').trim() : text.trim();
                    }
                }
            } catch(e) {}

            // Strategy 3: .iOzk7 — same structure as dsyhDe
            try {
                const els = document.querySelectorAll('.iOzk7');
                if (els.length > 0) {
                    const el = els[els.length - 1];
                    const text = el.textContent;
                    if (text && text.trim().length > 2) {
                        const parts = text.trim().split('\\n');
                        return parts.length > 1 ? parts.slice(1).join(' ').trim() : text.trim();
                    }
                }
            } catch(e) {}

            // Strategy 4: data attribute fallbacks
            try {
                const els = document.querySelectorAll('[data-message-text]');
                if (els.length > 0) {
                    const text = els[els.length - 1].textContent;
                    if (text && text.trim().length > 2) return text.trim();
                }
            } catch(e) {}

            // Strategy 5: aria-live polite regions (broad fallback)
            try {
                const polite = document.querySelectorAll('[aria-live="polite"]');
                for (const el of polite) {
                    const text = el.textContent;
                    if (text && text.trim().length > 5) {
                        return text.trim();
                    }
                }
            } catch(e) {}

            return '';
        })()
        """
