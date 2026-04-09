#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Meeting Insight Agent — API curl examples
#  Replace BASE_URL with your deployed URL or http://localhost:8000
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL="http://127.0.0.1:8000/api/v1"

echo "== 1. Health Check =="
curl -s "${BASE_URL%/api/v1}/health" | python -m json.tool

echo ""
echo "== 2. Readiness Check =="
curl -s "${BASE_URL%/api/v1}/readiness" | python -m json.tool

echo ""
echo "== 3. Budget Status =="
curl -s "${BASE_URL}/budget" | python -m json.tool

echo ""
echo "== 4. Upload a meeting file =="
# Creating a dummy file temporarily for testing
echo "Dummy audio data" > samples/sample_meeting.mp3
UPLOAD_RESPONSE=$(curl -s -X POST "${BASE_URL}/meetings/upload" \
  -F "file=@samples/sample_meeting.mp3" \
  -F "title=Sample Sprint Planning")
echo "$UPLOAD_RESPONSE" | python -m json.tool

MEETING_ID=$(echo "$UPLOAD_RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin)['meeting_id'])" 2>/dev/null)

if [ -n "$MEETING_ID" ]; then
  echo ""
  echo "== 5. Poll Status (meeting_id: $MEETING_ID) =="
  sleep 5
  curl -s "${BASE_URL}/meetings/${MEETING_ID}/status" | python -m json.tool

  echo ""
  echo "== 6. Get Full Report (after processing completes) =="
  echo "   Run this after status shows COMPLETED:"
  echo "   curl -s '${BASE_URL}/meetings/${MEETING_ID}/report' | python -m json.tool"
fi

echo ""
echo "== 7. Analyze Raw Transcript =="
curl -s -X POST "${BASE_URL}/meetings/analyze" \
  -H "Content-Type: application/json" \
  -d '{
    "transcript_text": "Alice: Good morning everyone. Lets start. We need to decide on the Q2 roadmap. Bob: I think we should prioritize the mobile app launch. Alice: Agreed. Bob will lead the mobile team. Deadline is June 15th. Carol: I will handle the marketing campaign. Alice: Great, lets meet again next Friday to review progress.",
    "duration_seconds": 60
  }' | python -m json.tool

echo ""
echo "== 8. Prometheus Metrics =="
curl -s "${BASE_URL%/api/v1}/metrics" | head -40
