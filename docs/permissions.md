# Permissions matrix

Server-enforced RBAC (generated from `finguard/authz/permissions.py`). Deny by default.
Resource-level ABAC applies on top: organization isolation (cross-tenant ⇒ 404), contractor ownership, separation of duties
(no self-approval; validator ≠ approver; escalation clearer ≠ financial approver; one decision per person per request),
amount-based approval limits and dual approval (policy), and contract status/date checks. `AI_AGENT` and `SCHEDULER` are
machine identities; the agent's effective rights are the intersection of its own and the delegating user's.

| Permission | Contractor | Finance Analyst | Finance Approver | Risk Officer | Finance Manager | Auditor | System Admin | Ai Agent | Scheduler |
|---|---|---|---|---|---|---|---|---|---|
| `contracts:create` — Create a contract draft and upload documents (own) | ✔ |  |  |  |  |  |  |  |  |
| `contracts:read_own` — Read own contracts, extraction and payment status | ✔ |  |  |  |  |  |  |  |  |
| `contracts:read_org` — Read all contracts in the organization |  | ✔ | ✔ | ✔ | ✔ | ✔ |  |  |  |
| `contracts:correct_fields_own` — Correct extracted fields on own draft contracts | ✔ |  |  |  |  |  |  |  |  |
| `contracts:correct_fields` — Correct extracted fields during validation |  | ✔ |  |  |  |  |  |  |  |
| `contracts:submit_own` — Submit own contract for validation | ✔ |  |  |  |  |  |  |  |  |
| `contracts:validate` — Perform contract validation |  | ✔ |  |  | ✔ |  |  |  |  |
| `contracts:approve` — Approve or reject contracts within approval limits |  |  | ✔ |  | ✔ |  |  |  |  |
| `contracts:suspend` — Suspend / terminate contracts (hold) |  |  |  | ✔ | ✔ |  |  |  |  |
| `amendments:request_own` — Request an amendment on own active contract | ✔ |  |  |  |  |  |  |  |  |
| `amendments:approve` — Approve or reject amendments within approval limits |  |  | ✔ |  | ✔ |  |  |  |  |
| `payments:read_own` — Read own scheduled payments | ✔ |  |  |  |  |  |  |  |  |
| `payments:read_org` — Read organization payments |  | ✔ | ✔ | ✔ | ✔ | ✔ |  |  |  |
| `payments:prepare` — Prepare payment requests and reconcile |  | ✔ |  |  |  |  |  |  |  |
| `payments:approve` — Approve held payments within approval limits |  |  | ✔ |  | ✔ |  |  |  |  |
| `payments:execute_scheduled` — Execute due scheduled payments via governance (machine) |  |  |  |  |  |  |  |  | ✔ |
| `payments:reconcile` — Reconcile unknown provider outcomes |  | ✔ |  |  |  |  |  |  | ✔ |
| `payments:hold` — Place or release compliance holds |  |  |  | ✔ |  |  |  |  |  |
| `invoices:submit_own` — Submit invoices for own scheduled payments | ✔ |  |  |  |  |  |  |  |  |
| `invoices:review` — Accept or reject invoices |  | ✔ |  |  |  |  |  |  |  |
| `beneficiaries:manage_own` — Register own payout beneficiary | ✔ |  |  |  |  |  |  |  |  |
| `beneficiaries:verify` — Verify beneficiaries |  | ✔ |  |  |  |  |  |  |  |
| `approvals:read` — View approval queues |  | ✔ | ✔ | ✔ | ✔ | ✔ |  |  |  |
| `escalations:resolve` — Resolve escalated cases |  |  |  | ✔ | ✔ |  |  |  |  |
| `risk:read` — Read risk assessments and governance decisions |  | ✔ | ✔ | ✔ | ✔ | ✔ |  |  |  |
| `audit:read` — Read audit events |  |  |  | ✔ |  | ✔ |  |  |  |
| `audit:export` — Export audit reports |  |  |  |  |  | ✔ |  |  |  |
| `policies:read` — Read risk/governance policy versions |  |  |  | ✔ | ✔ | ✔ | ✔ |  |  |
| `policies:manage` — Create and activate policy versions |  |  |  |  |  |  | ✔ |  |  |
| `approval_limits:manage` — Configure approval limits |  |  |  |  | ✔ |  |  |  |  |
| `users:manage` — Manage users and role assignments |  |  |  |  |  |  | ✔ |  |  |
| `system:read` — View system health and integration status |  |  |  |  |  |  | ✔ |  |  |
| `agent:use` — Use the AI financial agent | ✔ | ✔ | ✔ | ✔ | ✔ |  |  |  |  |
| `agent:tools` — Invoke allowlisted agent tools (machine identity) |  |  |  |  |  |  |  | ✔ |  |
| `evaluation:read` — View research evaluation runs |  |  |  | ✔ | ✔ | ✔ | ✔ |  |  |
| `evaluation:run` — Run evaluations |  |  |  |  |  |  | ✔ |  |  |
| `reports:read` — View operational reports |  | ✔ | ✔ | ✔ | ✔ | ✔ |  |  |  |

## Roles

* **CONTRACTOR** — External contractor: submits own contracts, invoices and amendment requests
* **FINANCE_ANALYST** — Validates contracts, prepares and reconciles payments
* **FINANCE_APPROVER** — Approves contracts, amendments and payments within limits
* **RISK_OFFICER** — Reviews escalations, holds, policy violations and audit history
* **FINANCE_MANAGER** — Higher-limit approvals, escalation resolution, approval-limit configuration
* **AUDITOR** — Read-only access to audit, versions, approvals and risk
* **SYSTEM_ADMIN** — Technical administration. No financial approval authority
* **AI_AGENT** — Machine identity for the AI agent (allowlisted tools only)
* **SCHEDULER** — Machine identity for the recurring-payment scheduler
