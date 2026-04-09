"""
End-to-end integration test for the Meeting Insight Agent API.
Cross-platform (Windows / macOS / Linux) — no curl dependency.
"""
import asyncio
import json
import sys

import httpx

BASE_URL = "http://127.0.0.1:8000"
API = f"{BASE_URL}/api/v1"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_POLL_ITERATIONS = 60  # 2s × 60 = 2 min max wait

passed = 0
failed = 0


def report(name: str, ok: bool, detail: str = ""):
    global passed, failed
    icon = "✅" if ok else "❌"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {icon} {name}" + (f"  — {detail}" if detail else ""))


async def main():
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:

        # ── 1. Health Check ──────────────────────────────────────────────
        print("\n== 1. Health Check ==")
        try:
            r = await client.get(f"{BASE_URL}/health")
            data = r.json()
            print(json.dumps(data, indent=2))
            report("GET /health", r.status_code == 200 and data.get("status") == "ok")
        except Exception as e:
            report("GET /health", False, repr(e))

        # ── 2. Readiness Check ───────────────────────────────────────────
        print("\n== 2. Readiness Check ==")
        try:
            r = await client.get(f"{BASE_URL}/readiness")
            data = r.json()
            print(json.dumps(data, indent=2))
            report("GET /readiness", r.status_code == 200 and data.get("status") in ("ready", "degraded"))
        except Exception as e:
            report("GET /readiness", False, repr(e))

        # ── 3. Budget Status ─────────────────────────────────────────────
        print("\n== 3. Budget Status ==")
        try:
            r = await client.get(f"{API}/budget")
            data = r.json()
            print(json.dumps(data, indent=2))
            report("GET /api/v1/budget", r.status_code == 200 and "total_budget_usd" in data)
        except Exception as e:
            report("GET /api/v1/budget", False, repr(e))

        # ── 4. Upload a meeting file ─────────────────────────────────────
        print("\n== 4. Upload Meeting File ==")
        try:
            with open("samples/Multimodel document_Audio Denoise.mp3", "rb") as f:
                r = await client.post(
                    f"{API}/meetings/upload",
                    data={"title": "Sprint Planning Demo"},
                    files={"file": ("demo_meeting.mp3", f, "audio/mpeg")},
                )
            upload_resp = r.json()
            print(json.dumps(upload_resp, indent=2))
            meeting_id = upload_resp.get("meeting_id")
            report("POST /upload", r.status_code == 202 and meeting_id is not None)
        except Exception as e:
            meeting_id = None
            report("POST /upload", False, repr(e))

        # ── 5. Poll Status ───────────────────────────────────────────────
        if meeting_id:
            print(f"\n== 5. Poll Status (meeting_id: {meeting_id}) ==")
            final_status = "UNKNOWN"
            for i in range(MAX_POLL_ITERATIONS):
                await asyncio.sleep(2)
                try:
                    status_r = await client.get(f"{API}/meetings/{meeting_id}/status")
                    status_data = status_r.json()
                    step = status_data.get("current_step", "")
                    pct = status_data.get("progress_percent", 0)
                    final_status = status_data.get("status", "UNKNOWN")
                    print(f"  [{i+1:02d}] {final_status} ({pct}%) — {step}")

                    if final_status in ("COMPLETED", "FAILED"):
                        if final_status == "FAILED":
                            print(f"       Error: {status_data.get('error', 'unknown')}")
                        break
                except Exception as e:
                    print(f"  [{i+1:02d}] Poll error: {e}")
            else:
                print("  ⚠️  Max poll iterations reached — giving up")

            report("Poll → terminal state", final_status in ("COMPLETED", "FAILED"))

            # ── 6. Get Report (if completed) ─────────────────────────────
            if final_status == "COMPLETED":
                print("\n== 6. Get Full Report ==")
                try:
                    report_r = await client.get(f"{API}/meetings/{meeting_id}/report")
                    report_data = report_r.json()
                    print(json.dumps(report_data, indent=2)[:2000])
                    report("GET /report", report_r.status_code == 200)
                except Exception as e:
                    report("GET /report", False, repr(e))

        # ── 7. Analyze Raw Transcript ────────────────────────────────────
        print("\n== 7. Analyze Raw Transcript ==")
        try:
            body = {
                "transcript": (
                    "Alice: Good morning! We need to finalize the marketing budget today. "
                    "Bob: I propose we allocate 5000 dollars to the new digital campaign. "
                    "Alice: Agreed. Let's have the proposal ready by Friday."
                )
            }
            r = await client.post(f"{API}/meetings/analyze", json=body)
            data = r.json()
            print(json.dumps(data, indent=2))
            report("POST /analyze", r.status_code == 202 and data.get("meeting_id") is not None)
        except Exception as e:
            report("POST /analyze", False, repr(e))

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print(f"  RESULTS:  {passed} passed,  {failed} failed")
    print("=" * 50)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
