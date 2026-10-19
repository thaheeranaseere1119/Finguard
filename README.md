# FinGuard AI — Risk-Tiered Delegation for Financial Agents

**Act vs Ask vs Escalate (+ Verify, Block)** for recurring contractor payments. Contractors upload contracts. OCR and
extraction propose payment terms with source evidence. Humans validate and approve, and an approved contract becomes an
immutable version with a deterministic payment schedule. A durable scheduler then lets every due payment, amendment and
agent action pass a single, explainable risk engine before anything happens:

`Risk = 3S + 5F + 3I + 3E + 3T + 3U` → **ACT** · **APPROVE (ask)** · **ESCALATE** · **VERIFY** · **BLOCK**

The AI has no unrestricted financial authority. It proposes; deterministic server-side rules, risk evaluation and audited
human approvals decide. Payouts run against a **sandbox** provider by default. Live money movement is refused unless it is
explicitly enabled for production.

## Contents

| Path | What |
|---|---|
| `backend/finguard/` | FastAPI app: auth, RBAC/ABAC, contracts & OCR, extraction, governance/risk engine, scheduling, payments & providers, approvals, agent, audit, workers, evaluation |
| `backend/migrations/` | Alembic migrations (schema + append-only/immutability triggers) |
| `backend/tests/` | 173 unit, integration, API-authorization, workflow, payment-sandbox and recovery tests (PostgreSQL) |
| `frontend/` | React + TypeScript + Vite + Tailwind + TanStack Query + Recharts SPA with role-specific dashboards |
| `evaluation/` | Source datasets (FinanceBench, TAT-QA, 35-task calibration set), derived tasks, results, traces, plots, `report.md` |
| `backend/finguard/xyz/` | XYZ Ltd synthetic 10-year daily ledger, metric engine, trained question-understanding model and grounded financial Q&A ([docs](docs/xyz-financial-qa.md)) |
| `docs/` | [architecture](docs/architecture.md) · [risk model](docs/risk-model.md) · [scheduling & payments](docs/scheduling-and-payments.md) · [payment sandbox](docs/payment-sandbox.md) · [security](docs/security.md) · [API summary](docs/api.md) · [permissions matrix](docs/permissions.md) · [implemented vs incomplete](docs/features-status.md) |

## Quick start (local, no Docker) — the path verified while building

Prerequisites: Python 3.12+, Node 20+, PostgreSQL 14+, Redis 6+, Tesseract 5 with English data.

```bash
# 0. OCR engine (macOS): brew install tesseract   ·   Debian/Ubuntu: apt install tesseract-ocr tesseract-ocr-eng
# 1. Install dependencies
make install
# 2. Database role + dev/test databases (local PostgreSQL)
make db-create
# 3. Configuration
cp .env.example .env          # set JWT_SECRET; set TESSDATA_PREFIX if tesseract cannot find its data
# 4. Schema + roles/permissions
make migrate
# 5. Demo data (created through the real services; development only)
DEMO_PASSWORD='choose-a-strong-password' make seed
# 6. Run (three terminals)
make api          # http://localhost:8010  (OpenAPI docs: /api/docs)
make scheduler    # durable payment scheduler (or `make tick` for a single cycle)
make web          # http://localhost:5173  (proxies /api to :8010)
```

With `USE_JOB_QUEUE=true`, also run `make worker` (RQ) for OCR jobs. With `USE_JOB_QUEUE=false`, OCR runs inline after the
upload commits.

### Demo accounts (password = your `DEMO_PASSWORD`)

Organization **acme**: `admin@` · `analyst@` · `approver@` · `approver2@` · `manager@` · `risk@` · `auditor@` and contractors
`priya@ rahul@ ananya@ vikram@ meera@ karan@ sneha@ dev@` (all `…@acme.test`). Organization **globex** (tenant isolation):
`zoe@ analyst@ approver@globex.test`.

| Scenario (seeded) | What it shows |
|---|---|
| Priya — ₹20,000/month, auto-pay | ACT: past occurrences paid automatically; amendment ₹20k→₹25k with OCR-checked document |
| Rahul — **scanned PDF**, ₹1,50,000/month | OCR evidence with bbox highlighting; manager approval; payments wait for human approval |
| Ananya — invoice required | VERIFY holds until an invoice is accepted; first payment needs approval |
| Vikram — prompt injection in contract | ESCALATE to risk/compliance; the injected "pre-approval" has no effect |
| Meera — two conflicting monthly amounts | NEEDS_REVIEW with a CONFLICT field; submission blocked until resolved |
| Karan — sandbox 5555 | payout pending → signed webhook → PAID |
| Sneha — sandbox 4444 | provider timeout → UNKNOWN → reconciliation → PAID (no duplicate) |
| Dev — ended > 45 days ago | EXPIRED contract → open payments BLOCKED |

## XYZ Ltd financial Q&A

```bash
cd backend && DEMO_PASSWORD='choose-a-strong-password' .venv/bin/python -m finguard.xyz.cli setup && .venv/bin/python -m finguard.xyz.cli train
```

This loads an imaginary company's 10-year daily double-entry ledger (FY2017–FY2026, ~190k entries). It then trains the
question-understanding model and evaluates it. Sign in to organization `xyz` and open **Company financials**, or ask the
AI agent. Answers are exact ledger computations with evidence, and other companies are refused. See
[docs/xyz-financial-qa.md](docs/xyz-financial-qa.md).

## Docker Compose

```bash
cp .env.example .env    # set POSTGRES_PASSWORD, JWT_SECRET, MOCK_WEBHOOK_SECRET (+ COOKIE_SECURE=true behind HTTPS)
docker compose up --build
docker compose exec -e DEMO_PASSWORD='...' api python -m finguard.cli seed-demo   # optional demo data
```

The stack runs PostgreSQL, Redis, a one-shot `migrate` (Alembic + role bootstrap), `api`, `worker` (RQ), `scheduler` and
`web` (nginx with the SPA and an `/api` reverse proxy) on **http://localhost:8080**. Documents live in a private volume
mounted only into backend containers. *Note: Docker was not available on the machine this was built on, so the
Compose path is written but was not executed there; the local path above was run end to end.*

## Tests

```bash
cd backend && .venv/bin/pytest            # needs local PostgreSQL db finguard_test (TEST_DATABASE_URL to override) + Tesseract
cd frontend && npx tsc -b && npm run build
```

Tests use real PostgreSQL (triggers, partial unique indexes, row locks are part of what is tested) and deterministic mocks
for the LLM and payout providers. No credentials are needed. The 20 required scenarios map to
`tests/integration/test_contract_workflow.py` (1–9, 18–19), `test_payments.py` (10–14, 20), `test_amendments.py` (15–16),
`test_api_security.py` (17 and auth), and `test_agent_and_integrity.py` (agent injection, append-only audit, live-mode
refusal).

## OCR setup

* Native PDFs use the embedded text layer (PyMuPDF). Pages with fewer than 40 characters are rendered at 300 dpi and
  OCR'd with Tesseract; PNG/JPEG are OCR'd directly; DOCX goes through python-docx.
* If Tesseract cannot find its language data, set `TESSDATA_PREFIX` to the directory containing `eng.traineddata`
  (e.g. `/opt/homebrew/share/tessdata`, `/usr/share/tesseract-ocr/5/tessdata`, or Anaconda's `share/tessdata`).
* Try it: upload any scanned contract in **Submit contract**, or run `python -m finguard.evaluation.run_all` to measure
  extraction accuracy on generated native and scanned documents.

## Payment sandbox

The default `PAYOUT_PROVIDER=mock` needs no credentials and never moves money. Beneficiary account suffixes select
scenarios (0000 success, 2222 insufficient funds, 4444 timeout-after-create, 5555 pending→success, …). See
[docs/payment-sandbox.md](docs/payment-sandbox.md) for the full table and the steps to connect a real **RazorpayX
Payouts** test account (adapter implemented, not tested against a live sandbox).

## AI provider

`LLM_PROVIDER=mock` (default) uses a deterministic planner/answerer and proposes nothing for extraction; the rule-based
extractor stands alone. Set `LLM_PROVIDER=gemini` with `GEMINI_API_KEY` (or `openai` / `anthropic`) for model proposals.
These are accepted only if they pass deterministic parsing and their quoted source text exists in the document. LLM output
never authorizes a payment.

## Research evaluation

```bash
cd backend && .venv/bin/python -m finguard.evaluation.run_all --record
```

This builds 1,625 derived tasks (dev 418 / calibration 480 / held-out test 727) from FinanceBench (CC-BY-NC-4.0), TAT-QA
(CC-BY-4.0) and synthetic finance-operations families. It compares **no oversight**, **always approve** and
**risk-tiered** delegation, calibrates thresholds on the calibration split only, and runs ablations and threshold sweeps.
Outputs land in `evaluation/results/` (`report.md`, JSON metrics, JSONL traces, PNG/SVG plots). Human review is
**simulated** and labels follow a written rubric; read the report's limitations before citing numbers.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Tesseract couldn't load any languages` | set `TESSDATA_PREFIX` (see OCR setup) |
| Upload stays in OCR processing | run `make worker`, or set `USE_JOB_QUEUE=false`; the scheduler also re-runs stuck jobs after 10 minutes |
| Payments never execute | run `make scheduler` / `make tick`; check that the contract is ACTIVE, the beneficiary is verified, the payment is due, and the payment detail's governance decision |
| 401 after a restart in development | without `JWT_SECRET` a random per-process secret is used; set it in `.env` |
| Port 8000 busy | the dev API uses port 8010 (`API_TARGET` overrides the Vite proxy target) |
| `refusing to seed demo data in production` | intended; the seed is development-only |

## Production deployment notes

Set `ENVIRONMENT=production`, a ≥32-character `JWT_SECRET`, `COOKIE_SECURE=true`, TLS in front of nginx (enable HSTS),
strong database credentials, and secrets from a secret manager. Run migrations as a separate step. Keep
`PAYOUT_MODE=sandbox` unless you deliberately operate live payouts (this also requires the acknowledgement variable and a
live provider key). Back up PostgreSQL and the documents volume together ([docs/security.md](docs/security.md)). This
prototype is not certified for any compliance framework.
# Risk-Delegation-System
