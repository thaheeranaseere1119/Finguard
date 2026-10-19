"""Contractor invoicing: draft an invoice for a scheduled payment from the APPROVED contract terms, render it as PDF,
and submit it as a claim. The amount is always the scheduled (approved) amount — never typed by the contractor.

A claim creates an Invoice with the generated PDF stored privately as a contract document, then validate_and_route():
  * every essential check passes (verified contractor/payout account, active contract, billable period, amount and
    currency equal the approved schedule, period not already paid) → accepted by rule, payment released at once to the
    risk engine (ACT pays now; large/unusual payments still go to approval);
  * a price increase (invoice above the schedule, or the scheduled amount above the last paid amount) → stays
    SUBMITTED for a finance analyst; on acceptance the payment is released at once;
  * any other failed check → finance analyst review.
A payout-account change is escalated by the risk engine itself (PAY_BENEFICIARY_UNCHANGED).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.errors import Conflict, InvalidState, NotFound
from finguard.documents import storage
from finguard.models import Beneficiary, Contract, ContractDocument, ContractVersion, Invoice, Organization, ScheduledPayment, User
from finguard.models.enums import ContractStatus, InvoiceStatus, RoleName, ScheduledPaymentStatus as SPS
from finguard.services.audit import AuditService
from finguard.services.notifications import notify_roles

CLOSED = (SPS.PAID, SPS.SUPERSEDED, SPS.CANCELLED)


def previous_month(today: date | None = None) -> tuple[date, date]:
    t = today or date.today()
    end = t.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def _owned_payment(db: Session, actor: Principal, sp_id: Any) -> tuple[ScheduledPayment, Contract]:
    sp = abac.load_scoped(db, ScheduledPayment, sp_id, actor)
    c = db.get(Contract, sp.contract_id)
    abac.ensure_owner(actor, c)
    return sp, c


def invoice_number(c: Contract, sp: ScheduledPayment) -> str:
    ref = (c.external_ref or str(c.id)[:8]).upper().replace(" ", "")
    return f"INV-{ref}-{sp.period_end:%Y%m}"


def draft(db: Session, actor: Principal, sp_id: Any) -> dict:
    abac.require(actor, "invoices:submit_own")
    sp, c = _owned_payment(db, actor, sp_id)
    v = db.get(ContractVersion, sp.contract_version_id)
    org = db.get(Organization, c.org_id)
    contractor = db.get(User, c.contractor_id)
    ben = db.get(Beneficiary, c.beneficiary_id) if c.beneficiary_id else None
    existing = db.execute(select(Invoice).where(Invoice.scheduled_payment_id == sp.id,
                                                Invoice.status != InvoiceStatus.REJECTED)).scalars().first()
    return {
        "invoice_number": existing.invoice_number if existing else invoice_number(c, sp),
        "invoice_date": date.today().isoformat(),
        "seller": {"name": v.contractor_legal_name or contractor.full_name, "company": contractor.company_name,
                   "email": contractor.email},
        "buyer": {"name": org.name},
        "contract": {"id": str(c.id), "title": c.title, "reference": c.external_ref, "version": v.version_no},
        "scheduled_payment_id": str(sp.id), "sequence_no": sp.sequence_no,
        "period_start": sp.period_start.isoformat(), "period_end": sp.period_end.isoformat(),
        "description": f"Professional services for {sp.period_start:%d %b %Y} – {sp.period_end:%d %b %Y} as per contract "
                       f"{c.external_ref or c.title} (payment #{sp.sequence_no})",
        "amount": str(sp.amount), "currency": sp.currency, "amount_source": "approved contract version (not editable)",
        "due_date": sp.due_date.isoformat(), "payment_status": sp.status,
        "payout_account": f"{ben.account_holder_name} · XXXX{ben.account_last4} · {ben.ifsc}" if ben else None,
        "notes": ["Tax deducted at source (TDS), where applicable, is withheld by the payer at payment time.",
                  "This invoice was generated from the approved contract terms recorded in FinGuard AI."],
        "existing_invoice": ({"id": str(existing.id), "status": existing.status, "number": existing.invoice_number,
                              "document_id": str(existing.document_id) if existing.document_id else None,
                              "review_note": existing.review_note} if existing else None),
        "claimable": existing is None and sp.status not in CLOSED and c.status in (ContractStatus.ACTIVE, ContractStatus.SUSPENDED)
                     and _period_billable(v, sp),
        "billable_from": (sp.period_start if v.payment_timing == "ADVANCE" else sp.period_end + timedelta(days=1)).isoformat(),
    }


def _period_billable(v: ContractVersion, sp: ScheduledPayment) -> bool:
    """Arrears contracts are invoiced after the service period ends; advance contracts from the period start."""
    today = date.today()
    return sp.period_start <= today if v.payment_timing == "ADVANCE" else sp.period_end < today


def _money(amount: str, cur: str) -> str:
    d = Decimal(amount)
    whole, frac = f"{d:.2f}".split(".")
    if cur == "INR" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return f"{cur} {whole}.{frac}"


def render_pdf(d: dict) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    font = None
    for path in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            open(path, "rb").close()
            font = path
            break
        except OSError:
            continue

    def t(x, y, s, size=10, bold=False, color=(0.1, 0.12, 0.16)):
        kw = {"fontsize": size, "color": color}
        if font:
            kw.update(fontname="F", fontfile=font)
        page.insert_text((x, y), s, **kw)

    page.draw_rect(fitz.Rect(0, 0, 595, 70), color=None, fill=(0.16, 0.34, 0.89))
    t(40, 45, "INVOICE", 22, color=(1, 1, 1))
    t(300, 35, f"Invoice no: {d['invoice_number']}", 10, color=(1, 1, 1))
    t(300, 52, f"Invoice date: {d['invoice_date']}", 10, color=(1, 1, 1))
    y = 110
    t(40, y, "From (contractor)", 9, color=(0.4, 0.45, 0.5))
    t(320, y, "Bill to", 9, color=(0.4, 0.45, 0.5))
    t(40, y + 16, d["seller"]["name"], 12)
    t(320, y + 16, d["buyer"]["name"], 12)
    if d["seller"].get("company"):
        t(40, y + 32, d["seller"]["company"], 10)
    t(40, y + 48, d["seller"]["email"], 9)
    t(320, y + 32, f"Contract: {d['contract']['reference'] or d['contract']['title']} (v{d['contract']['version']})", 9)
    y = 210
    page.draw_rect(fitz.Rect(40, y, 555, y + 24), color=None, fill=(0.95, 0.96, 0.98))
    t(48, y + 16, "Description", 9, color=(0.4, 0.45, 0.5))
    t(470, y + 16, "Amount", 9, color=(0.4, 0.45, 0.5))
    import textwrap

    yy = y + 44
    for line in textwrap.wrap(d["description"], 70):
        t(48, yy, line, 10)
        yy += 14
    t(440, y + 44, _money(d["amount"], d["currency"]), 11)
    yy += 10
    page.draw_line(fitz.Point(40, yy), fitz.Point(555, yy), color=(0.85, 0.87, 0.9))
    t(360, yy + 22, "Total due", 11)
    t(440, yy + 22, _money(d["amount"], d["currency"]), 13)
    yy += 60
    t(40, yy, f"Service period: {d['period_start']} to {d['period_end']}", 10)
    t(40, yy + 16, f"Payment due: {d['due_date']}  ·  Schedule payment #{d['sequence_no']}", 10)
    if d.get("payout_account"):
        t(40, yy + 32, f"Pay to: {d['payout_account']}", 10)
    yy += 64
    for n in d["notes"]:
        t(40, yy, f"• {n}", 8, color=(0.4, 0.45, 0.5))
        yy += 13
    t(40, 810, "Generated by FinGuard AI from approved contract terms · amount is not editable", 7, color=(0.55, 0.58, 0.62))
    try:
        doc.subset_fonts()   # embed only the glyphs used (keeps the PDF small)
    except Exception:
        pass
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


def claim(db: Session, actor: Principal, sp_id: Any) -> Invoice:
    """Submit the generated invoice for a scheduled payment (one active invoice per payment)."""
    d = draft(db, actor, sp_id)
    sp, c = _owned_payment(db, actor, sp_id)
    if d["existing_invoice"]:
        raise Conflict(f"invoice {d['existing_invoice']['number']} was already submitted ({d['existing_invoice']['status']})")
    if not d["claimable"]:
        if sp.status not in CLOSED and date.fromisoformat(d["billable_from"]) > date.today():
            raise InvalidState(f"this period can be invoiced from {d['billable_from']} (after the service period ends)")
        raise InvalidState(f"payment is {sp.status}; it cannot be invoiced")
    pdf = render_pdf(d)
    upload = storage.validate_upload(f"{d['invoice_number']}.pdf", pdf)
    key = storage.store(actor.org_id, upload)
    doc = ContractDocument(org_id=actor.org_id, contract_id=c.id, kind="INVOICE", original_filename=upload.safe_filename,
                           storage_key=key, mime_type=upload.mime_type, size_bytes=len(pdf), sha256=upload.sha256,
                           page_count=upload.page_count, uploaded_by=actor.user_id)
    db.add(doc)
    db.flush()
    inv = Invoice(org_id=c.org_id, contract_id=c.id, scheduled_payment_id=sp.id, invoice_number=d["invoice_number"],
                  amount=sp.amount, currency=sp.currency, document_id=doc.id, submitted_by=actor.user_id,
                  status=InvoiceStatus.SUBMITTED)
    db.add(inv)
    db.flush()
    AuditService(db).record("invoice.claimed", actor=actor, resource_type="invoice", resource_id=inv.id, contract_id=c.id,
                            new_state=InvoiceStatus.SUBMITTED,
                            payload={"number": inv.invoice_number, "amount": str(inv.amount), "currency": inv.currency,
                                     "scheduled_payment_id": str(sp.id), "period": f"{sp.period_start}..{sp.period_end}",
                                     "document_sha256": doc.sha256, "amount_source": "approved schedule"})
    inv.release = validate_and_route(db, inv, sp, c)
    return inv


def validate_checks(db: Session, inv: Invoice, sp: ScheduledPayment, c: Contract) -> list[dict]:
    """Is this a VALID invoice? (The claim amount itself is not judged here — the risk engine routes it by tier.)"""
    from finguard.services.payments import version_for_period

    v = version_for_period(db, c.id, sp.period_start)
    already = db.execute(select(ScheduledPayment.id).where(
        ScheduledPayment.contract_id == c.id, ScheduledPayment.period_start == sp.period_start,
        ScheduledPayment.status == SPS.PAID)).first()

    def chk(name, ok, why, kind="essential"):
        return {"check": name, "passed": bool(ok), "detail": None if ok else why, "kind": kind}

    return [
        chk("contract active", c.status == ContractStatus.ACTIVE, f"contract is {c.status}"),
        chk("service period billable", v is not None and _period_billable(v, sp), "the service period has not ended yet"),
        chk("currency matches the contract", inv.currency == sp.currency, f"invoice currency {inv.currency} ≠ {sp.currency}"),
        chk("period not already paid", already is None, "this service period was already paid"),
        chk("invoice document attached", inv.document_id is not None,
            "no invoice document — a typed amount alone is not proof of the claim"),
        chk("positive amount", Decimal(inv.amount) > 0, "invoice amount must be positive"),
    ]


def claim_tier(db: Session, inv: Invoice, sp: ScheduledPayment, c: Contract, valid: bool) -> dict:
    """Which scenario this claim falls into (for the contractor's message; the risk engine makes the decision)."""
    from finguard.governance.service import active_policy
    from finguard.services.common import service_principal
    from finguard.services.payments import build_snapshot

    pv = active_policy(db, c.org_id)
    snap = build_snapshot(db, service_principal(db, c.org_id, RoleName.SCHEDULER), sp, pv.config)
    tol = Decimal(str((pv.config.get("invoice_tiers") or {}).get("approve_tolerance_pct", "10")))
    terms, claim = snap.expected_amount, Decimal(inv.amount)
    pct = ((claim - terms) * 100 / terms).quantize(Decimal("0.1")) if terms > 0 else Decimal(0)
    reasons = []
    if not valid:
        reasons.append("the invoice failed validation")
    if snap.beneficiary_changed:
        reasons.append("your payout account changed")
    if claim > terms * (1 + tol / 100):
        reasons.append(f"the claim is {pct}% above your contract amount (more than {tol}%)")
    if reasons:
        return {"scenario": 3, "outcome": "ESCALATE", "label": "Escalated to risk review", "reasons": reasons,
                "claim": str(claim), "contract_amount": str(terms), "over_pct": str(pct)}
    if claim > terms:
        return {"scenario": 2, "outcome": "APPROVE", "label": f"Needs approver ({pct}% above contract)", "reasons":
                [f"the claim is {pct}% above your contract amount (within {tol}%)"], "claim": str(claim),
                "contract_amount": str(terms), "over_pct": str(pct)}
    return {"scenario": 1, "outcome": "ACT", "label": "Within contract — paid automatically", "reasons": [],
            "claim": str(claim), "contract_amount": str(terms), "over_pct": str(pct)}


def validate_and_route(db: Session, inv: Invoice, sp: ScheduledPayment, c: Contract,
                       extra_checks: list[dict] | None = None) -> dict:
    """Validate the invoice (ACCEPTED if valid, FLAGGED if not — never an analyst queue) and hand the payment to the
    risk engine, which applies the claim tiers: ≤ contract → paid; ≤ +10% → approver; otherwise → risk officer."""
    from finguard.governance.service import active_policy
    from finguard.services.common import service_principal

    checks = validate_checks(db, inv, sp, c) + list(extra_checks or [])
    failed = [x for x in checks if not x["passed"]]
    system = service_principal(db, c.org_id, RoleName.SCHEDULER)
    AuditService(db).record("invoice.validated", resource_type="invoice", resource_id=inv.id, contract_id=c.id, org_id=c.org_id,
                            payload={"checks": checks, "scheduled_payment_id": str(sp.id)})
    pv = active_policy(db, c.org_id)
    if not pv.config.get("invoice_auto_accept", True):          # automatic validation switched off → manual review
        notify_roles(db, c.org_id, [RoleName.FINANCE_ANALYST], "invoice.submitted", "Invoice awaiting review",
                     f"{inv.invoice_number}: {inv.amount} {inv.currency} for {c.title}", f"/payments/{sp.id}")
        return {"auto_accepted": False, "checks": checks, "payment_status": sp.status,
                "message": "Submitted — automatic validation is switched off, so finance will review it."}
    if failed:
        inv.status = InvoiceStatus.FLAGGED
        inv.review_note = "Failed automatic validation: " + "; ".join(x["detail"] for x in failed)
    else:
        inv.status = InvoiceStatus.ACCEPTED
        inv.review_note = "Valid invoice: all automatic checks passed."
    AuditService(db).record("invoice.auto_validated", actor=system, resource_type="invoice", resource_id=inv.id,
                            contract_id=c.id, new_state=inv.status,
                            payload={"failed": [x["check"] for x in failed], "amount": str(inv.amount)})
    tier = claim_tier(db, inv, sp, c, valid=not failed)
    rel = release_payment(db, sp, c, inv, system, tier=tier)
    return {"auto_accepted": not failed, "valid": not failed, "checks": checks, "tier": tier,
            "reason": "; ".join(x["detail"] for x in failed) or None, **rel}


def release_payment(db: Session, sp: ScheduledPayment, c: Contract, inv: Invoice, actor: Principal,
                    tier: dict | None = None) -> dict:
    """Claim submitted → re-queue this period's payment and, if it is due, let the risk engine decide now."""
    from finguard.services.common import service_principal
    from finguard.services.notifications import notify_user
    from finguard.services.payments import _set_status, govern_and_route

    system = service_principal(db, c.org_id, RoleName.SCHEDULER)
    if sp.status == SPS.HELD:                              # only the period this invoice covers
        _set_status(db, actor, sp, SPS.SCHEDULED, f"re-queued: invoice {inv.invoice_number} submitted")
    decision = None
    if sp.status == SPS.SCHEDULED and sp.due_at <= datetime.now(timezone.utc):
        db.flush()
        decision = govern_and_route(db, system, sp)
    db.refresh(sp)
    why = "; ".join((tier or {}).get("reasons") or [])
    due = f"{sp.due_date:%d %b %Y}"
    msg = {"ACT": "Valid claim within your contract amount — payment released to your verified payout account.",
           "APPROVE": "Waiting for a finance approver" + (f" ({why})" if why else "") + ". You'll be paid once they approve.",
           "ESCALATE": "Sent to risk review" + (f" because {why}" if why else f": {sp.hold_reason or ''}") + ".",
           "VERIFY": f"Payment on hold: {sp.hold_reason or 'verification needed'}.",
           "BLOCK": f"Payment blocked: {sp.hold_reason or 'a control failed'}."}.get(decision)
    if decision is None:
        scen = (tier or {}).get("scenario")
        msg = {1: f"Valid claim within your contract amount — it will be paid automatically on the due date ({due}).",
               2: f"On the due date ({due}) this goes to a finance approver ({why}).",
               3: f"On the due date ({due}) this goes to risk review because {why}."}.get(scen,
               f"Payment will be processed on its due date ({due}).")
    notify_user(db, c.org_id, c.contractor_id, "invoice.reviewed", "Invoice received", f"{inv.invoice_number}: {msg}",
                f"/payments/{sp.id}")
    return {"decision": decision, "payment_status": sp.status, "message": msg}


def claimable(db: Session, actor: Principal, month: str | None = None) -> dict:
    """The contractor's scheduled payments covering a calendar month (default: last month), with invoice state."""
    abac.require(actor, "invoices:submit_own")
    if month:
        y, m = (int(x) for x in month.split("-"))
        a = date(y, m, 1)
        b = date(y, m, calendar.monthrange(y, m)[1])
    else:
        a, b = previous_month()
    rows = db.execute(select(ScheduledPayment).join(Contract, Contract.id == ScheduledPayment.contract_id).where(
        Contract.org_id == actor.org_id, Contract.contractor_id == actor.user_id,
        ScheduledPayment.status.not_in([SPS.SUPERSEDED, SPS.CANCELLED]),
        ScheduledPayment.period_start <= b, ScheduledPayment.period_end >= a).order_by(ScheduledPayment.period_start)).scalars().all()
    items = [draft(db, actor, sp.id) for sp in rows]
    return {"month": f"{a:%Y-%m}", "month_label": f"{a:%B %Y}", "items": items}


def invoice_overview(db: Session, actor: Principal) -> dict:
    """Everything a contractor needs on the "Submit invoice" page: periods ready to invoice (oldest first), invoices
    already submitted (with their review and payment state) and the next period that will become billable."""
    abac.require(actor, "invoices:submit_own")
    today = date.today()
    rows = db.execute(select(ScheduledPayment).join(Contract, Contract.id == ScheduledPayment.contract_id).where(
        Contract.org_id == actor.org_id, Contract.contractor_id == actor.user_id,
        ScheduledPayment.status.not_in([SPS.SUPERSEDED, SPS.CANCELLED]),
        ScheduledPayment.period_start <= today + timedelta(days=62)).order_by(ScheduledPayment.period_start)).scalars().all()
    ready, submitted, upcoming = [], [], []
    for sp in rows:
        d = draft(db, actor, sp.id)
        if d["existing_invoice"]:
            submitted.append(d)
        elif d["claimable"]:
            ready.append(d)
        elif sp.status not in CLOSED and d["billable_from"] > today.isoformat():
            upcoming.append(d)
    return {"ready": ready, "submitted": list(reversed(submitted)), "upcoming": upcoming[:1]}


def monthly_terms(db: Session, actor: Principal) -> list[dict]:
    """Per active contract: the approved amount paid each period and the next scheduled payment."""
    abac.require(actor, "contracts:read_own")
    out = []
    for c in db.execute(select(Contract).where(Contract.org_id == actor.org_id, Contract.contractor_id == actor.user_id,
                                               Contract.status.in_([ContractStatus.ACTIVE, ContractStatus.SUSPENDED]))).scalars().unique():
        v = db.get(ContractVersion, c.current_version_id)
        nxt = db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id,
                                                        ScheduledPayment.status.not_in([SPS.PAID, SPS.SUPERSEDED, SPS.CANCELLED]),
                                                        ScheduledPayment.due_date >= date.today())
                         .order_by(ScheduledPayment.due_date)).scalars().first()
        out.append({"contract_id": str(c.id), "title": c.title, "reference": c.external_ref, "status": c.status,
                    "amount": str(v.payment_amount), "currency": v.currency, "frequency": v.frequency,
                    "due_day_of_month": v.due_day_of_month, "payment_timing": v.payment_timing,
                    "invoice_required": v.invoice_required, "start_date": v.start_date.isoformat(), "end_date": v.end_date.isoformat(),
                    "version_no": v.version_no,
                    "next_payment": ({"id": str(nxt.id), "due_date": nxt.due_date.isoformat(), "amount": str(nxt.amount),
                                      "status": nxt.status} if nxt else None)})
    return out
