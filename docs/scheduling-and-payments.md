# Recurring schedules, payment execution and amendments

## Schedule generation (`finguard/scheduling/generator.py`)

* **Service periods** are anchored at the contract start: `P_k = [A_k, A_{k+1} − 1 day]` with `A_k = start + k·step`.
  Each anchor is computed from the original start, never from the previous date, so month ends do not drift
  (Jan 31 → Feb 28/29 → Mar 31). "Monthly" means calendar months, not 30 days. Quarterly = 3 months, annual = 12 months,
  CUSTOM = n DAYS/WEEKS/MONTHS.
* **Nominal payment date**: ADVANCE pays on the first rule date on/after the period start; ARREARS pays on the first rule
  date after the period end. The rule is a day-of-month (clamped to month length; 31 = last day) or a weekday. With no
  rule, ADVANCE pays at period start and ARREARS at period end. An explicit first payment date overrides occurrence 1.
* **Business days**: weekends plus organization holidays (`organizations.settings.holidays`); rule NONE / FOLLOWING /
  PRECEDING / MODIFIED_FOLLOWING (default FOLLOWING, configurable per organization).
* **Execution instant**: `due_date` at the organization's execution hour in the **contract's time zone**, stored in UTC.
  DST is handled by `zoneinfo`; a non-existent local time is normalized deterministically.
* **Proration**: a last period truncated by the end date is prorated by actual/full days when the contract says pro rata
  (DAILY). Otherwise the full amount applies. Amounts use Decimal with ROUND_HALF_UP to minor units.
* **Total value**: validation flags a schedule whose total exceeds the stated total contract value. At payment time,
  `PAY_CUMULATIVE_CAP` escalates any payment that would exceed it.

## Due payment processing (`finguard/services/payments.py`)

1. `claim_due` locks due SCHEDULED / APPROVED_FOR_EXECUTION rows (`FOR UPDATE SKIP LOCKED`) → PROCESSING, commit.
2. For each occurrence: reload the contract and the approved version effective for the period, and re-derive the expected
   amount from the approved terms.
3. Build a `PaymentSnapshot`. It covers status/dates, due check, duplicates, unresolved attempts, amount/currency match,
   cumulative cap, invoice, beneficiary verification, pending amendment, auto-execution permission, document integrity
   (hash re-check), extraction confidence and actor authority.
4. Assess, then route: **ACT** → payout · **APPROVE** → approval request (approver role and limits, dual approval above the
   threshold) · **ESCALATE** → risk/compliance request · **VERIFY** → HELD with notifications (e.g. invoice request) ·
   **BLOCK** → BLOCKED if the failure is terminal (expired, outside dates, mismatch), otherwise unchanged (e.g. not yet due).
5. Payout: create a CREATED attempt with a unique idempotency key `…-a{n}` and **commit before calling the provider**.
   * success → PAID; pending → SUBMITTED (settled by webhook or reconciliation)
   * confirmed failure → FAILED (a new attempt with a new key may be made after re-running governance)
   * timeout / interrupted → UNKNOWN: **no retry** until `reconcile_attempt` asks the provider (by payout id or reference)
6. Recovery after a crash/restart: stale PROCESSING claims return to SCHEDULED, and orphaned CREATED attempts become
   UNKNOWN and are reconciled. No blind retries happen.

Expired contracts: the expiry job marks a contract EXPIRED 45 days after its end date (a settlement window for arrears
payments of the final period). Any open payment on an EXPIRED contract is BLOCKED.

## Amendments (`finguard/services/amendments.py`)

1. The contractor requests changes (e.g. `payment_amount: 25000`) with a reason, an effective date and optionally a signed
   document. Only allowlisted fields can be amended.
2. The effective date is aligned to the next unpaid period start, and a PROPOSED version is created. Approved terms are
   untouched.
3. The amendment document is OCR'd and compared with the proposed values. An increase above 10% without a document
   triggers VERIFY; a mismatch, currency change or injected text triggers ESCALATE; retroactive changes to paid or
   in-flight periods are BLOCKED.
4. While pending, payments in affected periods are HELD (`PAY_NO_PENDING_AMENDMENT`).
5. Approval by an authorized approver who is not the requester or contractor:
   * the new version becomes APPROVED with `version_no + 1` and `effective_from`; the old one becomes SUPERSEDED
   * only not-yet-executed occurrences from the effective date are SUPERSEDED and regenerated
   * paid history is never modified
6. Rejection: the proposed version is REJECTED, original terms remain, and held payments are re-queued.

## Contractor claim tiers (invoice-required contracts)

When a contractor claims a month (with the generated invoice or their own uploaded PDF or photo), the invoice is validated automatically and **never** goes to an analyst queue. The risk engine then routes the claim:

| Scenario | Condition | Decision |
|---|---|---|
| 1 | Valid invoice, claim **≤ contract amount** | **ACT**: paid automatically for the claimed amount. The claim is pre-authorized by the human contract approval, so a medium risk score doesn't force another approval |
| 2 | Valid invoice, **contract < claim ≤ contract + 10%** | **APPROVE**: approver, or manager by approval limit; two approvers above ₹5 lakh. The approver approves that exact amount |
| 3 | Invoice **failed validation**, **payout account (payee) changed**, or claim **> +10%** | **ESCALATE**: risk officer. *Clear* re-assesses the claim (≤ contract → paid; above → approver). *Reject* rejects the invoice and returns the month to the contractor for a corrected invoice |

- **Always applied:** hard rules (expired or suspended contract, duplicates, unverified payout account, pending amendment, total-value cap) and the `escalate_min` score.
- **Policy settings:** the tolerance is `invoice_tiers.approve_tolerance_pct` (default 10); tiers can be switched off with `invoice_tiers.enabled`.
- **Approval binding:** every approval and escalation clearance records the amount and payee (`approval_requests.amount` and `payee_ref`). It counts only while the payment still has exactly that amount and payout account. Otherwise a new approval is opened and the old one is cancelled or ignored.
- **Invoice limits:** one active invoice per payment, and an analyst can confirm or reject an invoice but can't change the payout amount.

Tests: `tests/integration/test_claim_tiers.py`.
