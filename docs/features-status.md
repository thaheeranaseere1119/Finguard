# Implemented vs. incomplete / credential-dependent

## Implemented and tested (automated tests against PostgreSQL)

* Authentication (Argon2id, JWT, rotating refresh tokens with reuse detection, lockout), contractor self-registration,
  sessions page and revocation.
* RBAC (9 roles incl. 2 machine identities) + ABAC (tenant isolation, ownership, SoD, approval limits, dual approval).
* Contract upload with magic-byte validation, private storage, SHA-256 duplicate detection and integrity re-check.
* Text extraction: native PDF (PyMuPDF), scanned PDF/PNG/JPEG OCR (Tesseract with word confidences and bounding boxes),
  DOCX. Rule-based extraction with page, text and bbox provenance for all schema fields; conflict and ambiguity
  detection; injection detection; immutable extraction records; append-only corrections.
* Contract lifecycle state machine (DRAFT … ACTIVE, plus exception states), validation checks, approval routing by
  commitment, escalation to compliance, activation with deterministic schedule generation.
* Immutable contract versions; amendments with document OCR comparison, separate risk decision, period-aligned effective
  dates, regeneration of future occurrences only, and rejection leaving terms unchanged.
* Recurring payment engine: durable DB-backed scheduler (SKIP LOCKED claims, leases, crash recovery), governance per
  occurrence, ACT/APPROVE/ESCALATE/VERIFY/BLOCK routing, invoice holds, beneficiary verification, cumulative caps,
  idempotent payouts, reconciliation of unknown outcomes, retries after confirmed failures.
* Mock payout sandbox with 10 scenarios, signed webhooks, duplicate-delivery and invalid-signature handling.
* Six-factor risk engine (`3S+5F+3I+3E+3T+3U`), configurable versioned policies, deterministic hard-rule overrides,
  structured decision records (factors, rationales, evidence refs, policy version, rules, next actions).
* AI agent: machine identity, 9 allowlisted tools, delegated authorization, deterministic planner/answerer (mock LLM),
  evidence citations, mandatory-evidence VERIFY, injection resistance, audited tool calls.
* Approval queues and escalation management with mandatory reasons; notifications.
* Hash-chained append-only audit log with verification and CSV export.
* Role-specific React dashboards (contractor, analyst, approver/manager, risk officer, auditor, admin), contract wizard,
  evidence viewer with OCR bbox highlighting, approvals workspace with amendment comparison, payments/reconciliation,
  agent chat, audit explorer, risk analytics, research page, admin (users/roles matrix, policies, system health).
* Evaluation harness: derived task set (FinanceBench, TAT-QA, synthetic families), three-policy comparison, calibration
  on the calibration split, ablations, reviewer-error sensitivity, threshold sweeps and Pareto plots, OCR accuracy, JSONL
  traces, recorded evaluation runs.

## Implemented but not verified in this environment

* **Docker Compose stack**: Dockerfiles, nginx and compose are written, but Docker is not installed on the build machine,
  so `docker compose up` was **not executed**. The local (non-Docker) path was run end to end.
* **RazorpayX adapter**: written against the official API reference; not exercised against a real sandbox (no account
  or keys). See `payment-sandbox.md` for the remaining setup.
* **Hosted LLM providers** (Gemini/OpenAI/Anthropic): adapters implemented; all tests and evaluations ran with the
  deterministic mock. With a key, extraction proposals are accepted only when they pass the deterministic parser and the
  quoted source text is found in the document.

## Credential / account dependent

| Item | Needed |
|---|---|
| RazorpayX Payouts sandbox | RazorpayX account with Payouts, test keys, test account number, IP allowlist, webhook secret |
| Gemini (or other) LLM | `GEMINI_API_KEY` (or OpenAI/Anthropic key) + `LLM_PROVIDER` |
| Optional cloud OCR | Not implemented (adapter point: `documents/text_extraction.extract`) — Tesseract is used |

## Known limitations

* Evaluation human review is simulated, and synthetic scenario families partly mirror the hard rules (see
  `evaluation/results/report.md`, Limitations).
* Rule-based extraction is tuned for English, contract-style language and Indian/US/EU currency notation. Unusual
  layouts (multi-column, tables spanning pages, handwritten values) will need the LLM path or manual correction.
  Names misread by OCR at high confidence are caught by the validation party-identity warning, not by confidence.
* Signature detection is textual (signature blocks), not visual.
* Holiday calendars are a per-organization list (no external holiday feed).
* No bulk upload, e-signature integration, multi-currency FX conversion of payouts (FX rates are used only for
  limits and impact bands), or email/SMS notification delivery (in-app only).
* pgvector semantic retrieval is not used; evidence search uses PostgreSQL full-text search.
