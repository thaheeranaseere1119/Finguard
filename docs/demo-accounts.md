# Demo accounts (local development only)

All seeded accounts share one password: the `DEMO_PASSWORD` value in your git-ignored `.env`
(the value used when `python -m finguard.cli seed-demo` was run). Sign in at http://localhost:5173 with the
**organization code**, email and that password.

## Organization `acme` — Acme Analytics Pvt Ltd

| Role | Email | What you can do |
|---|---|---|
| System Admin | admin@acme.test | Users & roles, policies, system health (no financial approvals) |
| Finance Analyst | analyst@acme.test | Validate contracts, verify payout accounts, review invoices, reconcile, re-run payments |
| Finance Approver | approver@acme.test | Approve contracts / amendments / payments up to ₹2,00,000 |
| Finance Approver (2nd) | approver2@acme.test | Second approver for dual-approval payments |
| Finance Manager | manager@acme.test | Higher-limit approvals (≤ ₹20,00,000), escalations, holds |
| Risk Officer | risk@acme.test | Escalation queue, holds/suspensions, risk analytics, audit |
| Auditor | auditor@acme.test | Read-only audit explorer, export, research results |
| Contractor | priya@acme.test | ₹20k auto-paid contract; approved ₹25k amendment |
| Contractor | rahul@acme.test | Scanned (OCR) ₹1.5L contract; payments awaiting approval |
| Contractor | ananya@acme.test | Invoice-required contract (VERIFY holds) |
| Contractor | vikram@acme.test | Prompt-injection contract (escalated) |
| Contractor | meera@acme.test | Conflicting amounts — needs field review |
| Contractor | karan@acme.test | Sandbox pending → webhook payout |
| Contractor | sneha@acme.test | Sandbox timeout → reconciled payout |
| Contractor | dev@acme.test | Expired contract (blocked payments) |

## Organization `xyz` — XYZ Ltd (imaginary company with a 10-year financial ledger)

| Role | Email |
|---|---|
| System Admin | admin@xyz.test |
| Finance Analyst | analyst@xyz.test |
| Finance Approver | approver@xyz.test |
| Finance Manager | manager@xyz.test |
| Risk Officer | risk@xyz.test |
| Auditor | auditor@xyz.test |
| Contractor (₹85,000/month contract; September 2026 invoice ready to claim; no access to company financials) | contractor@xyz.test |

Open **Company financials** or ask the **AI agent** about XYZ Ltd's revenue, profit, cash, customers etc. (FY2017–FY2026).

## Organization `globex` — Globex Consulting LLP (tenant-isolation demo)

| Role | Email |
|---|---|
| System Admin | admin@globex.test |
| Finance Analyst | analyst@globex.test |
| Finance Approver | approver@globex.test |
| Contractor | zoe@globex.test |

`AI_AGENT` and `SCHEDULER` are machine identities (`agent@` / `scheduler@service.finguard.internal`) and cannot sign in.
New contractors can self-register from the login page with organization code `acme`.
