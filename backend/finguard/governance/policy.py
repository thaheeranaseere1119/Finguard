"""Governance policy: default configuration and the deterministic ACT / APPROVE / ESCALATE / VERIFY / BLOCK decision.

All thresholds below are EXAMPLE values for a research prototype. They are configurable per action type through
policy_versions and must be calibrated on the calibration split; they are not claimed to be optimal.

Decision order (non-compensatory — a good score never overrides a failed hard rule):
  1. any failed hard rule with on_fail=BLOCK     → BLOCK
  2. any failed hard rule with on_fail=ESCALATE  → ESCALATE
  3. any failed hard rule with on_fail=VERIFY    → VERIFY
  4. score ≥ escalate_min                        → ESCALATE
  5. human_required, score > act_max, or a failed on_fail=APPROVE rule → APPROVE
  6. otherwise                                   → ACT
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finguard.models.enums import ActionType, Decision, RoleName

POLICY_SCHEMA_VERSION = "1"

DEFAULT_POLICY: dict[str, Any] = {
    "schema": POLICY_SCHEMA_VERSION,
    "label": "Prototype research policy (EXAMPLE thresholds — calibrate before relying on them)",
    "weights": {"S": "3", "F": "5", "I": "3", "E": "3", "T": "3", "U": "3"},
    "thresholds": {
        ActionType.PAYMENT_EXECUTION: {"act_max": "45", "escalate_min": "70", "human_required": False},
        ActionType.CONTRACT_APPROVAL: {"act_max": "0", "escalate_min": "65", "human_required": True},
        ActionType.AMENDMENT_APPROVAL: {"act_max": "0", "escalate_min": "60", "human_required": True},
        ActionType.AGENT_QUERY: {"act_max": "40", "escalate_min": "75", "human_required": False},
        ActionType.AGENT_TOOL: {"act_max": "40", "escalate_min": "70", "human_required": False},
    },
    # Financial-impact bands in the organization's base currency: [[upper_bound, level], ...]; above last → 5.
    "financial_bands": {
        "per_payment": [["0", 0], ["5000", 1], ["25000", 2], ["100000", 3], ["500000", 4]],
        "commitment": [["0", 0], ["60000", 1], ["300000", 2], ["1200000", 3], ["6000000", 4]],
    },
    # Approval limits per role, in base currency (synthetic research values).
    "approval_limits": {
        RoleName.FINANCE_APPROVER: "200000",
        RoleName.FINANCE_MANAGER: "2000000",
    },
    # Payments above this require two distinct approvers (at least one within limit).
    "dual_approval_threshold": "500000",
    # Contract approvals: commitment above this requires a FINANCE_MANAGER.
    "contract_manager_threshold": "1200000",
    "auto_execute_enabled": True,
    # Invoices are validated automatically (no analyst step). A valid invoice is then routed by its CLAIM TIER:
    #   claim ≤ contract amount                      → pre-authorized by the approved contract → ACT (paid automatically)
    #   contract < claim ≤ contract × (1 + tol%)     → APPROVE (approver / manager by approval limit)
    #   claim > tolerance, invalid invoice, or payee changed → ESCALATE (risk officer)
    # Hard rules and the escalate_min score still apply to every claim.
    "invoice_auto_accept": True,
    "invoice_tiers": {"enabled": True, "approve_tolerance_pct": "10"},
    "allow_validator_as_approver": False,
    "min_field_confidence": "0.70",
    "amendment_increase_requires_document_pct": "10",
    # Synthetic FX rates to base currency for limits/bands only — never used to compute a payout amount.
    "fx_to_base": {"INR": "1", "USD": "83", "EUR": "90", "GBP": "105", "SGD": "62", "AED": "22.6", "JPY": "0.56"},
    "tool_risk": {"payout.create": 4, "payout.reconcile": 2, "contract.activate": 3, "contract.amend": 3,
                  "read.contract": 1, "read.payments": 1, "read.evidence": 1, "calc.totals": 0,
                  "approval.submit": 1, "incident.create": 1, "payment.prepare": 3, "read.financials": 1},
}


def default_policy() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_POLICY)


@dataclass
class HardRuleResult:
    rule_id: str
    passed: bool
    on_fail: str  # Decision value
    message: str
    # terminal=True: a failure means the operation can never succeed as-is (e.g. contract expired).
    terminal: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "passed": self.passed, "on_fail": self.on_fail, "message": self.message,
                "terminal": self.terminal, "details": self.details}


def rule(rule_id: str, passed: bool, on_fail: Decision, message: str, *, terminal: bool = False,
         **details: Any) -> HardRuleResult:
    return HardRuleResult(rule_id, bool(passed), on_fail.value, message, terminal, details)


@dataclass
class DecisionOutcome:
    decision: Decision
    reasons: list[str]
    required_approver_role: str | None
    required_approvals: int
    required_next_actions: list[str]
    thresholds: dict[str, Any]


def to_base(amount: Decimal, currency: str, policy: dict[str, Any]) -> Decimal:
    rate = policy.get("fx_to_base", {}).get(currency)
    if rate is None:
        # Unknown currency: fail safe by treating it as very large.
        return Decimal("1e12")
    return amount * Decimal(str(rate))


def approver_role_for(amount_base: Decimal | None, policy: dict[str, Any]) -> tuple[str, int]:
    limits = policy["approval_limits"]
    approvals = 1
    if amount_base is not None and amount_base > Decimal(str(policy["dual_approval_threshold"])):
        approvals = 2
    if amount_base is None or amount_base <= Decimal(str(limits[RoleName.FINANCE_APPROVER])):
        return RoleName.FINANCE_APPROVER, approvals
    return RoleName.FINANCE_MANAGER, approvals


def decide(action_type: ActionType | str, score: Decimal, rules: list[HardRuleResult], policy: dict[str, Any], *,
           amount_base: Decimal | None = None, ablate_hard_rules: bool = False, human_approved: bool = False,
           escalation_cleared: bool = False, preauthorized: bool = False) -> DecisionOutcome:
    """Pure decision function. ``escalation_cleared`` (a recorded compliance clearance) turns ESCALATE into APPROVE;
    ``human_approved`` (recorded approvals by authorized humans) satisfies APPROVE. Neither lowers the risk score and
    neither can override BLOCK or VERIFY."""
    outcome = _decide(action_type, score, rules, policy, amount_base=amount_base, ablate_hard_rules=ablate_hard_rules,
                      preauthorized=preauthorized)
    if escalation_cleared and outcome.decision == Decision.ESCALATE:
        role, approvals = approver_role_for(amount_base, policy)
        outcome = DecisionOutcome(Decision.APPROVE, outcome.reasons + ["Escalation cleared by risk/compliance review; "
                                                                       "human approval still required."],
                                  role, approvals, [f"Approval by {role}."], outcome.thresholds)
    if human_approved and outcome.decision == Decision.APPROVE:
        outcome = DecisionOutcome(Decision.ACT, outcome.reasons + ["Executed under recorded human approval."],
                                  None, 0, [], outcome.thresholds)
    return outcome


def _decide(action_type: ActionType | str, score: Decimal, rules: list[HardRuleResult], policy: dict[str, Any], *,
            amount_base: Decimal | None = None, ablate_hard_rules: bool = False, preauthorized: bool = False) -> DecisionOutcome:
    th = policy["thresholds"][str(action_type)]
    act_max, esc_min = Decimal(str(th["act_max"])), Decimal(str(th["escalate_min"]))
    failed = [] if ablate_hard_rules else [r for r in rules if not r.passed]
    reasons: list[str] = []
    next_actions: list[str] = []

    def by(kind: Decision) -> list[HardRuleResult]:
        return [r for r in failed if r.on_fail == kind.value]

    role, approvals = approver_role_for(amount_base, policy)
    thresholds = {"act_max": str(act_max), "escalate_min": str(esc_min), "human_required": bool(th["human_required"])}

    if blocks := by(Decision.BLOCK):
        reasons += [f"[{r.rule_id}] {r.message}" for r in blocks]
        next_actions.append("Operation rejected. Resolve the blocking condition; it cannot be approved as-is.")
        return DecisionOutcome(Decision.BLOCK, reasons, None, 0, next_actions, thresholds)
    if escalations := by(Decision.ESCALATE):
        reasons += [f"[{r.rule_id}] {r.message}" for r in escalations]
        next_actions.append("Risk/compliance review required before any approval.")
        return DecisionOutcome(Decision.ESCALATE, reasons, RoleName.RISK_OFFICER, 1, next_actions, thresholds)
    if verifies := by(Decision.VERIFY):
        reasons += [f"[{r.rule_id}] {r.message}" for r in verifies]
        next_actions += [f"Provide or verify: {r.message}" for r in verifies]
        return DecisionOutcome(Decision.VERIFY, reasons, RoleName.FINANCE_ANALYST, 0, next_actions, thresholds)
    if score >= esc_min:
        reasons.append(f"Risk score {score} ≥ escalation threshold {esc_min}.")
        next_actions.append("Risk/compliance review required before any approval.")
        return DecisionOutcome(Decision.ESCALATE, reasons, RoleName.RISK_OFFICER, 1, next_actions, thresholds)
    approve_rules = by(Decision.APPROVE)
    # `preauthorized`: a valid claim within the human-approved contract amount. The contract approval already
    # authorized this spend, so a medium score does not force another approval (escalate_min above still applies).
    if th["human_required"] or (score > act_max and not preauthorized) or approve_rules:
        if th["human_required"]:
            reasons.append(f"Policy requires human approval for {action_type}.")
        if score > act_max and not th["human_required"]:
            reasons.append(f"Risk score {score} > autonomous threshold {act_max}.")
        reasons += [f"[{r.rule_id}] {r.message}" for r in approve_rules]
        next_actions.append(f"Approval by {role}" + (" (two distinct approvers)" if approvals == 2 else "") + ".")
        return DecisionOutcome(Decision.APPROVE, reasons, role, approvals, next_actions, thresholds)
    if preauthorized and score > act_max:
        reasons.append(f"Claim is within the approved contract amount (pre-authorized by the contract approval); "
                       f"risk score {score} < escalation threshold {esc_min} and all mandatory controls passed.")
    else:
        reasons.append(f"Risk score {score} ≤ autonomous threshold {act_max} and all mandatory controls passed.")
    return DecisionOutcome(Decision.ACT, reasons, None, 0, next_actions, thresholds)
