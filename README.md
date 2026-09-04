# BASE AI — Database Schema Generator

> Describe your product in plain English. Get a production-ready, 3-layer MySQL schema in seconds.

BASE AI is an AI-powered backend that transforms natural language business requirements into deeply structured, validated MySQL database schemas — complete with a PDF explanation of every design decision.

Built on **98 proprietary rules** across 24 categories, a **9-level validation pipeline**, and a conversational design session system that guides you from idea to schema.

---

## What it does

You send a prompt like:

> *"Build a database for a platform like Airbnb"*

BASE AI:
1. Detects the domain and matches relevant rules from the rule engine
2. Generates a **blueprint** (modules + tables) for you to review
3. After your approval, generates the full **production-ready SQL schema**
4. Validates the output against 98 rules and auto-fixes issues
5. Returns a downloadable **SQL file + PDF documentation**

---

## Architecture

```
User Prompt
    │
    ▼
┌─────────────────────────────────────┐
│  Conversation Engine                │  ← Guides user through stages
│  (clarification → blueprint → gen) │
└─────────────┬───────────────────────┘
              │
    ┌─────────▼──────────┐
    │   Rule Matcher      │  ← Vector search on 98 rules (Qdrant)
    │   + Domain Detect   │
    └─────────┬───────────┘
              │
    ┌─────────▼──────────┐
    │  Architecture       │  ← L1–L7 deep planning layers
    │  Planner (91KB)     │  ← Blueprint generation with fallback
    └─────────┬───────────┘
              │
    ┌─────────▼──────────┐
    │  Schema Generator   │  ← Batched AI calls (4 tables/call)
    │  (Multi-pass)       │  ← Multi-model fallback chain
    └─────────┬───────────┘
              │
    ┌─────────▼──────────┐
    │  Schema Validator   │  ← 7-dimension scoring (0–100)
    │  + Auto-fix         │  ← Auto-repairs rule violations
    └─────────┬───────────┘
              │
    ┌─────────▼──────────┐
    │  SQL + PDF Output   │  ← Downloadable files
    └────────────────────┘
```

### Tech Stack

| Layer | Technology |
|---|---|
| API Framework | FastAPI (Python 3.11) |
| AI Provider | Groq (`openai/gpt-oss-120b`) with multi-model fallback; local Ollama supported |
| Vector Search | Qdrant (rule retrieval) |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2) |
| Database | PostgreSQL (async via SQLAlchemy) |
| Session Store | Redis |
| Auth | JWT + API Key |
| PDF Generation | ReportLab |
| Rate Limiting | SlowAPI |
| Containerisation | Docker (multi-stage, non-root) |

---

## Rule Engine

The schema generator is powered by **98 proprietary rules** across 24 categories:

| Category | Rules | What it enforces |
|---|---|---|
| Architecture | 21 | 3-layer structure, module design |
| Convention | 10 | Naming standards (`_all` suffix, prefixes) |
| Workflow | 8 | State machines, lifecycle tables |
| Financial | 7 | GST, invoicing, amount precision |
| Constraints | 6 | Foreign keys, indexes, unique constraints |
| Domain | 6 | Industry-specific patterns |
| + 18 more | 40 | Identity, audit, configuration, operations, monitoring, temporal, security… |

---

## API Overview

### Conversational Flow (recommended)

```
POST /conversation/start          → Get session_id
POST /conversation/message        → Chat to refine requirements
                                    Returns blueprint when ready
POST /planner/generate            → Submit generation job (async)
GET  /planner/job/{job_id}        → Poll for result
GET  /conversation/download/sql   → Download SQL file
GET  /conversation/download/pdf   → Download PDF documentation
```

### Direct Generation

```
POST /planner/generate            → Async (returns job_id immediately)
GET  /planner/job/{job_id}        → Poll: queued → generating → done
POST /planner/generate-sync       → Synchronous (for testing only)
POST /planner/blueprint           → Generate blueprint only
POST /planner/match-rules         → See which rules apply to your prompt
```

### Auth

```
POST /auth/register               → Create account
POST /auth/login                  → Get JWT token
POST /auth/api-key                → Generate API key
```

### Other

```
GET  /health                      → Health check
GET  /ai/provider                 → Active AI provider info
GET  /rules                       → Browse all 98 rules
GET  /dashboard                   → Usage stats
GET  /docs                        → Swagger UI (debug mode only)
```

---

## Local Setup

### Prerequisites

- Python 3.11+
- PostgreSQL
- Redis
- [Qdrant](https://qdrant.tech/) (cloud or local)
- [Groq API Key](https://console.groq.com/) (free tier available)

### 1. Clone & install

```bash
git clone https://github.com/Yash-Satankar/base_ai_backend.git
cd base_ai_backend

python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual values
```

### 3. Seed the rule engine

```bash
python seed.py
```

This embeds all 98 rules into Qdrant for vector search. Run once on setup.

### 4. Start the server

```bash
uvicorn app.main:app --reload --port 8000
```

API is now live at `http://localhost:8000`
Swagger docs at `http://localhost:8000/docs`

---

## Docker Setup

### Single container

```bash
docker build -t base-ai .
docker run -p 8000:8000 --env-file .env base-ai
```

### Full stack (recommended)

```bash
docker-compose up --build
```

This starts the app + PostgreSQL + Redis together. Qdrant must be configured separately (use Qdrant Cloud free tier or add to compose).

---

## Example Usage

### Start a session

```bash
curl -X POST http://localhost:8000/conversation/start
# → { "session_id": "abc123", "stage": "gathering" }
```

### Send your requirement

```bash
curl -X POST http://localhost:8000/conversation/message \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "abc123",
    "message": "I want to build a platform like Airbnb for vacation rentals"
  }'
# → Returns clarifying questions or blueprint when ready
```

### Generate schema (async)

```bash
curl -X POST http://localhost:8000/planner/generate \
  -H "Content-Type: application/json" \
  -d '{
    "requirement": "Vacation rental platform like Airbnb",
    "blueprint": { ... }
  }'
# → { "job_id": "xyz789", "poll_url": "/planner/job/xyz789" }
```

### Poll for result

```bash
curl http://localhost:8000/planner/job/xyz789
# → { "status": "done", "result": { "schema_sql": "CREATE TABLE ..." } }
```

---

## Validation Scoring

Every generated schema is scored on 7 dimensions:

| Dimension | Weight | What's checked |
|---|---|---|
| Naming Convention | 20% | `_all` suffix, module prefixes, snake_case |
| Audit Fields | 20% | `created_at`, `updated_at`, `created_by` on every table |
| Financial Compliance | 15% | DECIMAL precision, GST fields, invoice structure |
| Data Preservation | 15% | Archive tables, soft delete, lifecycle tracking |
| Index & Constraints | 10% | Foreign keys, unique constraints, indexes |
| Status Convention | 10% | `tinyint(1) DEFAULT 2` pattern, status tables |
| Identity System | 10% | `unique_id_header_all` references |

A score below 60 triggers an automatic fix pass before the schema is returned.

---

## Security

- JWT authentication with configurable expiry
- API key authentication (database-backed)
- Rate limiting (200/day, 50/hour defaults)
- Input sanitisation with prompt injection detection
- SQL injection pattern blocking
- Non-root Docker user
- Docs hidden in production (`DEBUG=False`)

---

## Project Structure

```
base_ai_backend/
├── app/
│   ├── api/routes/          # FastAPI route handlers
│   ├── core/                # Config, security, logging, auth
│   ├── db/                  # Database, vector store, session store
│   ├── engine/              # Architecture planner, rule matcher, conversation engine
│   ├── prompts/             # System prompts (27KB of carefully crafted instructions)
│   ├── rules/               # rules.json (98 rules)
│   ├── schemas/             # Pydantic request/response models
│   ├── services/            # Business logic layer
│   └── validators/          # Schema validation engine
├── seed.py                  # One-time rule seeding script
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Built by

**Yash Satankar** — [yashsatankar.vercel.app](https://yashsatankar.vercel.app) · [LinkedIn](https://www.linkedin.com/in/yashsatankar)

---

## License

MIT License — see `LICENSE` for details.
