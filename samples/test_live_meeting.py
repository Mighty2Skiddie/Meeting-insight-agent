"""
End-to-end integration test for the live meeting bot.

Usage:
    python samples/test_live_meeting.py --url https://meet.google.com/xxx-xxxx-xxx

The script:
1. Calls POST /api/v1/meetings/join-live
2. Connects to the WebSocket using the meeting_id from the response
3. Prints all real-time caption and insight events
4. Runs for `--duration` seconds then calls POST /stop-live
5. Waits for final analysis and prints the report

Prerequisites:
    pip install -e ".[live]"
    playwright install chromium
    uvicorn src.main:app --reload --port 8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

import httpx

BASE_URL = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"


async def run_test(meeting_url: str, duration_seconds: int = 120) -> None:
    # ── Pre-flight: verify server is up ───────────────────────────────────
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        try:
            health = await client.get("/health")
            if health.status_code != 200:
                print(f"✗ Server health check failed: {health.status_code}")
                sys.exit(1)
        except httpx.ConnectError:
            print("✗ Cannot connect to server at http://localhost:8000")
            print("  Start it first: uvicorn src.main:app --reload --port 8000")
            sys.exit(1)

    print(f"\n{'='*60}")
    print("Meeting Insight Agent — Live Meeting Test")
    print(f"{'='*60}")
    print(f"Target URL   : {meeting_url}")
    print(f"Max duration : {duration_seconds}s")
    print()

    # ── Step 1: Join the meeting ──────────────────────────────────────────
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        print("▶ Sending join-live request...")
        resp = await client.post(                      # ← await is required
            "/api/v1/meetings/join-live",
            json={"meeting_url": meeting_url, "bot_name": "Transcriber"},
        )

        if resp.status_code != 202:
            print(f"✗ Join failed ({resp.status_code}):")
            print(f"  {resp.text}")
            sys.exit(1)

        data = resp.json()
        meeting_id = data["meeting_id"]               # UUID from server
        ws_url = f"{WS_BASE}{data['ws_url']}"        # ws://localhost:8000/api/v1/meetings/{uuid}/live
        status_url = f"{BASE_URL}{data['status_url']}"
        stop_url = f"{BASE_URL}{data['stop_url']}"

        print(f"✓ Meeting created : {meeting_id}")
        print(f"  WebSocket URL   : {ws_url}")
        print(f"  Status URL      : {status_url}")
        print()

    # ── Step 2: Connect WebSocket and listen ─────────────────────────────
    print("▶ Connecting to WebSocket...")
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError:
        print("✗ Install websockets: pip install websockets")
        sys.exit(1)

    caption_count = 0
    start = asyncio.get_event_loop().time()

    try:
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=30) as ws:
            print(f"✓ WebSocket connected")
            print()
            print("Listening for events (speak in the meeting)...\n")
            print("-" * 60)

            deadline = start + duration_seconds

            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    print(f"\n⏱ {duration_seconds}s elapsed — stopping meeting...")
                    break

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 10))
                except asyncio.TimeoutError:
                    # No event in last 10s — check if deadline reached
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print("\n⚠ WebSocket closed by server")
                    break

                t = datetime.now().strftime("%H:%M:%S")
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[{t}] ⚠ Non-JSON message: {raw}")
                    continue

                etype = event.get("type")

                if etype == "connected":
                    print(f"[{t}] 🔗 Connected to meeting {event.get('meeting_id', '')[:8]}...")

                elif etype == "status":
                    status = event.get("status", "")
                    elapsed = event.get("elapsed_seconds", 0)
                    words = event.get("word_count", 0)
                    print(f"[{t}] 📊 {status} | {elapsed}s elapsed | {words} words")

                elif etype == "caption":
                    caption_count += 1
                    speaker = event.get("speaker", "Unknown")
                    text = event.get("text", "")
                    print(f"[{t}] 💬 [{speaker}] {text}")

                elif etype == "interim_insights":
                    print(f"[{t}] 🧠 Interim insights (degraded={event.get('degraded')})")
                    insights = event.get("insights") or {}
                    if insights.get("summary"):
                        preview = insights["summary"][:120]
                        print(f"     Summary: {preview}{'...' if len(insights['summary']) > 120 else ''}")

                elif etype == "meeting_ended":
                    status = event.get("status", "")
                    print(f"\n[{t}] 🏁 Meeting ended — {status}")
                    if status == "COMPLETED":
                        print(f"     Report: {BASE_URL}{event.get('report_url', '')}")
                    break

                elif etype == "error":
                    msg = event.get("message", "")
                    print(f"[{t}] ❌ Error: {msg}")
                    # Don't break — let meeting_ended handle cleanup

    except Exception as exc:
        print(f"✗ WebSocket error: {exc}")

    print("-" * 60)

    # ── Step 3: Stop the meeting if still running ────────────────────────
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        print("\n▶ Sending stop-live request...")
        resp = await client.post(stop_url)           # ← await required
        print(f"  {resp.status_code} — {resp.json().get('message', '')[:80]}")

    # ── Step 4: Wait for COMPLETED ───────────────────────────────────────
    print("\n▶ Waiting for final report...")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        for i in range(40):
            await asyncio.sleep(1)
            status_resp = await client.get(f"/api/v1/meetings/{meeting_id}/status")  # ← await
            current = status_resp.json().get("status", "UNKNOWN")
            if current == "COMPLETED":
                print(f"  ✓ COMPLETED after {i + 1}s")
                break
            if current == "FAILED":
                error = status_resp.json().get("error", "no details")
                print(f"  ✗ FAILED: {error}")
                sys.exit(1)
            if (i + 1) % 5 == 0:
                print(f"  [{i + 1}s] {current}...")
        else:
            print(f"  ⚠ Not completed within 40s. Check manually:")
            print(f"  curl {BASE_URL}/api/v1/meetings/{meeting_id}/report")
            return

    # ── Step 5: Print report ─────────────────────────────────────────────
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        report_resp = await client.get(f"/api/v1/meetings/{meeting_id}/report")  # ← await
        report = report_resp.json()

    print(f"\n{'='*60}")
    print("FINAL REPORT")
    print(f"{'='*60}")
    print(f"Meeting ID        : {meeting_id}")
    print(f"Captions captured : {caption_count}")
    print(f"Duration          : {report.get('duration_formatted', 'N/A')}")
    meta = report.get("metadata") or {}
    print(f"Provider (STT)    : {meta.get('provider_stt', 'N/A')}")
    print(f"Provider (LLM)    : {meta.get('provider_llm', 'N/A')}")
    print(f"Tier              : {meta.get('tier_used', 'N/A')}")
    print(f"Cost              : ${meta.get('cost_usd', 0):.4f}")

    insights = report.get("insights") or {}
    if insights:
        print(f"\nSummary:\n  {insights.get('summary', 'N/A')}")
        decisions = insights.get("key_decisions") or []
        if decisions:
            print(f"\nKey decisions ({len(decisions)}):")
            for d in decisions:
                print(f"  • {d}")
        actions = insights.get("action_items") or []
        if actions:
            print(f"\nAction items ({len(actions)}):")
            for item in actions:
                p = (item.get("priority") or "?").upper()
                t = item.get("task", "?")
                o = item.get("owner", "?")
                print(f"  [{p}] {t} → {o}")
        prod = insights.get("productivity") or {}
        if prod:
            print(f"\nProductivity      : {prod.get('score', '?')} (confidence={prod.get('confidence', '?')})")
            print(f"Sentiment         : {insights.get('sentiment', '?')}")
    else:
        print("\nNo insights generated (transcript may have been too short).")

    print()
    print(f"Full report: {BASE_URL}/api/v1/meetings/{meeting_id}/report")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test live Google Meet transcription",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python samples/test_live_meeting.py --url https://meet.google.com/abc-defg-hij
  python samples/test_live_meeting.py --url https://meet.google.com/abc-defg-hij --duration 300
        """,
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Google Meet URL (format: https://meet.google.com/xxx-xxxx-xxx)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=120,
        help="Max duration to listen in seconds (default: 120)",
    )
    args = parser.parse_args()
    asyncio.run(run_test(args.url, args.duration))
