"""Payments, invoices, reconciliation and provider webhooks."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.api.deps import current_principal
from finguard.api.schemas import InvoiceIn, VerifyIn
from finguard.api.serializers import invoice_view, payment_view
from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.db import get_db
from finguard.core.errors import NotFound, RateLimited
from finguard.core.ratelimit import hit
from finguard.governance.service import decision_view
from finguard.models import (Contract, GovernanceDecision, Invoice, PaymentAttempt, PaymentProviderEvent, PolicyVersion,
                             RiskAssessment)
from finguard.services import beneficiaries as ben_svc
from finguard.services import payments as svc

router = APIRouter(tags=["payments"])


@router.get("/payments")
def list_payments(status: str | None = None, contract_id: str | None = None, due_before: date | None = None,
                  due_after: date | None = None, limit: int = Query(100, le=500), offset: int = 0,
                  p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    rows, total = svc.list_payments(db, p, status=status, contract_id=contract_id, due_before=due_before,
                                    due_after=due_after, limit=limit, offset=offset)
    contracts = {c.id: c for c in db.execute(select(Contract).where(Contract.id.in_({r.contract_id for r in rows}))).scalars().unique()} if rows else {}
    return {"items": [payment_view(r, contracts.get(r.contract_id)) for r in rows], "total": total, "limit": limit,
            "offset": offset}


@router.get("/payments/{payment_id}")
def get_payment(payment_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    sp = svc.get_payment(db, p, payment_id)
    c = db.get(Contract, sp.contract_id)
    out = payment_view(sp, c)
    out["invoices"] = [invoice_view(i) for i in db.execute(select(Invoice).where(Invoice.scheduled_payment_id == sp.id)
                                                           .order_by(Invoice.created_at)).scalars()]
    gds = db.execute(select(GovernanceDecision).join(RiskAssessment).where(
        RiskAssessment.resource_type == "scheduled_payment", RiskAssessment.resource_id == sp.id)
        .order_by(GovernanceDecision.created_at.desc())).scalars().all()
    if p.has("risk:read"):
        out["decisions"] = [decision_view(g, db.get(PolicyVersion, g.risk_assessment.policy_version_id).version) for g in gds[:10]]
    else:  # contractors: outcome and plain-language reasons only
        out["decisions"] = [{"decision": g.decision, "reasons": g.reasons, "required_next_actions": g.required_next_actions,
                             "created_at": g.created_at.isoformat()} for g in gds[:5]]
    attempt_ids = [a.id for a in sp.attempts]
    out["provider_events"] = [{"event_id": e.event_id, "event_type": e.event_type, "outcome": e.outcome, "detail": e.detail,
                               "received_at": e.received_at.isoformat()}
                              for e in db.execute(select(PaymentProviderEvent).where(PaymentProviderEvent.payment_attempt_id.in_(attempt_ids))
                                                  .order_by(PaymentProviderEvent.received_at)).scalars()] if attempt_ids and p.has("payments:read_org") else []
    return out


@router.post("/payments/{payment_id}/invoices", status_code=201)
def submit_invoice(payment_id: str, body: InvoiceIn, p: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict:
    inv = ben_svc.submit_invoice(db, p, payment_id, body.invoice_number, body.amount, body.currency)
    db.commit()
    return invoice_view(inv)


@router.get("/my/monthly-terms")
def my_monthly_terms(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict]:
    from finguard.services.invoicing import monthly_terms

    return monthly_terms(db, p)


@router.get("/my/invoices")
def my_invoices(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    from finguard.services.invoicing import invoice_overview

    return invoice_overview(db, p)


@router.get("/my/claimable")
def my_claimable(month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"), p: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> dict:
    from finguard.services.invoicing import claimable

    return claimable(db, p, month)


@router.get("/payments/{payment_id}/invoice-draft")
def invoice_draft(payment_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    from finguard.services.invoicing import draft

    return draft(db, p, payment_id)


@router.get("/payments/{payment_id}/invoice.pdf")
def invoice_pdf(payment_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    from fastapi import Response

    from finguard.services.invoicing import draft, render_pdf

    d = draft(db, p, payment_id)
    return Response(render_pdf(d), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{d["invoice_number"]}.pdf"', "Cache-Control": "private, no-store"})


@router.post("/payments/{payment_id}/claim", status_code=201)
def claim_invoice(payment_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    from finguard.services.invoicing import claim

    inv = claim(db, p, payment_id)
    db.commit()
    return invoice_view(inv) | {"document_id": str(inv.document_id), "release": getattr(inv, "release", None)}


@router.post("/payments/{payment_id}/invoice-upload", status_code=201)
async def upload_invoice(payment_id: str, file: UploadFile = File(...), invoice_number: str | None = Form(None, max_length=64),
                         amount: str | None = Form(None, max_length=32), p: Principal = Depends(current_principal),
                         db: Session = Depends(get_db)) -> dict:
    """Contractor uploads their own invoice (PDF/PNG/JPEG); it is read, checked and routed like a generated invoice."""
    from finguard.core.config import get_settings
    from finguard.core.errors import ValidationFailed
    from finguard.services.invoice_upload import upload_invoice as upload_svc

    if not hit(f"invupload:{p.user_id}", 20, 3600):
        raise RateLimited("upload limit reached")
    limit = get_settings().max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed("file too large")
    inv = upload_svc(db, p, payment_id, file.filename or "invoice", data, invoice_number, amount)
    db.commit()
    return invoice_view(inv) | {"release": getattr(inv, "release", None)}


@router.post("/invoices/{invoice_id}/review")
def review_invoice(invoice_id: str, body: VerifyIn, p: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict:
    inv = ben_svc.review_invoice(db, p, invoice_id, body.approve, body.note)
    db.commit()
    return invoice_view(inv)


@router.post("/payments/{payment_id}/retry")
def retry(payment_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    decision = svc.retry_failed(db, p, payment_id)
    db.commit()
    sp = svc.get_payment(db, p, payment_id)
    return {"decision": decision, "payment": payment_view(sp)}


@router.post("/payments/{payment_id}/reconcile")
def reconcile(payment_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "payments:reconcile")
    sp = svc.get_payment(db, p, payment_id)
    open_attempts = [a for a in sp.attempts if a.status in ("UNKNOWN", "PENDING", "CREATED")]
    if not open_attempts:
        raise NotFound("no attempt awaiting reconciliation")
    svc.reconcile_attempt(db, p, open_attempts[-1])
    db.refresh(sp)
    return payment_view(svc.get_payment(db, p, payment_id))


@router.post("/webhooks/payouts/{provider}")
async def payout_webhook(provider: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Provider webhook. Authenticated ONLY by the HMAC signature over the raw body; idempotent by event id."""
    ip = request.client.host if request.client else "unknown"
    if not hit(f"webhook:{ip}", 300, 60):
        raise RateLimited("webhook rate limit")
    raw = await request.body()
    if len(raw) > 256 * 1024:
        return {"outcome": "REJECTED", "detail": "payload too large"}
    return svc.handle_webhook(db, provider, raw, dict(request.headers))


@router.get("/reconciliation")
def reconciliation(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "payments:read_org")
    attempts = db.execute(select(PaymentAttempt).where(PaymentAttempt.org_id == p.org_id)
                          .order_by(PaymentAttempt.created_at.desc()).limit(200)).scalars().all()
    summary: dict[str, int] = {}
    for a in attempts:
        summary[a.status] = summary.get(a.status, 0) + 1
    from finguard.api.serializers import attempt_view

    return {"summary": summary, "attempts": [attempt_view(a) | {"scheduled_payment_id": str(a.scheduled_payment_id)}
                                             for a in attempts]}
