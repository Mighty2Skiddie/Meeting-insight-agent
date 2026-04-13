# Meeting Insight Agent 🎙️

> **AI-powered meeting analysis** — transcription, structured insights, action items, and productivity evaluation. OpenAI-first with intelligent 4-tier fallback chains.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat&logo=fastapi&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-Whisper%20%2B%20GPT--4o-412991?style=flat&logo=openai&logoColor=white)
![Groq](https://img.shields.io/badge/Groq-Whisper%20v3%20%2B%20LLaMA%203.3-F55036?style=flat&logo=groq&logoColor=white)
![Gemini](https://img.shields.io/badge/Google-Gemini%20Flash%202.0-4285F4?style=flat&logo=google&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-WAL%20Mode-003B57?style=flat&logo=sqlite&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0%20Async-D71F00?style=flat&logo=sqlalchemy&logoColor=white)
![Prometheus](https://img.shields.io/badge/Prometheus-Metrics-E6522C?style=flat&logo=prometheus&logoColor=white)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-Tracing-425CC7?style=flat&logo=opentelemetry&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Multi--stage%20Build-2496ED?style=flat&logo=docker&logoColor=white)
![Render](https://img.shields.io/badge/Render-Deploy%20Ready-46E3B7?style=flat&logo=render&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow?style=flat)


---

## Live Demo

| Resource | URL |
|:---------|:----|
| **API Base** | `https://meeting-insight-agent.onrender.com/api/v1` |
| **Swagger UI** | `https://meeting-insight-agent.onrender.com/docs` |
| **ReDoc** | `https://meeting-insight-agent.onrender.com/redoc` |
| **Health** | `https://meeting-insight-agent.onrender.com/health` |

---

## System Architecture

### High-Level Architecture

```mermaid
graph TB
    subgraph INPUT ["📥 Meeting Input Layer"]
        UF["Audio / Video File Upload<br/>.mp3 · .mp4 · .wav · .m4a · .webm"]
        RT["Raw Transcript Text<br/>Direct string input via API"]
    end

    subgraph EDGE ["🌐 Edge Layer — FastAPI Gateway"]
        RL["Rate Limiter<br/>slowapi"]
        CID["Correlation ID<br/>asgi-correlation-id"]
        TM["Request Timing<br/>Middleware"]
    end

    subgraph APP ["⚙️ Application Layer"]
        SVC["MeetingService<br/>Pipeline Orchestrator"]
        CT["CostTracker<br/>Budget Ledger"]
    end

    subgraph ROUTER ["🧠 Provider Router — Cost-Aware"]
        BG["Budget Guard<br/>Premium if remaining > $0.20"]
        CB["Circuit Breakers<br/>aiobreaker — per service"]
        RT2["Retry + Jitter<br/>tenacity"]
    end

    subgraph AI ["🤖 AI Provider Tiers"]
        T1_STT["🥇 OpenAI Whisper<br/>whisper-1 · $0.006/min"]
        T1_LLM["🥇 GPT-4o-mini<br/>Structured JSON Output"]
        T2_STT["🥈 Groq Whisper v3<br/>FREE · LPU Speed"]
        T2_LLM["🥈 Gemini 2.0 Flash<br/>FREE · 1M token context"]
        T3_LLM["🥉 Groq Llama 3.3 70B<br/>FREE · ultra-fast"]
        T4_LLM["🛡️ Rule-Based Engine<br/>Offline · always works"]
    end

    subgraph INFRA ["🗄️ Infrastructure"]
        DB[("SQLite + aiosqlite<br/>Meetings + Cost Ledger")]
        FS["Local FileSystem<br/>UUID-named uploads"]
        BG2["BackgroundTasks<br/>Async pipeline"]
    end

    subgraph OBS ["📊 Observability"]
        SL["structlog<br/>JSON structured logs"]
        PROM["/metrics<br/>Prometheus"]
        HC["/health · /readiness<br/>Liveness + Readiness"]
        BUDGET["/budget<br/>Real-time cost dashboard"]
    end

    UF --> EDGE
    RT --> EDGE
    EDGE --> APP
    APP --> ROUTER
    ROUTER --> BG
    BG -->|"budget OK"| T1_STT
    BG -->|"budget OK"| T1_LLM
    BG -->|"budget low / circuit open"| T2_STT
    BG -->|"budget low / circuit open"| T2_LLM
    T2_LLM -->|"circuit open"| T3_LLM
    T3_LLM -->|"circuit open"| T4_LLM
    APP --> INFRA
    APP --> OBS
```

### End-to-End Processing Pipeline

```mermaid
sequenceDiagram
    participant C as Client
    participant GW as FastAPI Gateway
    participant SVC as MeetingService
    participant BG as BackgroundTask
    participant FFmpeg as FFmpeg Processor
    participant Router as Provider Router
    participant OpenAI as OpenAI API
    participant Groq as Groq API (Free)
    participant DB as SQLite

    C->>GW: POST /upload-meeting (file)
    GW->>SVC: create_meeting(file)
    SVC->>DB: INSERT meeting status=UPLOADED
    SVC->>BG: schedule pipeline
    GW-->>C: 202 Accepted + meeting_id

    Note over BG: Async pipeline starts
    BG->>FFmpeg: Convert to 16kHz mono MP3
    BG->>DB: UPDATE status=TRANSCRIBING

    alt Budget available AND OpenAI circuit closed
        BG->>Router: transcribe via Tier 1
        Router->>OpenAI: Whisper API
        OpenAI-->>Router: transcript + timestamps
    else Budget low OR OpenAI down
        BG->>Router: transcribe via Tier 2
        Router->>Groq: Whisper v3 (free)
        Groq-->>Router: transcript + timestamps
    end

    BG->>DB: UPDATE transcript status=ANALYZING

    alt Budget OK
        Router->>OpenAI: GPT-4o-mini structured output
        OpenAI-->>Router: guaranteed valid JSON insights
    else Fallback chain
        Router->>Groq: Gemini → Llama → Rule Engine
    end

    BG->>DB: UPDATE insights status=COMPLETED
    C->>GW: GET /meeting-report/id
    GW-->>C: 200 OK full structured report
```

### Provider Priority Matrix

| Priority | STT | LLM | Cost | Quality |
|:---------|:----|:----|:-----|:--------|
| 🥇 Tier 1 | OpenAI Whisper | GPT-4o-mini | ~$0.20/meeting | ★★★★★ |
| 🥈 Tier 2 | Groq Whisper v3 | Gemini 2.0 Flash | $0 | ★★★★☆ |
| 🥉 Tier 3 | — | Groq Llama 3.3 70B | $0 | ★★★☆☆ |
| 🛡️ Tier 4 | — | Rule-Based Engine | $0 | ★★☆☆☆ |

---

## Model Selection

| Task | Primary | Fallback 1 | Fallback 2 | Fallback 3 |
|:-----|:--------|:----------|:----------|:----------|
| **Speech-to-Text** | OpenAI `whisper-1` | Groq `whisper-large-v3` | — | — |
| **Meeting Analysis** | OpenAI `gpt-4o-mini` (structured output) | Google `gemini-2.0-flash` | Groq `llama-3.3-70b-versatile` | Regex rule engine |

**Why GPT-4o-mini?** — Native `response_format: json_schema` support guarantees the output matches the Pydantic schema exactly. No parsing failures, no missing fields.

**Why Groq?** — Fastest inference available (LPU hardware), completely free tier, near-OpenAI quality on Whisper.

---

## API Endpoints

| Method | Endpoint | Description |
|:-------|:---------|:-----------|
| `POST` | `/api/v1/meetings/upload` | Upload audio/video file |
| `POST` | `/api/v1/meetings/analyze` | Analyze meeting or raw transcript |
| `GET` | `/api/v1/meetings/{id}/status` | Poll processing status |
| `GET` | `/api/v1/meetings/{id}/report` | Get full structured report |
| `GET` | `/api/v1/budget` | Real-time cost tracking dashboard |
| `GET` | `/health` | Liveness probe |
| `GET` | `/readiness` | Readiness probe (checks all deps) |
| `GET` | `/metrics` | Prometheus metrics |

---

## Quick Start (Local)

### Prerequisites

| Requirement | Version | Install |
|:------------|:--------|:--------|
| Python | 3.12+ | [python.org](https://python.org) |
| FFmpeg | Any | `choco install ffmpeg` · `brew install ffmpeg` · `apt install ffmpeg` |
| Git | Any | [git-scm.com](https://git-scm.com) |
| API Keys | — | OpenAI + Groq (free) + Gemini (free) |

Get free keys:
- **Groq** (required for free tier STT + LLM): [console.groq.com](https://console.groq.com) → create key
- **Gemini** (optional, extra LLM fallback): [aistudio.google.com](https://aistudio.google.com) → Get API key
- **OpenAI** (optional, best quality): [platform.openai.com/api-keys](https://platform.openai.com/api-keys)

---

### Step-by-Step Setup

**1. Clone the repository**

```bash
git clone https://github.com/Mighty2Skiddie/Meeting-insight-agent
cd Meeting-insight-agent
```

**2. Create a virtual environment**

```bash
# Windows
python -m venv myvenv
myvenv\Scripts\activate

# macOS / Linux
python -m venv myvenv
source myvenv/bin/activate
```

**3. Install dependencies**

```bash
pip install -e ".[dev]"
```

**4. Configure environment variables**

```bash
# Copy the example env file
cp .env.example .env
```

Open `.env` and set your values:

```env
# ── Required ──────────────────────────────────────────────
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GEMINI_API_KEY=AIxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ── Recommended (enables best-quality Tier 1) ─────────────
OPENAI_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxxxxxx

# ── Storage (leave as-is for local development) ───────────
DATABASE_URL=sqlite+aiosqlite:///./data/meetings.db
UPLOAD_DIR=./data/uploads

# ── Tuning ────────────────────────────────────────────────
ENVIRONMENT=development
LOG_LEVEL=INFO
BUDGET_LIMIT_USD=2.00
BUDGET_RESERVE_USD=0.20
MAX_UPLOAD_SIZE_MB=200
```

> **Note:** Keys prefixed `gsk_` are Groq keys. Keys prefixed `sk-` are OpenAI keys. Gemini keys start with `AI`. The system validates these prefixes at startup — a wrong format is treated as missing.

**5. Verify FFmpeg is installed**

```bash
ffmpeg -version
# Should print: ffmpeg version X.X ...
```

If it fails on Windows: `choco install ffmpeg` then restart your terminal.

**6. Run the development server**

```bash
uvicorn src.main:app --reload --port 8000
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process
```

**7. Verify it's working**

```bash
curl http://localhost:8000/health
# {"status":"ok","uptime_seconds":2.1,"version":"1.0.0"}

curl http://localhost:8000/readiness
# {"status":"ready","checks":{"database":{"status":"ok"},"api_keys":{"openai":"configured","groq":"configured","gemini":"configured"}}}
```

---

### Docker (Alternative)

```bash
cp .env.example .env   # Fill in your keys
docker build -t meeting-insight-agent .
docker run -p 8000:8000 --env-file .env meeting-insight-agent
```

---

## Using the API

### Interactive Swagger UI (Recommended for First Use)

Open **http://localhost:8000/docs** in your browser.

You get a full interactive UI where you can:
- Upload audio files directly with a file picker
- See exact request/response schemas
- Try every endpoint with real data
- Authorize with API keys if needed

> **Tip:** Click **"Try it out"** on any endpoint, fill in the fields, and click **"Execute"**. The UI shows you the exact `curl` command it used.

---

### Endpoint Reference — Complete Usage Guide

#### 1. Upload a Meeting File

Accepts any audio or video format. Returns a `meeting_id` immediately — processing happens in the background.

```bash
curl -X POST http://localhost:8000/api/v1/meetings/upload \
  -F "file=@/path/to/your/meeting.mp3" \
  -F "title=Q2 Sprint Planning"
```

**Supported formats:** `.mp3` `.mp4` `.wav` `.m4a` `.webm` `.ogg` `.flac` `.mkv` `.mov` `.avi`

**Response:**
```json
{
  "meeting_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "UPLOADED",
  "estimated_duration_seconds": 60,
  "provider_tier": "premium",
  "tracking_url": "/api/v1/meetings/550e8400-e29b-41d4-a716-446655440000/status"
}
```

Save the `meeting_id` — you'll need it for all subsequent calls.

**Python client:**
```python
import httpx

with open("meeting.mp3", "rb") as f:
    response = httpx.post(
        "http://localhost:8000/api/v1/meetings/upload",
        files={"file": ("meeting.mp3", f, "audio/mpeg")},
        data={"title": "Q2 Sprint Planning"},
    )

data = response.json()
meeting_id = data["meeting_id"]
print(f"Uploaded. Tracking: {data['tracking_url']}")
```

---

#### 2. Poll Processing Status

Processing runs in the background. Poll this endpoint every 2-3 seconds until status is `COMPLETED` or `FAILED`.

```bash
curl http://localhost:8000/api/v1/meetings/550e8400-e29b-41d4-a716-446655440000/status
```

**Response (in progress):**
```json
{
  "meeting_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "TRANSCRIBING",
  "progress_percent": 25,
  "current_step": "Transcribing audio",
  "provider_tier": "free",
  "error": null
}
```

**Status progression:**
```
UPLOADED (5%) → TRANSCRIBING (10-49%) → ANALYZING (50-99%) → COMPLETED (100%)
                                                            ↘ FAILED (any step)
```

**Python polling loop:**
```python
import time
import httpx

def wait_for_completion(meeting_id: str, base_url: str = "http://localhost:8000") -> dict:
    url = f"{base_url}/api/v1/meetings/{meeting_id}/status"
    for attempt in range(60):  # max 2 minutes at 2s intervals
        response = httpx.get(url)
        data = response.json()
        status = data["status"]
        print(f"[{attempt*2}s] {status} — {data['progress_percent']}% — {data['current_step']}")
        if status == "COMPLETED":
            return data
        if status == "FAILED":
            raise RuntimeError(f"Processing failed: {data['error']}")
        time.sleep(2)
    raise TimeoutError("Processing did not complete within 2 minutes")

result = wait_for_completion(meeting_id)
```

---

#### 3. Get the Full Report

Only call this when status is `COMPLETED`. Returns the full transcript and all AI-generated insights.

```bash
curl http://localhost:8000/api/v1/meetings/550e8400-e29b-41d4-a716-446655440000/report
```

**Full response schema:**
```json
{
  "meeting_id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Q2 Sprint Planning",
  "duration_seconds": 2340.5,
  "duration_formatted": "39m 0s",
  "transcript": {
    "full_text": "Alice: Let's start with the mobile launch timeline...",
    "segments": [
      {
        "speaker": "Speaker 1",
        "start": 0.0,
        "end": 12.4,
        "text": "Let's start with the mobile launch timeline."
      }
    ],
    "word_count": 542,
    "language": "English"
  },
  "insights": {
    "summary": "The team aligned on Q2 priorities, confirming the mobile app launch as the top initiative for June with Bob leading delivery...",
    "key_decisions": [
      "Mobile app launch scheduled for June 15th",
      "Budget approved for third-party testing partner"
    ],
    "action_items": [
      {
        "task": "Finalize mobile app delivery plan",
        "owner": "Speaker 1",
        "priority": "high",
        "deadline_mentioned": "June 15th"
      },
      {
        "task": "Shortlist testing vendors and share with team",
        "owner": "Speaker 2",
        "priority": "medium",
        "deadline_mentioned": null
      }
    ],
    "discussion_topics": [
      {
        "topic": "Mobile app launch timeline",
        "time_spent_percent": 40,
        "resolution": "resolved"
      },
      {
        "topic": "Q2 budget allocation",
        "time_spent_percent": 35,
        "resolution": "resolved"
      }
    ],
    "productivity": {
      "score": "Productive",
      "reasoning": "Clear decisions were made with assigned owners. The meeting stayed on topic with concrete outcomes for both agenda items.",
      "confidence": 0.87,
      "improvement_suggestions": [
        "Share agenda 24 hours before next meeting to improve prep time"
      ]
    },
    "sentiment": "Positive",
    "follow_up_meeting_needed": true
  },
  "metadata": {
    "status": "COMPLETED",
    "provider_stt": "openai_whisper",
    "provider_llm": "gpt_4o_mini",
    "tier_used": "premium",
    "degraded": false,
    "cost_usd": 0.21,
    "processing_time_seconds": 18.4,
    "created_at": "2026-04-10T09:20:47",
    "completed_at": "2026-04-10T09:21:05"
  }
}
```

**Python — one-liner full workflow:**
```python
import time, httpx

BASE = "http://localhost:8000"

# Upload
with open("meeting.mp3", "rb") as f:
    resp = httpx.post(f"{BASE}/api/v1/meetings/upload", files={"file": f})
meeting_id = resp.json()["meeting_id"]

# Poll
while True:
    status = httpx.get(f"{BASE}/api/v1/meetings/{meeting_id}/status").json()
    if status["status"] in ("COMPLETED", "FAILED"):
        break
    time.sleep(2)

# Report
report = httpx.get(f"{BASE}/api/v1/meetings/{meeting_id}/report").json()
print(report["insights"]["summary"])
print("Action items:")
for item in report["insights"]["action_items"]:
    print(f"  [{item['priority'].upper()}] {item['task']} → {item['owner']}")
```

---

#### 4. Analyze a Raw Transcript (No File Upload)

Have text already? Skip the audio step entirely.

```bash
curl -X POST http://localhost:8000/api/v1/meetings/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "transcript": "Alice: We need to decide on the launch date. Bob: I think June 15th works. Alice: Agreed. Bob: I will prepare the rollout plan by Friday."
  }'
```

**Response:**
```json
{
  "meeting_id": "7f3a9200-...",
  "status": "COMPLETED",
  "insights": {
    "summary": "A brief alignment meeting where Alice and Bob agreed on a June 15th launch date...",
    "key_decisions": ["Launch date set to June 15th"],
    "action_items": [
      {
        "task": "Prepare rollout plan",
        "owner": "Bob",
        "priority": "high",
        "deadline_mentioned": "Friday"
      }
    ],
    "productivity": {
      "score": "Productive",
      "reasoning": "Clear decision made immediately with a concrete action item assigned.",
      "confidence": 0.95
    },
    "sentiment": "Positive",
    "follow_up_meeting_needed": false
  },
  "provider": "gpt_4o_mini",
  "cost_usd": 0.001
}
```

---

#### 5. Check Budget Usage

```bash
curl http://localhost:8000/api/v1/budget
```

**Response:**
```json
{
  "budget_limit_usd": 2.00,
  "total_spent_usd": 0.63,
  "remaining_usd": 1.37,
  "reserve_usd": 0.20,
  "premium_available": true,
  "active_tier": "premium",
  "breakdown_by_provider": {
    "openai_whisper": 0.42,
    "gpt_4o_mini": 0.21
  },
  "meetings_processed": 3
}
```

When `remaining_usd` drops below `reserve_usd`, the system automatically switches from OpenAI to free-tier providers.

---

#### 6. Readiness Check — Verify Your Setup

```bash
curl http://localhost:8000/readiness | python -m json.tool
```

**What to look for:**
```json
{
  "status": "ready",
  "checks": {
    "database": { "status": "ok", "latency_ms": 0.4 },
    "openai_api": { "status": "ok", "circuit": "closed" },
    "groq_api":   { "status": "ok", "circuit": "closed" },
    "gemini_api": { "status": "ok", "circuit": "closed" },
    "budget": {
      "status": "ok",
      "remaining_usd": 1.37,
      "active_tier": "premium"
    },
    "api_keys": {
      "openai": "configured",
      "groq": "configured",
      "gemini": "configured"
    }
  }
}
```

| Value to check | Good | Bad |
|:--------------|:-----|:----|
| `status` | `"ready"` | `"not_ready"` or `"degraded"` |
| `database.status` | `"ok"` | `"error"` — check `DATABASE_URL` |
| `api_keys.openai` | `"configured"` | `"missing"` — check your `.env` key format (`sk-...`) |
| `api_keys.groq` | `"configured"` | `"missing"` — check your `.env` key format (`gsk_...`) |
| `budget.status` | `"ok"` | `"low"` — remaining <= reserve; free tier only |
| `*.circuit` | `"closed"` | `"open"` — provider was failing; waits 2 min to reset |

---

#### 7. Prometheus Metrics

```bash
curl http://localhost:8000/metrics
```

Connect Grafana or any Prometheus-compatible tool to this endpoint. Key metrics:

| Metric | Type | What it tells you |
|:-------|:-----|:-----------------|
| `meetings_processed_total` | Counter | Total completed/failed meetings |
| `meeting_processing_duration_seconds` | Histogram | End-to-end pipeline time |
| `transcription_duration_seconds` | Histogram | STT time per provider |
| `analysis_duration_seconds` | Histogram | LLM time per provider |
| `active_background_jobs` | Gauge | Pipelines running right now |
| `cost_per_meeting_usd` | Histogram | Dollar cost distribution |
| `fallback_activations_total` | Counter | How often cheaper tiers were used |

---

### Common Issues

| Issue | Likely Cause | Fix |
|:------|:------------|:----|
| `api_keys.groq: "missing"` | Key format wrong | Groq keys start with `gsk_` — regenerate if needed |
| `api_keys.openai: "missing"` | Key format wrong | OpenAI keys start with `sk-` or `sk-proj-` |
| `status: "FAILED"` on upload | FFmpeg not found | Run `ffmpeg -version`. Install if missing. |
| `status: "FAILED"` on analyze | LLM API error | Check `/readiness` → look for open circuit breakers |
| Upload returns 413 | File too large | Default max is 200MB. Change `MAX_UPLOAD_SIZE_MB` in `.env` |
| Upload returns 415 | Wrong file type | Only audio/video formats are accepted |
| `database.status: "error"` | DB path not writable | Create the `./data/` directory manually or change `DATABASE_URL` |
| Insights always `null` | All LLM providers failed | Check `/readiness` — all providers may be circuit-broken. Wait 2 min. |

---

## Running Tests

```bash
# Unit tests only (no API calls, uses in-memory SQLite)
pytest tests/unit/ -v

# With coverage report
pytest tests/unit/ -v --cov=src --cov-report=term-missing

# Type checking
mypy src/ --ignore-missing-imports

# Linting
ruff check src/ tests/

# End-to-end integration test (requires running server + valid API keys)
python samples/test_api.py
# Uploads sample file → polls until complete → validates report fields
```

---



| Decision | Rationale |
|:---------|:----------|
| **SQLite over PostgreSQL** | Zero-config for Assignment. Abstracted via Repository pattern — swap to Postgres with one config line. |
| **BackgroundTasks over ARQ/Celery** | No Redis needed → fits Render free 512MB RAM. Trade-off: jobs lost on crash (acceptable for prototype). |
| **Cloud-only STT fallback** | Local Faster-Whisper needs ~1GB RAM; Render free tier has 512MB. Groq free STT is equivalent quality. |
| **Rule-based Tier 4** | Always-on insurance policy — the system produces *something* even with zero API connectivity. |
| **GPT-4o-mini over GPT-4o** | 95% quality at 10% of the price (within $2 budget). Structured outputs eliminate parsing risk. |
| **OpenTelemetry over Datadog SDK** | Vendor neutral — can switch to any observability backend without code changes. |

---

## Live Meeting Integration — Design Decision & Production Path

### Why Live Join Is Not Implemented

The assignment explicitly states the system should support **"at least one of"**: join a live meeting **OR** process a recorded file. This is a deliberate scope qualifier — live meeting bots are a significantly more complex infrastructure problem, not an AI problem.

Here is the honest engineering reality:

| Platform | Official Bot API? | What It Actually Requires | Infrastructure Cost |
|:---------|:-----------------|:--------------------------|:--------------------|
| **Zoom** | ✅ Meeting SDK | Zoom Bot app + Zoom paid business plan ($13.33/mo) + persistent server to run the bot participant | Paid plan required |
| **Google Meet** | ❌ No public bot API | Headless browser (Puppeteer/Playwright) joins as a human user, captures tab audio via `getDisplayMedia()` | Fragile — breaks on every UI update |
| **Microsoft Teams** | ⚠️ Partial | Azure AD app registration + Microsoft Graph Communications API + Teams business license | Azure costs + M365 license |

> **The core problem**: Every platform treats bots as a security threat. Getting audio in real-time requires either paying for SDK access, running a headless browser that impersonates a human, or paying an intermediary service like Recall.ai.

### How Production Systems Solve This

Companies like **Fireflies.ai**, **Otter.ai**, and **Notion AI Meetings** all use the same pattern:

```mermaid
graph LR
    subgraph "Live Meeting Platforms"
        ZM[Zoom]
        GM[Google Meet]
        MT[MS Teams]
    end

    subgraph "Bot Infrastructure"
        HB["Headless Browser<br/>Playwright / Puppeteer<br/>on a GPU server"]
        AC["Audio Capture<br/>getDisplayMedia() API"]
        BUF["Audio Buffer<br/>Chunked streaming"]
    end

    subgraph "Our Pipeline"
        STT["Whisper STT<br/>streaming chunks"]
        LLM["GPT-4o-mini<br/>real-time analysis"]
        API["Meeting Insight API"]
    end

    ZM -->|"Bot joins as participant"| HB
    GM -->|"Bot joins via browser"| HB
    MT -->|"Bot joins via browser"| HB
    HB --> AC --> BUF --> STT --> LLM --> API
```

**The stack that makes this work in production:**
- **Recall.ai** — managed bot SDK ($99/month) — joins any platform, returns audio stream
- **Playwright** — open source headless browser, self-hosted, brittle
- **OpenAI Realtime API** (`gpt-4o-realtime-preview`) — streams audio in 100ms chunks, returns live transcript + analysis

### Our Architecture Is Ready For It

This codebase is designed with a **pluggable input adapter pattern**. Adding live meeting support requires implementing exactly one interface:

```python
# src/inputs/base.py  ← add this file
class MeetingInputAdapter(ABC):
    @abstractmethod
    async def capture_audio(self, meeting_url: str) -> AsyncIterator[bytes]:
        """Yields audio chunks in real-time from a live meeting."""
        ...

# src/inputs/recall_adapter.py  ← implement for Recall.ai ($99/mo)
class RecallMeetingAdapter(MeetingInputAdapter):
    async def capture_audio(self, meeting_url: str) -> AsyncIterator[bytes]:
        # 1. POST to Recall.ai API — bot joins the meeting
        # 2. Recall streams audio back via webhook
        # 3. Yield chunks to Whisper streaming API
        ...

# src/inputs/playwright_adapter.py  ← implement for free (brittle)
class PlaywrightMeetingAdapter(MeetingInputAdapter):
    async def capture_audio(self, meeting_url: str) -> AsyncIterator[bytes]:
        # 1. Launch headless Chromium
        # 2. Join meeting URL as guest
        # 3. Capture tab audio via CDP (Chrome DevTools Protocol)
        # 4. Yield audio to Whisper
        ...
```

The `MeetingService` would call `adapter.capture_audio(url)` and pipe chunks into the same transcription and analysis pipeline that already exists — **zero changes to the AI or API layer**.

### Why We Chose File Upload For This Assignment

1. **Assignment allows it** — "at least one of" is satisfied by file upload
2. **Same AI pipeline** — the transcription and analysis quality is identical whether audio comes from a file or a live stream
3. **No paid infrastructure** — live join requires either a paid bot platform or a persistent VM to run a headless browser
4. **More robust for evaluation** — a reviewer can upload a sample file and see results instantly; a live bot requires an active meeting
5. **Production path is clear** — the adapter interface above makes the upgrade path explicit and non-breaking

> In a real product sprint, live join would be a **Phase 2 feature** added after validating the core AI pipeline quality — which is exactly what this implementation does.

---

## Running Tests

```bash
# Unit tests (no API calls)
pytest tests/unit/ -v --cov=src

# Type checking
mypy src/ --ignore-missing-imports

# Linting
ruff check src/ tests/
```

---

