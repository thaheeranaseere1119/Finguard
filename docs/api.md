# API endpoint summary

Generated from the live OpenAPI schema (`/api/openapi.json`; interactive docs at `/api/docs`).
All endpoints except login/register/refresh, `/api/health` and provider webhooks require `Authorization: Bearer <access token>`.
Errors use `{"error": {"code", "message", "details?", "request_id"}}`. Every response carries `X-Request-ID`.

## Auth

| Method | Path | Summary |
|---|---|---|
| POST | `/api/v1/auth/login` | Login |
| POST | `/api/v1/auth/logout` | Logout |
| GET | `/api/v1/auth/me` | Me |
| POST | `/api/v1/auth/refresh` | Refresh |
| POST | `/api/v1/auth/register` | Register |
| GET | `/api/v1/auth/sessions` | Sessions |
| DELETE | `/api/v1/auth/sessions/{session_id}` | Revoke Session |

## Contracts

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/amendments/{amendment_id}` | Get Amendment |
| POST | `/api/v1/amendments/{amendment_id}/document` | Amendment Document |
| POST | `/api/v1/amendments/{amendment_id}/withdraw` | Withdraw Amendment |
| POST | `/api/v1/beneficiaries/{beneficiary_id}/verify` | Verify Beneficiary |
| POST | `/api/v1/contracts` | Upload Contract |
| GET | `/api/v1/contracts` | List Contracts |
| GET | `/api/v1/contracts/{contract_id}` | Get Contract |
| POST | `/api/v1/contracts/{contract_id}/amendments` | Request Amendment |
| GET | `/api/v1/contracts/{contract_id}/amendments` | List Amendments |
| POST | `/api/v1/contracts/{contract_id}/beneficiary` | Add Beneficiary |
| GET | `/api/v1/contracts/{contract_id}/extraction` | Get Extraction |
| POST | `/api/v1/contracts/{contract_id}/fields` | Correct Field |
| POST | `/api/v1/contracts/{contract_id}/hold` | Hold |
| POST | `/api/v1/contracts/{contract_id}/retry-ocr` | Retry Ocr |
| GET | `/api/v1/contracts/{contract_id}/schedule` | Schedule |
| POST | `/api/v1/contracts/{contract_id}/submit` | Submit |
| POST | `/api/v1/contracts/{contract_id}/validate` | Validate |
| GET | `/api/v1/contracts/{contract_id}/validation-checks` | Validation Preview |
| GET | `/api/v1/contracts/{contract_id}/versions` | Versions |
| GET | `/api/v1/documents/{document_id}/download` | Document Download |
| GET | `/api/v1/documents/{document_id}/pages/{page}` | Document Page |

## Payments

| Method | Path | Summary |
|---|---|---|
| POST | `/api/v1/invoices/{invoice_id}/review` | Review Invoice |
| GET | `/api/v1/payments` | List Payments |
| GET | `/api/v1/payments/{payment_id}` | Get Payment |
| POST | `/api/v1/payments/{payment_id}/invoices` | Submit Invoice |
| POST | `/api/v1/payments/{payment_id}/reconcile` | Reconcile |
| POST | `/api/v1/payments/{payment_id}/retry` | Retry |
| GET | `/api/v1/reconciliation` | Reconciliation |
| POST | `/api/v1/webhooks/payouts/{provider}` | Payout Webhook |

## Governance

| Method | Path | Summary |
|---|---|---|
| POST | `/api/v1/agent/chat` | Agent Chat |
| GET | `/api/v1/agent/runs` | Agent Runs |
| GET | `/api/v1/analytics/ocr` | Ocr Analytics |
| GET | `/api/v1/analytics/overview` | Overview |
| GET | `/api/v1/analytics/risk` | Risk Analytics |
| GET | `/api/v1/analytics/workload` | Workload |
| GET | `/api/v1/approvals` | List Approvals |
| GET | `/api/v1/approvals/{request_id}` | Get Approval |
| POST | `/api/v1/approvals/{request_id}/decision` | Decide |
| GET | `/api/v1/audit` | Audit |
| GET | `/api/v1/audit/export` | Audit Export |
| GET | `/api/v1/audit/verify` | Audit Verify |
| GET | `/api/v1/decisions` | List Decisions |
| GET | `/api/v1/decisions/{decision_id}` | Get Decision |
| GET | `/api/v1/notifications` | Notifications |
| POST | `/api/v1/notifications/read` | Mark Read |

## Admin

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/evaluation/runs` | Evaluation Runs |
| GET | `/api/v1/policies` | Policies |
| POST | `/api/v1/policies` | New Policy |
| POST | `/api/v1/policies/{policy_id}/activate` | Activate |
| GET | `/api/v1/roles` | Roles |
| GET | `/api/v1/system/health` | System Health |
| POST | `/api/v1/system/scheduler/tick` | Run Tick |
| GET | `/api/v1/users` | List Users |
| POST | `/api/v1/users` | Create User |
| PUT | `/api/v1/users/{user_id}/active` | Set Active |
| PUT | `/api/v1/users/{user_id}/roles` | Set Roles |

## Health

| Method | Path | Summary |
|---|---|---|
| GET | `/api/health` | Health |
