"""
WebSocket broadcaster for real-time live meeting events.

Clients connect to ws://.../api/v1/meetings/{id}/live and receive
caption events, status updates, and interim insights as JSON.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

# Registry: meeting_id → set of connected WebSocket clients
_connections: dict[str, set[WebSocket]] = {}
_lock = asyncio.Lock()


async def register(meeting_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    async with _lock:
        _connections.setdefault(meeting_id, set()).add(websocket)


async def unregister(meeting_id: str, websocket: WebSocket) -> None:
    async with _lock:
        _connections.get(meeting_id, set()).discard(websocket)


async def broadcast(meeting_id: str, event: dict[str, Any]) -> None:
    """Send an event to all WebSocket clients watching this meeting."""
    dead: set[WebSocket] = set()
    for ws in list(_connections.get(meeting_id, set())):
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    if dead:
        async with _lock:
            _connections.get(meeting_id, set()).difference_update(dead)


async def live_transcript_ws(websocket: WebSocket, meeting_id: str) -> None:
    """
    WebSocket handler — mount at /api/v1/meetings/{meeting_id}/live

    Event types sent to clients:
    - {"type": "caption",          "speaker": "Alice", "text": "...", "ts": 1234.5}
    - {"type": "status",           "status": "LIVE_TRANSCRIBING", "elapsed_seconds": 120}
    - {"type": "interim_insights", "insights": {...}, "elapsed_seconds": 300}
    - {"type": "meeting_ended",    "meeting_id": "uuid"}
    - {"type": "error",            "message": "..."}
    """
    await register(meeting_id, websocket)
    try:
        # Send initial connection confirmation
        await websocket.send_json({"type": "connected", "meeting_id": meeting_id})
        # Keep alive — clients can send any text as a ping
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await unregister(meeting_id, websocket)
