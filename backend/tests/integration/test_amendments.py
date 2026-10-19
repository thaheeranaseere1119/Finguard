"""Amendments (required tests 15–16) and invoice VERIFY holds."""

from datetime import date
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from finguard.core.errors import Forbidden, InvalidState, ValidationFailed
from finguard.models import ApprovalRequest, Contract, ContractVersion, ScheduledPayment
from finguard.samples import amendment_lines, searchable_pdf
from finguard.services import amendments, approvals, beneficiaries, payments
from tests.conftest import drain, onboard


def _live(db, c):
    db.expire_all()
    return db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id,
                                                     ScheduledPayment.status.not_in(["SUPERSEDED", "CANCELLED"]))
                      .order_by(ScheduledPayment.period_start)).scalars().all()


def _request(db, o, c, new="25000", with_doc=True):
    eff = date.today().replace(day=1) + relativedelta(months=1)
    doc = None
    if with_doc:
        doc = ("amend.pdf", searchable_pdf(amendment_lines("X-1", "Acme Analytics Pvt Ltd", "Priya Raman", 20000, int(new), eff)))
    am = amendments.request_amendment(db, o.p(db, "contractor"), c.id, {"payment_amount": new},
                                      "Scope expanded to weekly reporting", eff, doc)
    db.commit()
    db.expire_all()
    return am, eff


def test_15_approved_amendment_changes_only_future_payments(db, make_org, now):
    o = make_org()
    c = onboard(db, o, amount=20000)
    drain(db, now)
    paid_before = [s for s in _live(db, c) if s.status == "PAID"]
    assert paid_before
    am, eff = _request(db, o, c)
    assert am.status == "PENDING_APPROVAL" and am.extraction_id  # amendment document OCR'd and compared
    ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.resource_id == am.id,
                                                  ApprovalRequest.status == "OPEN")).scalar_one()
    assert ar.kind == "AMENDMENT_APPROVAL"
    # requester cannot approve their own amendment
    with pytest.raises(Forbidden):
        approvals.decide(db, o.p(db, "contractor"), ar.id, "APPROVE", "approving my own change")
    db.rollback()
    approvals.decide(db, o.p(db, "approver"), ar.id, "APPROVE", "Signed amendment matches request")
    db.commit()
    c = db.get(Contract, c.id)
    db.refresh(c)
    v2 = db.get(ContractVersion, c.current_version_id)
    assert v2.version_no == 2 and v2.payment_amount == Decimal("25000.00") and v2.effective_from == eff
    v1 = db.execute(select(ContractVersion).where(ContractVersion.contract_id == c.id,
                                                  ContractVersion.version_no == 1)).scalar_one()
    assert v1.status == "SUPERSEDED" and v1.payment_amount == Decimal("20000.00")  # history preserved
    live = _live(db, c)
    for s in live:
        if s.period_start < eff:
            assert s.amount == Decimal("20000.00")
        else:
            assert s.amount == Decimal("25000.00") and s.contract_version_id == v2.id
    for s in paid_before:  # completed payments untouched
        s2 = db.get(ScheduledPayment, s.id)
        assert s2.status == "PAID" and s2.amount == Decimal("20000.00")
    assert len({s.period_start for s in live}) == len(live)  # no duplicate periods
    assert [s.sequence_no for s in live] == list(range(1, len(live) + 1))


def test_16_rejected_amendment_leaves_terms_unchanged(db, make_org):
    o = make_org()
    c = onboard(db, o, amount=20000)
    before = [(s.period_start, s.amount, s.contract_version_id) for s in _live(db, c)]
    am, _ = _request(db, o, c)
    ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.resource_id == am.id,
                                                  ApprovalRequest.status == "OPEN")).scalar_one()
    approvals.decide(db, o.p(db, "approver"), ar.id, "REJECT", "Budget not approved for expanded scope")
    db.commit()
    c = db.get(Contract, c.id)
    db.refresh(c)
    assert db.get(ContractVersion, c.current_version_id).payment_amount == Decimal("20000.00")
    assert [(s.period_start, s.amount, s.contract_version_id) for s in _live(db, c)] == before


def test_approved_contract_terms_are_immutable_in_database(db, make_org):
    o = make_org()
    c = onboard(db, o)
    v = db.get(ContractVersion, c.current_version_id)
    v.payment_amount = Decimal("99999.00")
    with pytest.raises(Exception, match="immutable"):
        db.commit()
    db.rollback()


def test_large_increase_without_document_requires_evidence(db, make_org):
    o = make_org()
    c = onboard(db, o)
    am, _ = _request(db, o, c, new="30000", with_doc=False)
    ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.resource_id == am.id,
                                                  ApprovalRequest.status == "OPEN")).scalar_one()
    assert ar.kind == "VERIFICATION"
    with pytest.raises(InvalidState):
        approvals.decide(db, o.p(db, "approver"), ar.id, "APPROVE", "approve without evidence")


def test_pending_amendment_holds_affected_payment(db, make_org, now):
    o = make_org()
    c = onboard(db, o)
    am, eff = _request(db, o, c)
    # simulate time: the first period on/after the effective date becomes due while the amendment is pending
    target = next(s for s in _live(db, c) if s.period_start >= eff)
    drain(db, target.due_at)
    assert db.get(ScheduledPayment, target.id).status == "HELD"


def test_amendment_cannot_change_unknown_fields_or_non_active(db, make_org):
    o = make_org()
    c = onboard(db, o)
    with pytest.raises(ValidationFailed):
        amendments.request_amendment(db, o.p(db, "contractor"), c.id, {"beneficiary_account": "123"}, "hack the account",
                                     date.today(), None)


def test_invoice_required_payment_held_until_invoice_accepted(db, make_org, now):
    o = make_org()
    c = onboard(db, o, invoice_required=True)
    drain(db, now)
    sp = next(s for s in _live(db, c) if s.due_at <= now)
    assert sp.status == "HELD" and "invoice" in sp.hold_reason.lower()
    from finguard.services import invoicing

    inv = invoicing.claim(db, o.p(db, "contractor"), sp.id)
    db.commit()
    with pytest.raises(Forbidden):
        beneficiaries.review_invoice(db, o.p(db, "contractor"), inv.id, True, "self review")
    db.rollback()
    # verified contractor + valid invoice within the contract amount → accepted by rule, no analyst step, released
    assert inv.status == "ACCEPTED" and inv.reviewed_by is None and inv.release["auto_accepted"]
    drain(db, now)
    assert db.get(ScheduledPayment, sp.id).status == "PAID"
