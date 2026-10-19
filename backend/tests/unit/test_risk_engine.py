"""Risk model and decision policy (pure functions)."""

from datetime import date
from decimal import Decimal as D

import pytest

from finguard.governance.assessors import (AgentSnapshot, AmendmentSnapshot, ContractApprovalSnapshot, PaymentSnapshot,
                                           assess_agent, assess_amendment, assess_contract_approval, assess_payment)
from finguard.governance.policy import decide, default_policy, rule
from finguard.models.enums import ActionType, Decision
from finguard.risk.engine import RiskVector, make_factor, risk_score


def vec(s, f, i, e, t, u):
    return RiskVector(*(make_factor(k, v) for k, v in zip("SFIETU", (s, f, i, e, t, u))))


def test_weighted_formula_and_bounds():
    assert risk_score(vec(0, 0, 0, 0, 0, 0)) == D("0.00")
    assert risk_score(vec(5, 5, 5, 5, 5, 5)) == D("100.00")
    # Risk = 3S + 5F + 3I + 3E + 3T + 3U
    assert risk_score(vec(2, 2, 4, 0, 4, 0)) == D(3 * 2 + 5 * 2 + 3 * 4 + 3 * 4)
    assert risk_score(vec(0, 1, 0, 0, 0, 0)) == D(5)
    assert risk_score(vec(1, 0, 0, 0, 0, 0)) == D(3)


def test_factors_are_clamped():
    assert make_factor("S", 9).value == D("5.00")
    assert make_factor("S", -3).value == D("0.00")


def test_custom_weights_are_normalized_to_100():
    w = {"S": 1, "F": 1, "I": 1, "E": 1, "T": 1, "U": 1}
    assert risk_score(vec(5, 5, 5, 5, 5, 5), w) == D("100.00")


def pay(**kw):
    base = dict(amount=D("20000.00"), currency="INR", expected_amount=D("20000.00"), expected_currency="INR",
                amount_base=D("20000"), due_date=date(2026, 11, 5), period_start=date(2026, 10, 1),
                period_end=date(2026, 10, 31), contract_start=date(2026, 1, 1), contract_end=date(2026, 12, 31),
                contract_status="ACTIVE", is_due=True, auto_execute_permitted=True)
    base.update(kw)
    return PaymentSnapshot(**base)


P = default_policy()


def test_low_risk_valid_payment_acts():
    a = assess_payment(pay(), P)
    assert a.outcome.decision == Decision.ACT
    assert a.score == D("40.00")


def test_high_value_payment_needs_approval_even_with_perfect_evidence():
    a = assess_payment(pay(amount=D("150000"), expected_amount=D("150000"), amount_base=D("150000")), P)
    assert a.vector.E.value == 0  # evidence is perfect...
    assert a.vector.F.value == 4  # ...but financial impact stays high
    assert a.outcome.decision == Decision.APPROVE


def test_dual_approval_above_threshold():
    a = assess_payment(pay(amount=D("600000"), expected_amount=D("600000"), amount_base=D("600000")), P)
    assert a.outcome.decision == Decision.APPROVE
    assert a.outcome.required_approvals == 2
    assert a.outcome.required_approver_role == "FINANCE_MANAGER"


@pytest.mark.parametrize("override,rule_id", [
    ({"contract_status": "EXPIRED"}, "PAY_CONTRACT_NOT_EXPIRED"),
    ({"actor_authorized": False}, "PAY_ACTOR_AUTHORIZED"),
    ({"same_org": False}, "PAY_ORG_SCOPE"),
    ({"actor_is_contractor": True}, "PAY_NOT_SELF"),
    ({"already_paid": True}, "PAY_NOT_DUPLICATE"),
    ({"amount": D("25000.00")}, "PAY_AMOUNT_CURRENCY_MATCH"),
    ({"currency": "USD"}, "PAY_AMOUNT_CURRENCY_MATCH"),
    ({"is_due": False}, "PAY_IS_DUE"),
    ({"version_current": False}, "PAY_VERSION_CURRENT"),
    ({"period_start": date(2025, 12, 1)}, "PAY_WITHIN_CONTRACT_DATES"),
])
def test_hard_rules_block_regardless_of_score(override, rule_id):
    a = assess_payment(pay(**override), P)
    assert a.outcome.decision == Decision.BLOCK
    assert rule_id in " ".join(a.outcome.reasons)


def test_verify_when_evidence_retrievable():
    a = assess_payment(pay(invoice_required=True, invoice_status=None), P)
    assert a.outcome.decision == Decision.VERIFY
    a = assess_payment(pay(beneficiary_verified=False), P)
    assert a.outcome.decision == Decision.VERIFY
    a = assess_payment(pay(pending_amendment_affects=True), P)
    assert a.outcome.decision == Decision.VERIFY


def test_escalate_on_unknown_outcome_and_cap():
    assert assess_payment(pay(unknown_attempt=True), P).outcome.decision == Decision.ESCALATE
    assert assess_payment(pay(total_value=D("30000"), paid_to_date=D("20000")), P).outcome.decision == Decision.ESCALATE
    assert assess_payment(pay(contract_status="SUSPENDED"), P).outcome.decision == Decision.ESCALATE


def test_human_approval_does_not_lower_risk_score():
    base = assess_payment(pay(amount=D("150000"), expected_amount=D("150000"), amount_base=D("150000")), P)
    approved = assess_payment(pay(amount=D("150000"), expected_amount=D("150000"), amount_base=D("150000"),
                                  human_approval_granted=True), P, {"human_approved": True})
    assert approved.score == base.score
    assert approved.outcome.decision == Decision.ACT


def test_human_approval_cannot_override_block():
    a = assess_payment(pay(contract_status="EXPIRED", human_approval_granted=True), P, {"human_approved": True})
    assert a.outcome.decision == Decision.BLOCK


def test_contract_not_auto_executable_requires_approval():
    a = assess_payment(pay(auto_execute_permitted=False), P)
    assert a.outcome.decision == Decision.APPROVE


def test_decision_order_block_beats_escalate_beats_verify():
    rules = [rule("A", False, Decision.VERIFY, "v"), rule("B", False, Decision.ESCALATE, "e"),
             rule("C", False, Decision.BLOCK, "b")]
    assert decide(ActionType.PAYMENT_EXECUTION, D(10), rules, P).decision == Decision.BLOCK
    assert decide(ActionType.PAYMENT_EXECUTION, D(10), rules[:2], P).decision == Decision.ESCALATE
    assert decide(ActionType.PAYMENT_EXECUTION, D(10), rules[:1], P).decision == Decision.VERIFY
    assert decide(ActionType.PAYMENT_EXECUTION, D(75), [], P).decision == Decision.ESCALATE


def test_contract_approval_always_human_and_injection_escalates():
    snap = ContractApprovalSnapshot(commitment_base=D("240000"), payment_amount_base=D("20000"),
                                    contract_status="PENDING_APPROVAL", preview=True)
    assert assess_contract_approval(snap, P).outcome.decision == Decision.APPROVE
    snap.injection_detected = True
    assert assess_contract_approval(snap, P).outcome.decision == Decision.ESCALATE


def test_contract_self_approval_blocked():
    snap = ContractApprovalSnapshot(commitment_base=D("240000"), payment_amount_base=D("20000"),
                                    contract_status="PENDING_APPROVAL", approver_is_contractor=True)
    assert assess_contract_approval(snap, P).outcome.decision == Decision.BLOCK


def test_amendment_rules():
    base = dict(old_amount=D("20000"), new_amount=D("25000"), old_currency="INR", new_currency="INR",
                commitment_delta_base=D("30000"), new_amount_base=D("25000"), amendment_status="PENDING_APPROVAL",
                preview=True)
    assert assess_amendment(AmendmentSnapshot(**base), P).outcome.decision == Decision.VERIFY  # 25% w/o document
    assert assess_amendment(AmendmentSnapshot(**base, has_document=True, document_matches=True), P).outcome.decision == Decision.APPROVE
    assert assess_amendment(AmendmentSnapshot(**base, has_document=True, document_matches=False), P).outcome.decision == Decision.ESCALATE
    assert assess_amendment(AmendmentSnapshot(**base, retroactive_affects_paid=True), P).outcome.decision == Decision.BLOCK
    b2 = dict(base, new_currency="USD")
    assert assess_amendment(AmendmentSnapshot(**b2, has_document=True), P).outcome.decision == Decision.ESCALATE


def test_agent_tool_not_allowlisted_or_unauthorized_blocks():
    assert assess_agent(AgentSnapshot("payout.create.arbitrary", True, 1, D(1)), P).outcome.decision == Decision.BLOCK
    assert assess_agent(AgentSnapshot("read.contract", False, 1, D(1)), P).outcome.decision == Decision.BLOCK
    assert assess_agent(AgentSnapshot("read.contract", True, 1, D(1)), P).outcome.decision == Decision.ACT


def test_ablation_flags_change_only_targeted_factors():
    s = pay(invoice_required=True, invoice_status="ACCEPTED", invoice_amount=D("20000.00"), first_payment=True,
            fields_corrected=2)
    full = assess_payment(s, P)
    no_e = assess_payment(s, P, {"no_evidence": True})
    no_u = assess_payment(s, P, {"no_uncertainty": True})
    assert no_e.vector.E.value == 0 and no_e.vector.F.value == full.vector.F.value
    assert no_u.vector.U.value == 0 and no_u.vector.S.value == full.vector.S.value


def test_factual_answer_without_evidence_requires_verification():
    snap = AgentSnapshot("read.evidence", True, 1, D(0), evidence_required=True)
    a = assess_agent(snap, P)
    assert a.outcome.decision == Decision.VERIFY
    assert assess_agent(AgentSnapshot("read.evidence", True, 1, D(1), evidence_required=True), P).outcome.decision == Decision.ACT
