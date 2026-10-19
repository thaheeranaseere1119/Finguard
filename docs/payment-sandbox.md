# Payment sandbox

Contractor payments are **outbound payouts**. They are not inbound checkout/collections; a Razorpay Payment Gateway
checkout integration would not prove that contractor payouts work, so none is used here.

## What is implemented

| Component | Status |
|---|---|
| Provider interface (`create_payout`, `get_payout_status`, `find_by_reference`, `cancel_payout`, `verify_webhook`, `parse_webhook`, `register_beneficiary`) | Implemented |
| `MockSandboxPaymentProvider` (deterministic, no credentials, no money) | Implemented and tested end-to-end |
| `RazorpayXProvider` adapter | Implemented against the official API reference. **Not tested against a real RazorpayX sandbox account** (no credentials were available) |
| Webhook endpoint `POST /api/v1/webhooks/payouts/{provider}` | HMAC-SHA256 over the raw body, dedupe by `x-razorpay-event-id`, amount/currency/reference validation |
| Startup safety | `PAYOUT_MODE=live` refused unless `ENVIRONMENT=production` and `LIVE_PAYOUTS_ACKNOWLEDGEMENT=I_UNDERSTAND_REAL_MONEY_WILL_MOVE`; RazorpayX refuses a `rzp_live_` key in sandbox mode |

## Mock sandbox scenarios

Register a payout account whose **last four digits** select the scenario:

| Suffix | Scenario | Expected platform result |
|---|---|---|
| 0000 (or other) | success | occurrence PAID |
| 1111 | provider rejects request | FAILED (retry allowed after fixing) |
| 2222 | insufficient funds | FAILED; retry re-runs governance (higher uncertainty → human approval) |
| 3333 | invalid beneficiary | FAILED |
| 4444 | timeout **after** the provider created the payout | UNKNOWN → reconciliation finds it → PAID (no duplicate) |
| 5555 | pending → processed | SUBMITTED → signed webhook → PAID |
| 6666 | pending → failed | SUBMITTED → signed webhook → FAILED |
| 7777 | network error before sending | FAILED (nothing created) |
| 8888 | timeout, payout never created | UNKNOWN → reconciliation finds nothing → FAILED (safe retry) |
| 9999 | processed then reversed | PAID → webhook → FAILED (reversed) |

Duplicate webhook delivery returns `DUPLICATE`; invalid signatures return `REJECTED` and are audited. A duplicate
execution attempt is stopped twice: governance (`PAY_NOT_DUPLICATE`), then the database partial unique index.

The mock provider stores its own state in `sandbox_payouts` (in an independent transaction, like an external system) and
signs webhooks with `MOCK_WEBHOOK_SECRET` using the same scheme as RazorpayX. So the real verification path is exercised.
`python -m finguard.worker.main tick` settles pending sandbox payouts and delivers their webhooks.

## Try it locally

```bash
cd backend
DEMO_PASSWORD='choose-a-strong-password' .venv/bin/python -m finguard.cli seed-demo   # creates contracts incl. 4444 and 5555 cases
.venv/bin/python -m finguard.worker.main tick                                     # reconciles 4444, settles 5555 via webhook
```

In the UI: **Payments → Held / failed**, **Reconciliation**, and each payment's attempts and provider events.

## Connecting the official RazorpayX sandbox (remaining external setup)

1. Get a RazorpayX account with **Payouts** enabled. Ordinary Payment Gateway test keys do not enable payouts.
2. In **Test mode**, generate API keys (`rzp_test_…`) and note the **test business account number** (customer identifier).
3. Allowlist the server's egress IP in the RazorpayX dashboard (required for payouts).
4. Create a webhook to `https://<your-host>/api/v1/webhooks/payouts/razorpayx` for `payout.*` events, with a secret.
5. Set in `.env`:
   ```
   PAYOUT_PROVIDER=razorpayx
   PAYOUT_MODE=sandbox
   RAZORPAYX_KEY_ID=rzp_test_...
   RAZORPAYX_KEY_SECRET=...
   RAZORPAYX_ACCOUNT_NUMBER=...
   RAZORPAYX_WEBHOOK_SECRET=...
   ```
6. Register beneficiaries again: the adapter creates a Contact and a bank-account Fund Account and stores only the `fa_…` id
   and the last four digits.
7. Verify manually: success, failure, pending → webhook, duplicate webhook, invalid signature, timeout → reconcile
   (`GET /v1/payouts?reference_id=`). Record which of these you tested.

Endpoints used: `POST /v1/contacts`, `POST /v1/fund_accounts`, `POST /v1/payouts` (header `X-Payout-Idempotency`, amount in
paise, `mode` IMPS, `purpose` "vendor bill", `queue_if_low_balance=false`), `GET /v1/payouts/{id}`,
`GET /v1/payouts?account_number=&reference_id=`, `POST /v1/payouts/{id}/cancel` (queued payouts only; this endpoint was
not confirmed on the fetched reference page). Status mapping: queued/pending/processing → PENDING; processed → PROCESSED;
failed/rejected → FAILED; cancelled; reversed.

**Tested against a real provider:** none. **Simulated:** all scenarios above.

## Dwolla sandbox (US ACH, USD) — `PAYOUT_PROVIDER=dwolla`

Adapter: `backend/finguard/payments/providers/dwolla.py`. It calls `https://api-sandbox.dwolla.com` and refuses any other URL while `PAYOUT_MODE=sandbox`.

- **Credentials.** Put `DWOLLA_KEY` and `DWOLLA_SECRET` (a sandbox application key) in `.env`. Optional: `DWOLLA_FUNDING_SOURCE` picks which account pays out (default: the account's verified bank). `DWOLLA_WEBHOOK_SECRET` is needed only for webhooks.
- **Currency.** USD only. Contracts in INR are refused with `unsupported_currency`. Payees need a US bank account and a 9-digit ABA routing number, entered in the IFSC field. The sandbox test routing number is `222222226`.
- **Flow.** The contractor's payout account becomes a Dwolla receive-only customer with a funding source. Each payment attempt is a `POST /transfers` with an `Idempotency-Key` and a `correlationId` (our reference). Transfer status, failure codes and reconciliation by `correlationId` are read back from Dwolla.
- **Webhooks.** Each event is checked against its `X-Request-Signature-SHA-256` header. The event only links to the transfer, so FinGuard re-reads the transfer from Dwolla before applying anything. Dwolla can only send webhooks to a public HTTPS URL: `POST /api/v1/webhooks/payouts/dwolla`.
- **Sandbox behaviour.** Transfers stay `pending` until `POST /sandbox-simulations` runs. A payee bank account ending in **1111** or **3333** is named `R01` or `R03`, which makes its transfers fail with that ACH return code.
- **Idempotency quirk, verified in the sandbox.** The same key with an identical body returns the same transfer. The same key with a *different* body creates a new transfer. `execute_payout` always rebuilds the identical request from the stored attempt.
- **Live check (opt-in).** `RUN_DWOLLA_SANDBOX=1 pytest tests/integration/test_dwolla_sandbox.py`. It covers an end-to-end payout, the idempotent replay, an R01 return, an invoice auto-payment, the signed webhook, refusal of INR, and refusal of bad credentials.
