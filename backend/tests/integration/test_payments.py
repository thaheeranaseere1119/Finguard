"""Recurring payment execution, sandbox scenarios, webhooks and recovery (required tests 10–14, 20)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from finguard.models import (ApprovalRequest, AuditLog, Contract, PaymentAttempt, PaymentProviderEvent, SandboxPayout,
                             ScheduledPayment)
from finguard.payments.providers import get_payout_provider
from finguard.services import approvals, contracts, payments
from tests.conftest import drain, onboard


def _sps(db, c):
    db.expire_all()
    return db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id)
                      .order_by(ScheduledPayment.sequence_no)).scalars().all()


def _due(db, c, now):
    return [s for s in _sps(db, c) if s.due_at <= now]


def _sandbox_count(db, sp):
    return db.execute(select(func.count()).select_from(SandboxPayout).where(
        SandboxPayout.idempotency_key.like(f"{sp.idempotency_key}%"))).scalar_one()


def test_10_due_payment_passes_checks_and_is_paid(db, make_org, now):
    o = make_org()
    c = onboard(db, o, amount=20000)
    due = _due(db, c, now)
    assert due, "contract started in the past, so occurrences are due"
    drain(db, now)
    for sp in _due(db, c, now):
        assert sp.status == "PAID"
        assert len(sp.attempts) == 1 and sp.attempts[0].status == "PROCESSED"
        assert sp.attempts[0].provider == "mock" and sp.attempts[0].provider_mode == "sandbox"
    future = [s for s in _sps(db, c) if s.due_at > now]
    assert all(s.status == "SCHEDULED" for s in future)  # nothing paid early
    ev = db.execute(select(AuditLog).where(AuditLog.event_type == "governance.decision",
                                           AuditLog.contract_id == c.id, AuditLog.new_state == "ACT")).scalars().all()
    assert len(ev) == len(due)
    assert all(e.payload["policy_version"] and e.payload["factors"] for e in ev)


def test_high_value_payment_waits_for_approval_then_executes(db, make_org, now):
    o = make_org()
    c = onboard(db, o, amount=150000, approver="manager")
    drain(db, now)
    sp = _due(db, c, now)[0]
    assert sp.status == "AWAITING_APPROVAL"
    ar = db.get(ApprovalRequest, sp.approval_request_id)
    with pytest.raises(Exception):
        approvals.decide(db, o.p(db, "contractor"), ar.id, "APPROVE", "my own payment")
    db.rollback()
    approvals.decide(db, o.p(db, "approver"), ar.id, "APPROVE", "Invoice and contract verified")
    db.commit()
    sp = db.get(ScheduledPayment, sp.id)
    db.refresh(sp)
    assert sp.status == "PAID"


def test_11_payment_blocked_when_contract_expired(db, make_org, now):
    o = make_org()
    c = onboard(db, o)
    c = db.get(Contract, c.id)
    contracts.transition(db, o.p(db, "risk"), c, "EXPIRED", "test: contract expired")
    db.commit()
    drain(db, now)
    for sp in _due(db, c, now):
        assert sp.status == "BLOCKED"
        assert not sp.attempts
        assert "EXPIRED" in sp.hold_reason


def test_suspended_contract_holds_payment_for_compliance(db, make_org, now):
    o = make_org()
    c = onboard(db, o)
    contracts.set_hold(db, o.p(db, "risk"), c.id, "SUSPEND", "Investigation pending")
    db.commit()
    drain(db, now)
    assert {sp.status for sp in _due(db, c, now)} == {"ESCALATED"}


def test_12_duplicate_payment_attempt_rejected(db, make_org, now):
    o = make_org()
    c = onboard(db, o)
    drain(db, now)
    sp = _due(db, c, now)[0]
    assert sp.status == "PAID"
    # 1) governance refuses a second execution of the same occurrence
    sched = o.scheduler(db)
    sp.status = "SCHEDULED"
    decision = payments.govern_and_route(db, sched, sp, now=now)
    db.commit()
    assert decision == "BLOCK"
    assert len(db.get(ScheduledPayment, sp.id).attempts) == 1
    # 2) the database refuses a second live attempt even if application checks were bypassed
    a = sp.attempts[0]
    with pytest.raises(Exception):
        db.execute(text("INSERT INTO payment_attempts (id, org_id, scheduled_payment_id, attempt_no, idempotency_key, provider,"
                        " provider_mode, amount, currency, fund_account_id, status, initiated_by, governance_decision_id,"
                        " response_snapshot, created_at, updated_at) SELECT gen_random_uuid(), org_id, scheduled_payment_id, 2,"
                        " idempotency_key || '-dup', provider, provider_mode, amount, currency, fund_account_id, 'CREATED',"
                        " initiated_by, governance_decision_id, '{}', now(), now() FROM payment_attempts WHERE id = :id"),
                   {"id": a.id})
    db.rollback()
    assert _sandbox_count(db, sp) == 1


def test_13_timeout_never_causes_duplicate_and_is_reconciled(db, make_org, now):
    o = make_org()
    c = onboard(db, o, account="50100000044444")  # sandbox: provider creates payout, then the response times out
    drain(db, now)
    sp = _due(db, c, now)[0]
    assert sp.status == "UNKNOWN" and sp.attempts[0].status == "UNKNOWN"
    # A retry is refused while the outcome is unknown
    with pytest.raises(Exception):
        payments.retry_failed(db, o.p(db, "analyst"), sp.id)
    db.rollback()
    # A new scheduler run does not create another payout
    drain(db, now + timedelta(minutes=5))
    assert _sandbox_count(db, sp) == 1
    payments.reconcile_attempt(db, o.p(db, "analyst"), db.get(ScheduledPayment, sp.id).attempts[0])
    sp = db.get(ScheduledPayment, sp.id)
    db.refresh(sp)
    assert sp.status == "PAID" and sp.attempts[0].status == "PROCESSED"
    assert _sandbox_count(db, sp) == 1


def test_timeout_where_payout_never_created_allows_safe_retry(db, make_org, now):
    o = make_org()
    c = onboard(db, o, account="50100000048888")
    drain(db, now)
    sp = _due(db, c, now)[0]
    assert sp.status == "UNKNOWN"
    payments.reconcile_attempt(db, o.p(db, "analyst"), sp.attempts[0])
    sp = db.get(ScheduledPayment, sp.id)
    db.refresh(sp)
    assert sp.status == "FAILED" and sp.attempts[0].failure_code == "not_found_at_provider"


@pytest.mark.parametrize("account,status,attempt_status", [
    ("50100000041111", "FAILED", "FAILED"),     # rejected by provider
    ("50100000042222", "FAILED", "FAILED"),     # insufficient funds
    ("50100000043333", "FAILED", "FAILED"),     # invalid beneficiary
    ("50100000047777", "FAILED", "FAILED"),     # network error before send
    ("50100000045555", "SUBMITTED", "PENDING"),  # pending at provider
])
def test_sandbox_failure_scenarios(db, make_org, now, account, status, attempt_status):
    o = make_org()
    c = onboard(db, o, account=account)
    drain(db, now)
    sp = _due(db, c, now)[0]
    assert sp.status == status and sp.attempts[0].status == attempt_status


def test_retry_after_confirmed_failure(db, make_org, now):
    o = make_org()
    c = onboard(db, o, account="50100000042222")
    drain(db, now)
    sp = _due(db, c, now)[0]
    assert sp.status == "FAILED"
    decision = payments.retry_failed(db, o.p(db, "analyst"), sp.id)
    db.commit()
    # A prior failure raises model uncertainty, so the retry is routed to a human instead of auto-executing.
    assert decision == "APPROVE"
    sp = db.get(ScheduledPayment, sp.id)
    db.refresh(sp)
    approvals.decide(db, o.p(db, "approver"), sp.approval_request_id, "APPROVE", "Funds topped up; retry approved")
    db.commit()
    db.expire_all()
    sp = db.get(ScheduledPayment, sp.id)
    assert [a.attempt_no for a in sp.attempts] == [1, 2]   # new attempt with a NEW idempotency key
    assert len({a.idempotency_key for a in sp.attempts}) == 2


def _webhook(provider, sp, status):
    p = db_payout = None
    from finguard.core.db import new_session

    with new_session() as s:
        db_payout = s.execute(select(SandboxPayout).where(SandboxPayout.idempotency_key.like(f"{sp.idempotency_key}%"))).scalar_one()
        db_payout.status = status
        s.commit()
        return provider.webhook_for(db_payout)


def test_pending_then_success_via_webhook_and_duplicate_delivery(db, make_org, now):
    o = make_org()
    c = onboard(db, o, account="50100000045555")
    drain(db, now)
    sp = _due(db, c, now)[0]
    body, headers = _webhook(get_payout_provider(), sp, "processed")
    assert payments.handle_webhook(db, "mock", body, headers)["outcome"] == "APPLIED"
    assert payments.handle_webhook(db, "mock", body, headers)["outcome"] == "DUPLICATE"
    sp = db.get(ScheduledPayment, sp.id)
    db.refresh(sp)
    assert sp.status == "PAID"


def test_pending_then_failure_via_webhook(db, make_org, now):
    o = make_org()
    c = onboard(db, o, account="50100000046666")
    drain(db, now)
    sp = _due(db, c, now)[0]
    body, headers = _webhook(get_payout_provider(), sp, "failed")
    assert payments.handle_webhook(db, "mock", body, headers)["outcome"] == "APPLIED"
    sp = db.get(ScheduledPayment, sp.id)
    db.refresh(sp)
    assert sp.status == "FAILED"


def test_14_invalid_webhook_signature_rejected(db, make_org, now):
    o = make_org()
    c = onboard(db, o, account="50100000045555")
    drain(db, now)
    sp = _due(db, c, now)[0]
    body, headers = _webhook(get_payout_provider(), sp, "processed")
    headers["x-razorpay-signature"] = "0" * 64
    assert payments.handle_webhook(db, "mock", body, headers)["outcome"] == "REJECTED"
    tampered = body.replace(b'"processed"', b'"failed"')
    good_sig = _webhook(get_payout_provider(), sp, "processed")[1]
    assert payments.handle_webhook(db, "mock", tampered, good_sig)["outcome"] == "REJECTED"
    sp = db.get(ScheduledPayment, sp.id)
    db.refresh(sp)
    assert sp.status == "SUBMITTED"  # unchanged by forged events
    assert db.execute(select(AuditLog).where(AuditLog.event_type == "webhook.rejected")).first()


def test_webhook_amount_mismatch_is_not_applied(db, make_org, now):
    o = make_org()
    c = onboard(db, o, account="50100000045555")
    drain(db, now)
    sp = _due(db, c, now)[0]
    provider = get_payout_provider()
    body, _ = _webhook(provider, sp, "processed")
    data = json.loads(body)
    data["payload"]["payout"]["entity"]["amount"] += 100
    forged = json.dumps(data).encode()
    headers = {"x-razorpay-signature": provider.sign(forged), "x-razorpay-event-id": "evt-mismatch-1"}
    assert payments.handle_webhook(db, "mock", forged, headers)["outcome"] == "MISMATCH"
    sp = db.get(ScheduledPayment, sp.id)
    db.refresh(sp)
    assert sp.status == "UNKNOWN"


def test_20_recovery_after_worker_restart(db, make_org, now):
    """A worker crashed after claiming one occurrence and after recording a payout intent for another."""
    o = make_org()
    c = onboard(db, o, account="50100000040000")
    due = _due(db, c, now)
    assert len(due) >= 2
    stale = now - timedelta(minutes=30)
    a, b = due[0], due[1]
    a.status, a.claimed_at, a.claimed_by = "PROCESSING", stale, "dead-worker"
    # b: intent persisted (CREATED attempt) but the provider was never called before the crash
    gd_id = None
    from finguard.governance.assessors import assess_payment
    from finguard.governance.service import active_policy, record_assessment

    pv = active_policy(db, b.org_id)
    gd = record_assessment(db, o.scheduler(db), assess_payment(payments.build_snapshot(db, o.scheduler(db), b, pv.config, now=now),
                                                               pv.config), pv, resource_type="scheduled_payment",
                           resource_id=b.id)
    gd_id = gd.id
    ben = db.get(Contract, c.id).beneficiary
    db.add(PaymentAttempt(org_id=b.org_id, scheduled_payment_id=b.id, attempt_no=1, idempotency_key=f"{b.idempotency_key}-a1",
                          provider="mock", provider_mode="sandbox", amount=b.amount, currency=b.currency,
                          fund_account_id=ben.provider_fund_account_id, status="CREATED", initiated_by=o.users["admin"].id,
                          governance_decision_id=gd_id, created_at=stale, updated_at=stale))
    b.status = "SUBMITTED"
    db.commit()
    # "Restart": the next scheduler cycle recovers both
    drain(db, now)
    payments.reconcile_open(db)
    a, b = db.get(ScheduledPayment, a.id), db.get(ScheduledPayment, b.id)
    db.refresh(a), db.refresh(b)
    assert a.status == "PAID" and len(a.attempts) == 1
    # b's intent was never sent: reconciliation finds no payout → FAILED (safe to retry), never a blind duplicate
    assert b.status == "FAILED" and b.attempts[0].status == "FAILED"
    assert _sandbox_count(db, b) == 0
