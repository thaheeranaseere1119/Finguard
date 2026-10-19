"""Beneficiaries (payout destinations) and invoices.

The full account number is passed to the payout provider once to create a fund account and is never stored;
only the provider reference and the last four digits are kept. A finance analyst (not the contractor) must verify
a beneficiary before any payout can be executed to it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.errors import Conflict, InvalidState, ProviderError, ValidationFailed
from finguard.extraction import parsers as P
from finguard.models import Beneficiary, Contract, Invoice, ScheduledPayment, User
from finguard.models.enums import ContractStatus, InvoiceStatus, RoleName, ScheduledPaymentStatus
from finguard.payments.providers import get_payout_provider
from finguard.payments.providers.base import ProviderError as PayoutProviderError
from finguard.services.audit import AuditService
from finguard.services.notifications import notify_roles, notify_user

IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def register_beneficiary(db: Session, actor: Principal, contract_id: Any, holder: str, account_number: str,
                         ifsc: str) -> Beneficiary:
    from finguard.services.contracts import get_contract

    abac.require(actor, "beneficiaries:manage_own")
    c = get_contract(db, actor, contract_id)
    abac.ensure_owner(actor, c)
    digits = re.sub(r"\D", "", account_number or "")
    if getattr(get_payout_provider(), "bank_code_kind", "IFSC") == "ABA":     # US payouts (Dwolla): ABA routing number
        from finguard.payments.providers.dwolla import aba_valid

        if not 4 <= len(digits) <= 17:
            raise ValidationFailed("US bank account number must be 4–17 digits")
        ifsc = re.sub(r"\D", "", ifsc or "")
        if not aba_valid(ifsc):
            raise ValidationFailed("invalid US routing number (9 digits, ABA checksum)")
    else:
        if not 5 <= len(digits) <= 35:
            raise ValidationFailed("account number must be 5–35 digits")
        ifsc = (ifsc or "").strip().upper()
        if not IFSC_RE.match(ifsc):
            raise ValidationFailed("invalid IFSC code")
    if not 3 <= len(holder.strip()) <= 120:
        raise ValidationFailed("account holder name must be 3–120 characters")
    user = db.get(User, actor.user_id)
    try:
        contact_id, fa_id = get_payout_provider().register_beneficiary(
            name=holder.strip(), email=user.email, contractor_ref=str(actor.user_id)[:40], account_number=digits, ifsc=ifsc)
    except PayoutProviderError as exc:
        raise ProviderError(f"payout provider rejected the beneficiary: {exc}") from exc
    if c.beneficiary_id:
        old = db.get(Beneficiary, c.beneficiary_id)
        old.is_active = False
    b = Beneficiary(org_id=actor.org_id, contractor_id=actor.user_id, account_holder_name=holder.strip(), ifsc=ifsc,
                    account_last4=digits[-4:], provider=get_payout_provider().name, provider_contact_id=contact_id,
                    provider_fund_account_id=fa_id, verified=False)
    db.add(b)
    db.flush()
    c.beneficiary_id = b.id
    AuditService(db).record("beneficiary.registered", actor=actor, resource_type="beneficiary", resource_id=b.id,
                            contract_id=c.id, payload={"last4": b.account_last4, "ifsc": ifsc, "provider": b.provider})
    notify_roles(db, c.org_id, [RoleName.FINANCE_ANALYST], "beneficiary.registered", "Beneficiary awaiting verification",
                 f"{holder} (****{b.account_last4}) for {c.title}", f"/contracts/{c.id}")
    return b


def verify_beneficiary(db: Session, actor: Principal, beneficiary_id: Any, approve: bool, note: str) -> Beneficiary:
    abac.require(actor, "beneficiaries:verify")
    b = abac.load_scoped(db, Beneficiary, beneficiary_id, actor)
    abac.ensure_not_self(actor, b.contractor_id, what="verify your own beneficiary")
    if approve:
        b.verified, b.verified_by, b.verified_at = True, actor.user_id, datetime.now(timezone.utc)
    else:
        b.is_active = False
    AuditService(db).record("beneficiary.verified" if approve else "beneficiary.rejected", actor=actor,
                            resource_type="beneficiary", resource_id=b.id, payload={"note": note, "last4": b.account_last4})
    c = db.execute(select(Contract).where(Contract.beneficiary_id == b.id)).scalars().first()
    if c and approve:
        from finguard.services.payments import requeue_held

        requeue_held(db, c.id, "beneficiary verified", actor)
    notify_user(db, b.org_id, b.contractor_id, "beneficiary.reviewed",
                "Beneficiary verified" if approve else "Beneficiary rejected", note, f"/contracts/{c.id}" if c else None)
    return b


def submit_invoice(db: Session, actor: Principal, sp_id: Any, invoice_number: str, amount: str, currency: str) -> Invoice:
    abac.require(actor, "invoices:submit_own")
    sp = abac.load_scoped(db, ScheduledPayment, sp_id, actor)
    c = db.get(Contract, sp.contract_id)
    abac.ensure_owner(actor, c)
    if c.status not in (ContractStatus.ACTIVE, ContractStatus.SUSPENDED):
        raise InvalidState("invoices can be submitted only for active contracts")
    if sp.status in (ScheduledPaymentStatus.PAID, ScheduledPaymentStatus.SUPERSEDED, ScheduledPaymentStatus.CANCELLED):
        raise InvalidState(f"payment is {sp.status}")
    try:
        amt = P.parse_amount_text(amount)
        cur = P.normalize_currency(currency)
    except P.ParseError as exc:
        raise ValidationFailed(str(exc)) from exc
    number = invoice_number.strip()[:64]
    if not number:
        raise ValidationFailed("invoice number required")
    if db.execute(select(Invoice.id).where(Invoice.contract_id == c.id, Invoice.invoice_number == number)).first():
        raise ValidationFailed("invoice number already used for this contract")
    active = db.execute(select(Invoice).where(Invoice.scheduled_payment_id == sp.id,
                                              Invoice.status != InvoiceStatus.REJECTED)).scalars().first()
    if active:   # one active claim per payment: a second invoice must never alter a claim already in review/approval
        raise Conflict(f"invoice {active.invoice_number} is already {active.status.lower()} for this period")
    inv = Invoice(org_id=c.org_id, contract_id=c.id, scheduled_payment_id=sp.id, invoice_number=number, amount=amt,
                  currency=cur, submitted_by=actor.user_id, status=InvoiceStatus.SUBMITTED)
    db.add(inv)
    db.flush()
    AuditService(db).record("invoice.submitted", actor=actor, resource_type="invoice", resource_id=inv.id,
                            contract_id=c.id, payload={"number": number, "amount": str(amt), "currency": cur,
                                                       "scheduled_payment_id": str(sp.id),
                                                       "matches_schedule": amt == Decimal(sp.amount) and cur == sp.currency})
    from finguard.services.invoicing import validate_and_route

    inv.release = validate_and_route(db, inv, sp, c)
    return inv


def review_invoice(db: Session, actor: Principal, invoice_id: Any, accept: bool, note: str) -> Invoice:
    abac.require(actor, "invoices:review")
    inv = abac.load_scoped(db, Invoice, invoice_id, actor)
    c = db.get(Contract, inv.contract_id)
    abac.ensure_not_self(actor, c.contractor_id, inv.submitted_by, what="review your own invoice")
    sp_now = db.get(ScheduledPayment, inv.scheduled_payment_id)
    withdrawable = (not accept and inv.status == InvoiceStatus.ACCEPTED
                    and sp_now.status not in ("PAID", "SUBMITTED", "PROCESSING", "APPROVED_FOR_EXECUTION"))
    if inv.status not in (InvoiceStatus.SUBMITTED, InvoiceStatus.FLAGGED) and not withdrawable:
        raise InvalidState(f"invoice is {inv.status}")
    inv.status = InvoiceStatus.ACCEPTED if accept else InvoiceStatus.REJECTED
    inv.reviewed_by, inv.review_note = actor.user_id, note
    AuditService(db).record("invoice.reviewed", actor=actor, resource_type="invoice", resource_id=inv.id,
                            contract_id=c.id, new_state=inv.status, payload={"note": note})
    sp = db.get(ScheduledPayment, inv.scheduled_payment_id)
    if accept:
        # Accepting confirms the invoice is genuine; it does NOT approve the amount. The risk engine still routes the
        # claim by tier (≤ contract → paid, ≤ +10% → approver, more → risk officer) — an analyst cannot raise a payout.
        from finguard.services.invoicing import claim_tier, release_payment

        inv.release = release_payment(db, sp, c, inv, actor, tier=claim_tier(db, inv, sp, c, valid=True))
    else:
        if sp.status in ("SCHEDULED", "ESCALATED", "AWAITING_APPROVAL"):
            from datetime import datetime, timezone

            from finguard.models import ApprovalRequest
            from finguard.services.payments import _set_status

            for ar in db.execute(select(ApprovalRequest).where(ApprovalRequest.resource_type == "scheduled_payment",
                                                               ApprovalRequest.resource_id == sp.id,
                                                               ApprovalRequest.status == "OPEN")).scalars():
                ar.status, ar.resolution_reason = "CANCELLED", f"invoice {inv.invoice_number} rejected"
                ar.resolved_at = datetime.now(timezone.utc)
            _set_status(db, actor, sp, "HELD", f"invoice {inv.invoice_number} rejected: {note}; submit a corrected invoice")
        notify_user(db, c.org_id, c.contractor_id, "invoice.reviewed", "Invoice rejected", note,
                    f"/payments/{inv.scheduled_payment_id}")
    return inv
