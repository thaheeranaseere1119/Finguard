# Risk model and decision policy

`Risk = 3·S + 5·F + 3·I + 3·E + 3·T + 3·U` (each factor 0–5, max 100). These are prototype research weights with
financial impact weighted higher; if a policy changes them, the score is renormalized to 0–100.

## Factor scoring by action (from `governance/assessors.py`)

| Factor | Payment execution | Contract approval | Amendment approval | Agent query/tool |
|---|---|---|---|---|
| S sensitivity | 2 (masked banking ref) +1 unverified beneficiary +1 suspicious document | 3 (+1 bank details in document) | 3 | classification of data accessed |
| F financial impact | per-payment band (base currency): ≤5k 1 · ≤25k 2 · ≤100k 3 · ≤500k 4 · >500k 5 | commitment band: ≤60k 1 · ≤3L 2 · ≤12L 3 · ≤60L 4 · >60L 5 | max(commitment-delta band, new per-payment band) | per-payment band if an amount is involved, else 0 |
| I irreversibility | 4 (bank payouts) | 2 +1 auto-execution +1 term > 24 months | 3 | 2 writes, 0 reads |
| E evidence risk | +2 invoice missing · +1 low extraction confidence · +1 manual corrections · +2 document integrity failure · +2 version mismatch | +2 per unresolved critical field (≤4) · +1 low-confidence fields · +1 no signature · +1 many corrections | +2 no document · +2 document mismatch · +1 change > 25% | (1 − coverage)·5 + 1 conflicting |
| T tool risk | payout.create = 4 | contract.activate = 3 | contract.amend = 3 | tool risk table |
| U uncertainty | +1 first payment · +1/2 prior failures · +2 unknown outcome · +1 prorated · +2 injection flag | +1 low-confidence OCR · +2 conflicts · +2 injection · +1 corrections | +1 frequency change · +2 injection · +1 change > 50% | (1 − coverage)·3 + 2 injection |

Good evidence lowers E and U, never F. Roles and approvals never lower a factor.

## Decision order (non-compensatory)

1. any failed hard rule with on_fail=BLOCK → **BLOCK**
2. … on_fail=ESCALATE → **ESCALATE** (risk/compliance)
3. … on_fail=VERIFY → **VERIFY** (retrieve/request evidence)
4. score ≥ escalate_min → **ESCALATE**
5. human required by policy, score > act_max, or an on_fail=APPROVE rule → **APPROVE** (role by approval limits; two
   approvers above the dual-approval threshold)
6. otherwise → **ACT**

A recorded compliance clearance turns ESCALATE into APPROVE. Recorded human approval satisfies APPROVE. Neither changes
the score, and neither can override BLOCK or VERIFY.

## Example thresholds (policy `v1.0.0-example`)

| Action | ACT ≤ | ESCALATE ≥ | Human always |
|---|---|---|---|
| PAYMENT_EXECUTION | 45 | 70 | no |
| CONTRACT_APPROVAL | — | 65 | yes |
| AMENDMENT_APPROVAL | — | 60 | yes |
| AGENT_QUERY | 40 | 75 | no |
| AGENT_TOOL | 40 | 70 | no |

Worked example: a valid ₹20,000 monthly payment gives S2 F2 I4 E0 T4 U0, so 6+10+12+0+12+0 = **40 → ACT**. The same
contract at ₹1,50,000 gives F4, so **50 → APPROVE** by a Finance Approver (limit ₹2,00,000).

The calibration run (calibration split only) selected act_max=49 / escalate_min=59 for payments. It is reported, not
auto-activated: an administrator can publish it as a new policy version.

## Hard rules

Payments: actor authorized, org scope, not self-payment, contract not expired/terminated/rejected, not suspended
(ESCALATE), active, period within contract dates, due, version current, not duplicate, no unreconciled attempt
(ESCALATE), amount/currency match re-derived terms, cumulative cap (ESCALATE), verified beneficiary (VERIFY), no pending
amendment (VERIFY), invoice present/accepted (VERIFY) and matching (ESCALATE), auto-execution permitted (APPROVE).
Contracts: org scope, valid dates, no duplicate active contract, critical fields resolved (VERIFY), no conflicts
(VERIFY), no injection (ESCALATE), approver authorized, not self, validator ≠ approver, status, validation passed.
Amendments: org scope, contract active, base version current, not retroactive, currency unchanged (ESCALATE), supporting
document for large increases (VERIFY), document matches (ESCALATE), no injection (ESCALATE), approver authorized, not
self, status. Agent: tool allowlisted, delegated authorization, no injection (ESCALATE), factual answers need evidence
(VERIFY).
