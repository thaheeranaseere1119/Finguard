"""Contractor invoice drafting, PDF generation and claim flow."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from finguard.core.errors import Conflict, Forbidden, NotFound
from finguard.models import ContractDocument, Invoice, ScheduledPayment
from finguard.services import beneficiaries, invoicing
from tests.conftest import drain, onboard


def _held(db, c, now):
    drain(db, now)
    return db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id, ScheduledPayment.status == "HELD")
                      .order_by(ScheduledPayment.sequence_no)).scalars().first()


def test_draft_pdf_claim_and_release(db, make_org, now):
    o = make_org()
    c = onboard(db, o, amount=85000, invoice_required=True)
    sp = _held(db, c, now)
    assert sp is not None and "invoice" in sp.hold_reason.lower()
    d = invoicing.draft(db, o.p(db, "contractor"), sp.id)
    assert Decimal(d["amount"]) == sp.amount == Decimal("85000.00") and d["claimable"] and d["existing_invoice"] is None
    pdf = invoicing.render_pdf(d)
    assert pdf.startswith(b"%PDF")
    import fitz

    with fitz.open(stream=pdf, filetype="pdf") as doc:
        from finguard.documents.text_extraction import clean_text

        text = clean_text(doc[0].get_text())
    assert d["invoice_number"] in text and "85,000.00" in text
    # another contractor in the same org cannot draft or claim it
    with pytest.raises(Forbidden):
        invoicing.claim(db, o.p(db, "contractor2"), sp.id)
    db.rollback()
    inv = invoicing.claim(db, o.p(db, "contractor"), sp.id)
    db.commit()
    # verified contractor + invoice equal to the approved schedule → accepted by rule, payment released at once
    assert inv.amount == sp.amount and inv.status == "ACCEPTED" and inv.reviewed_by is None
    assert inv.release["auto_accepted"] and inv.release["decision"] in ("ACT", "APPROVE")
    assert db.get(ContractDocument, inv.document_id).kind == "INVOICE"
    assert db.get(ScheduledPayment, sp.id).status in ("PAID", "AWAITING_APPROVAL")
    with pytest.raises(Conflict):
        invoicing.claim(db, o.p(db, "contractor"), sp.id)
    db.rollback()


def _pay(db, o, sp_id):
    """Get a payment to PAID (approving it if the risk engine asked for a human)."""
    from finguard.models import ApprovalRequest
    from finguard.services import approvals

    sp = db.get(ScheduledPayment, sp_id)
    if sp.status == "AWAITING_APPROVAL":
        approvals.decide(db, o.p(db, "approver"), sp.approval_request_id, "APPROVE", "Within limit and matches schedule")
        db.commit()
    db.expire_all()
    return db.get(ScheduledPayment, sp_id)


def _held_all(db, c, now):
    drain(db, now)
    return db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id, ScheduledPayment.status == "HELD")
                      .order_by(ScheduledPayment.sequence_no)).scalars().all()


def test_payout_account_change_escalates(db, make_org, now):
    o = make_org()
    c = onboard(db, o, amount=50000, invoice_required=True)
    first, second = _held_all(db, c, now)[:2]
    invoicing.claim(db, o.p(db, "contractor"), first.id)
    db.commit()
    assert _pay(db, o, first.id).status == "PAID"
    # contractor switches bank account; finance verifies it, but the change itself is escalated
    beneficiaries.register_beneficiary(db, o.p(db, "contractor"), c.id, "Priya Raman", "50100000099999", "ICIC0004321")
    db.commit()
    db.refresh(c)
    beneficiaries.verify_beneficiary(db, o.p(db, "analyst"), c.beneficiary_id, True, "new account verified")
    db.commit()
    sp2 = db.get(ScheduledPayment, second.id)
    if sp2.status == "SCHEDULED":
        drain(db, now)
    inv = invoicing.claim(db, o.p(db, "contractor"), second.id)
    db.commit()
    assert inv.status == "ACCEPTED"
    db.expire_all()
    sp2 = db.get(ScheduledPayment, second.id)
    assert sp2.status == "ESCALATED" and "Payout account (payee) changed" in (sp2.hold_reason or "")


def test_claimable_last_month_and_monthly_terms(db, make_org):
    from datetime import date

    o = make_org()
    c = onboard(db, o, amount=40000, invoice_required=True)
    a, b = invoicing.previous_month()
    res = invoicing.claimable(db, o.p(db, "contractor"))
    assert res["month"] == f"{a:%Y-%m}"
    assert all(it["period_start"] <= b.isoformat() and it["period_end"] >= a.isoformat() for it in res["items"])
    terms = invoicing.monthly_terms(db, o.p(db, "contractor"))
    assert terms[0]["amount"] == "40000.00" and terms[0]["invoice_required"] is True
    # cross-tenant access is hidden
    other = make_org("other")
    sp = db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id)).scalars().first()
    with pytest.raises(NotFound):
        invoicing.draft(db, other.p(db, "contractor"), sp.id)


def test_arrears_period_cannot_be_invoiced_before_it_ends(db, make_org):
    from datetime import date

    from finguard.core.errors import InvalidState

    o = make_org()
    c = onboard(db, o, amount=30000, invoice_required=True)
    current = db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id,
                                                        ScheduledPayment.period_start <= date.today(),
                                                        ScheduledPayment.period_end >= date.today())).scalars().first()
    d = invoicing.draft(db, o.p(db, "contractor"), current.id)
    assert d["claimable"] is False and d["billable_from"] > date.today().isoformat()
    with pytest.raises(InvalidState):
        invoicing.claim(db, o.p(db, "contractor"), current.id)


def test_invoice_overview_lists_ready_submitted_and_upcoming(db, make_org, now):
    o = make_org()
    c = onboard(db, o, amount=30000, invoice_required=True)
    drain(db, now)
    ov = invoicing.invoice_overview(db, o.p(db, "contractor"))
    assert ov["ready"] and all(d["claimable"] and d["existing_invoice"] is None for d in ov["ready"])
    assert [d["period_start"] for d in ov["ready"]] == sorted(d["period_start"] for d in ov["ready"])   # oldest first
    assert ov["upcoming"] and ov["upcoming"][0]["claimable"] is False
    first = ov["ready"][0]
    invoicing.claim(db, o.p(db, "contractor"), first["scheduled_payment_id"])
    db.commit()
    ov2 = invoicing.invoice_overview(db, o.p(db, "contractor"))
    assert first["scheduled_payment_id"] in [d["scheduled_payment_id"] for d in ov2["submitted"]]
    assert first["scheduled_payment_id"] not in [d["scheduled_payment_id"] for d in ov2["ready"]]
    from finguard.core.errors import Forbidden

    with pytest.raises(Forbidden):                     # finance roles do not submit contractor invoices
        invoicing.invoice_overview(db, o.p(db, "analyst"))
