"""Recurring payment execution with governance, idempotency, reconciliation and webhooks.

Per due occurrence:
  1. claim it (row lock, SKIP LOCKED) → PROCESSING; commit
  2. reload the contract and the approved version effective for the period
  3. build a PaymentSnapshot (contract status/dates, duplicates, amount re-derived from approved terms, invoice,
     beneficiary, pending amendment, cumulative cap, provider state, actor authority) and assess it
  4. ACT → payout · APPROVE → approval request · ESCALATE → compliance request · VERIFY → hold · BLOCK → blocked/reverted
Payout execution persists a CREATED attempt (durable intent, unique idempotency key) and COMMITS before calling the
provider. A timeout yields UNKNOWN: no retry happens until reconciliation establishes the outcome.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.config import get_settings
from finguard.core.errors import Forbidden, InvalidState, NotFound
from finguard.core.money import from_minor_units, to_minor_units
from finguard.governance.assessors import PaymentSnapshot, assess_payment
from finguard.governance.policy import to_base
from finguard.governance.service import active_policy, record_assessment
from finguard.models import (ApprovalRequest, Beneficiary, Contract, ContractAmendment, ContractDocument,
                             ContractVersion, ExtractionResult, Invoice, Organization, PaymentAttempt,
                             PaymentProviderEvent, ScheduledPayment)
from finguard.models.enums import (AmendmentStatus, ApprovalKind, ApprovalStatus, AttemptStatus as AS, ContractStatus as CS,
                                   Decision, RoleName, ScheduledPaymentStatus as SPS, VersionStatus)
from finguard.payments.providers import get_payout_provider
from finguard.payments.providers.base import (PayoutRequest, ProviderNotSent, ProviderOutcomeUnknown, ProviderRejected,
                                              WebhookEvent)
from finguard.scheduling.generator import expected_amount_for_period
from finguard.services.audit import AuditService
from finguard.services.common import org_calendar, service_principal, terms_of
from finguard.services.notifications import notify_roles, notify_user

log = logging.getLogger(__name__)
CLAIMABLE = (SPS.SCHEDULED, SPS.APPROVED_FOR_EXECUTION)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(db: Session, actor: Principal, event: str, sp: ScheduledPayment, prev: str | None = None,
           new: str | None = None, **payload: Any) -> None:
    AuditService(db).record(event, actor=actor, org_id=sp.org_id, resource_type="scheduled_payment", resource_id=sp.id,
                            contract_id=sp.contract_id, previous_state=prev, new_state=new,
                            payload={"sequence_no": sp.sequence_no, "amount": str(sp.amount), "currency": sp.currency,
                                     "due_date": sp.due_date.isoformat(), "idempotency_key": sp.idempotency_key,
                                     "contract_version_id": str(sp.contract_version_id), **payload})


def _set_status(db: Session, actor: Principal, sp: ScheduledPayment, new: str, reason: str | None = None, **kw) -> None:
    prev = sp.status
    sp.status, sp.updated_at = new, _now()
    if reason is not None:
        sp.hold_reason = reason[:1000]
    _audit(db, actor, "payment.status_changed", sp, prev, new, reason=reason, **kw)


# ------------------------------------------------------------------------------------------- read helpers
def get_payment(db: Session, actor: Principal, sp_id: Any) -> ScheduledPayment:
    sp = abac.load_scoped(db, ScheduledPayment, sp_id, actor)
    c = db.get(Contract, sp.contract_id)
    if actor.has("payments:read_org"):
        return sp
    if actor.has("payments:read_own") and c.contractor_id == actor.user_id:
        return sp
    raise NotFound("resource not found")


def list_payments(db: Session, actor: Principal, *, status: str | None = None, contract_id: str | None = None,
                  due_before: date | None = None, due_after: date | None = None, limit: int = 100,
                  offset: int = 0) -> tuple[list[ScheduledPayment], int]:
    q = select(ScheduledPayment).where(ScheduledPayment.org_id == actor.org_id)
    if not actor.has("payments:read_org"):
        abac.require(actor, "payments:read_own")
        q = q.join(Contract, Contract.id == ScheduledPayment.contract_id).where(Contract.contractor_id == actor.user_id)
    if status:
        q = q.where(ScheduledPayment.status.in_(status.split(",")))
    if contract_id:
        q = q.where(ScheduledPayment.contract_id == uuid.UUID(contract_id))
    if due_before:
        q = q.where(ScheduledPayment.due_date <= due_before)
    if due_after:
        q = q.where(ScheduledPayment.due_date >= due_after)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(ScheduledPayment.due_date, ScheduledPayment.sequence_no)
                      .limit(min(limit, 500)).offset(offset)).scalars().all()
    return list(rows), total


def version_for_period(db: Session, contract_id: uuid.UUID, period_start: date) -> ContractVersion | None:
    """Latest approved version whose effective date covers the period (original version: effective from start)."""
    rows = db.execute(select(ContractVersion).where(ContractVersion.contract_id == contract_id,
                                                    ContractVersion.status.in_([VersionStatus.APPROVED, VersionStatus.SUPERSEDED]),
                                                    ContractVersion.version_no.is_not(None))
                      .order_by(ContractVersion.version_no.desc())).scalars().all()
    for v in rows:
        if (v.effective_from or v.start_date) <= period_start:
            return v
    return rows[-1] if rows else None


# ------------------------------------------------------------------------------------------- snapshot & governance
def _payout_context(db: Session, sp: ScheduledPayment, policy: dict) -> tuple[Decimal, str | None, Invoice | None, Decimal | None]:
    """(amount that would be paid, payee fingerprint, latest active invoice, claim amount).

    For invoice-required contracts with claim tiers on, the payout is the amount the contractor CLAIMED on the latest
    non-rejected invoice (it is then routed by tier). Otherwise it is the scheduled amount from the approved terms."""
    c = db.get(Contract, sp.contract_id)
    invoice = db.execute(select(Invoice).where(Invoice.scheduled_payment_id == sp.id, Invoice.status != "REJECTED")
                         .order_by(Invoice.created_at.desc())).scalars().first()
    ben = db.get(Beneficiary, c.beneficiary_id) if c.beneficiary_id else None
    payee = ben.provider_fund_account_id if ben else None
    v = version_for_period(db, c.id, sp.period_start)
    tiers = policy.get("invoice_tiers") or {"enabled": True}
    claim = None
    if v and v.invoice_required and tiers.get("enabled", True) and invoice is not None and invoice.currency == sp.currency:
        claim = Decimal(invoice.amount)
    return (claim if claim is not None else Decimal(sp.amount)), payee, invoice, claim


def _approval_matches(db: Session, sp: ScheduledPayment, kind: str, amount: Decimal, payee: str | None) -> bool:
    """An approval/clearance counts only while the payment still has the SAME amount and payee it was granted for."""
    rows = db.execute(select(ApprovalRequest).where(
        ApprovalRequest.resource_type == "scheduled_payment", ApprovalRequest.resource_id == sp.id,
        ApprovalRequest.kind == kind, ApprovalRequest.status == ApprovalStatus.APPROVED)).scalars().all()
    # payee_ref is always recorded for new requests; NULL only on rows created before approval binding existed
    return any(ar.amount is not None and Decimal(ar.amount) == Decimal(amount) and (ar.payee_ref is None or ar.payee_ref == payee)
               for ar in rows)


def build_snapshot(db: Session, actor: Principal, sp: ScheduledPayment, policy: dict, *, human_approved: bool = False,
                   now: datetime | None = None) -> PaymentSnapshot:
    now = now or _now()
    c = db.get(Contract, sp.contract_id)
    org = db.get(Organization, c.org_id)
    v = version_for_period(db, c.id, sp.period_start)
    current = db.get(ContractVersion, c.current_version_id) if c.current_version_id else None
    expected = expected_amount_for_period(terms_of(v), sp.period_start, org_calendar(org)) if v else None
    attempts = list(db.execute(select(PaymentAttempt).where(PaymentAttempt.scheduled_payment_id == sp.id)).scalars())
    paid_to_date = db.execute(select(func.coalesce(func.sum(PaymentAttempt.amount), 0))
                              .join(ScheduledPayment, ScheduledPayment.id == PaymentAttempt.scheduled_payment_id)
                              .where(ScheduledPayment.contract_id == c.id, PaymentAttempt.status == AS.PROCESSED)).scalar_one()
    payout, payee, invoice, claim = _payout_context(db, sp, policy)
    pending_amend = db.execute(select(ContractAmendment).where(
        ContractAmendment.contract_id == c.id,
        ContractAmendment.status.in_([AmendmentStatus.PENDING_APPROVAL, AmendmentStatus.OCR_PROCESSING]),
        ContractAmendment.requested_effective_date <= sp.period_end)).scalars().first()
    ben = db.get(Beneficiary, c.beneficiary_id) if c.beneficiary_id else None
    last_paid_to = db.execute(select(PaymentAttempt.fund_account_id).join(
        ScheduledPayment, ScheduledPayment.id == PaymentAttempt.scheduled_payment_id).where(
        ScheduledPayment.contract_id == c.id, PaymentAttempt.status == AS.PROCESSED)
        .order_by(PaymentAttempt.created_at.desc()).limit(1)).scalar_one_or_none()
    er = db.get(ExtractionResult, v.source_extraction_id) if v and v.source_extraction_id else None
    min_conf = min((f.extraction_confidence for f in er.fields if f.is_critical and f.value is not None), default=None) if er else None
    from finguard.models import FieldCorrection

    corrections = db.execute(select(func.count()).select_from(FieldCorrection)
                             .where(FieldCorrection.extraction_id == er.id)).scalar_one() if er else 0
    doc = db.get(ContractDocument, c.primary_document_id) if c.primary_document_id else None
    prior_paid = db.execute(select(func.count()).select_from(PaymentAttempt).join(
        ScheduledPayment, ScheduledPayment.id == PaymentAttempt.scheduled_payment_id).where(
        ScheduledPayment.contract_id == c.id, PaymentAttempt.status == AS.PROCESSED)).scalar_one()
    evidence = [{"type": "contract_version", "id": str(v.id) if v else None, "version_no": v.version_no if v else None,
                 "terms_hash": v.terms_hash if v else None},
                {"type": "schedule_occurrence", "id": str(sp.id), "period": f"{sp.period_start}..{sp.period_end}",
                 "expected_amount": str(expected) if expected is not None else None}]
    if doc:
        evidence.append({"type": "document", "id": str(doc.id), "sha256": doc.sha256})
    if invoice:
        evidence.append({"type": "invoice", "id": str(invoice.id), "number": invoice.invoice_number,
                         "status": invoice.status, "amount": str(invoice.amount)})
    if claim is not None:
        evidence.append({"type": "claim", "invoice_id": str(invoice.id), "claim_amount": str(claim),
                         "contract_amount": str(expected) if expected is not None else None, "payee": payee})
    # payee changed: replaced since the last successful payment, or (no payment yet) added after contract activation
    payee_changed = bool(ben and ((last_paid_to and ben.provider_fund_account_id != last_paid_to) or
                                  (not last_paid_to and c.activated_at and ben.created_at and ben.created_at > c.activated_at)))
    integrity_ok = True
    if doc is not None:
        try:
            from finguard.documents import storage
            from finguard.services.contracts import hashlib_sha256

            integrity_ok = hashlib_sha256(storage.read(doc.storage_key)) == doc.sha256
        except Exception:
            integrity_ok = False
    return PaymentSnapshot(
        amount=Decimal(sp.amount), currency=sp.currency,
        expected_amount=expected if expected is not None else Decimal("-1"),
        expected_currency=v.currency if v else "???",
        amount_base=to_base(payout, sp.currency, policy),
        due_date=sp.due_date, period_start=sp.period_start, period_end=sp.period_end,
        contract_start=(current or v).start_date if (current or v) else sp.period_start,
        contract_end=(current or v).end_date if (current or v) else None,
        contract_status=c.status, is_due=sp.due_at <= now,
        actor_authorized=actor.has("payments:execute_scheduled") or (human_approved and actor.has("payments:approve")),
        same_org=actor.org_id == sp.org_id, actor_is_contractor=actor.user_id == c.contractor_id,
        version_current=v is not None and sp.contract_version_id == v.id,
        already_paid=any(a.status == AS.PROCESSED for a in attempts),
        active_attempt=any(a.status in (AS.CREATED, AS.PENDING) for a in attempts),
        unknown_attempt=any(a.status == AS.UNKNOWN for a in attempts),
        total_value=Decimal(v.total_value) if v and v.total_value is not None else None,
        paid_to_date=Decimal(paid_to_date), invoice_required=bool(v and v.invoice_required),
        invoice_status=invoice.status if invoice else None,
        invoice_amount=Decimal(invoice.amount) if invoice else None,
        pending_amendment_affects=pending_amend is not None,
        beneficiary_present=ben is not None and ben.is_active, beneficiary_verified=bool(ben and ben.verified),
        beneficiary_changed=payee_changed,
        claim_amount=claim, payout_amount=payout,
        claim_cleared=_approval_matches(db, sp, ApprovalKind.ESCALATION, payout, payee),
        auto_execute_permitted=bool(v and v.auto_execute_permitted),
        auto_execute_enabled=bool(policy.get("auto_execute_enabled", True)), human_approval_granted=human_approved,
        extraction_min_confidence=Decimal(min_conf) if min_conf is not None else None, fields_corrected=int(corrections),
        document_integrity_ok=integrity_ok,
        injection_flag=bool(er and (er.injection_report or {}).get("detected")), first_payment=prior_paid == 0,
        prior_failed_attempts=sum(1 for a in attempts if a.status == AS.FAILED), is_prorated=sp.is_prorated,
        trigger="human_approval" if human_approved else ("agent" if actor.has_role(RoleName.AI_AGENT) else "scheduler"),
        evidence_refs=evidence,
    )


def _escalation_cleared(db: Session, sp: ScheduledPayment) -> bool:
    payout, payee, _, _ = _payout_context(db, sp, active_policy(db, sp.org_id).config)
    return _approval_matches(db, sp, ApprovalKind.ESCALATION, payout, payee)


def govern_and_route(db: Session, actor: Principal, sp: ScheduledPayment, *, human_approved: bool = False,
                     now: datetime | None = None) -> str:
    """Assess and route one occurrence. Returns the decision. Caller commits."""
    prior_status = sp.status if sp.status != SPS.PROCESSING else SPS.SCHEDULED
    pv = active_policy(db, sp.org_id)
    payout, payee, _, _ = _payout_context(db, sp, pv.config)
    # a recorded approval only counts if it was granted for exactly this amount and payee (F-01 / F-02)
    approved = bool(human_approved) and _approval_matches(db, sp, ApprovalKind.PAYMENT_APPROVAL, payout, payee)
    if human_approved and not approved:
        _audit(db, actor, "payment.approval_superseded", sp, sp.status, sp.status, amount=str(payout),
               reason="amount or payee changed after approval; a new approval is required")
    snap = build_snapshot(db, actor, sp, pv.config, human_approved=approved, now=now)
    a = assess_payment(snap, pv.config, {"human_approved": approved,
                                         "escalation_cleared": _approval_matches(db, sp, ApprovalKind.ESCALATION, payout, payee)})
    gd = record_assessment(db, actor, a, pv, resource_type="scheduled_payment", resource_id=sp.id,
                           contract_id=sp.contract_id)
    sp.last_decision_id = gd.id
    sp.claimed_at = sp.claimed_by = None
    o = a.outcome
    c = db.get(Contract, sp.contract_id)
    if o.decision == Decision.ACT:
        execute_payout(db, actor, sp, gd.id, amount=payout)
    elif o.decision == Decision.APPROVE:
        _set_status(db, actor, sp, SPS.AWAITING_APPROVAL, "; ".join(o.reasons), decision_id=str(gd.id))
        _open_request(db, actor, sp, ApprovalKind.PAYMENT_APPROVAL, o.required_approver_role, o.required_approvals, gd.id,
                      f"Approve payment #{sp.sequence_no}: {c.title}", f"risk {a.score}; " + "; ".join(o.reasons),
                      amount=payout, payee=payee)
    elif o.decision == Decision.ESCALATE:
        _set_status(db, actor, sp, SPS.ESCALATED, "; ".join(o.reasons), decision_id=str(gd.id))
        _open_request(db, actor, sp, ApprovalKind.ESCALATION, RoleName.RISK_OFFICER, 1, gd.id,
                      f"Escalated payment #{sp.sequence_no}: {c.title}", f"risk {a.score}; " + "; ".join(o.reasons),
                      amount=payout, payee=payee)
    elif o.decision == Decision.VERIFY:
        _set_status(db, actor, sp, SPS.HELD, "; ".join(o.reasons), decision_id=str(gd.id))
        failed_ids = {r.rule_id for r in a.rules if not r.passed}
        if failed_ids & {"PAY_INVOICE_PRESENT"}:
            notify_user(db, sp.org_id, c.contractor_id, "payment.invoice_required", "Invoice required for payment",
                        f"Submit an invoice for {sp.amount} {sp.currency} (period {sp.period_start} – {sp.period_end}).",
                        f"/payments/{sp.id}")
        notify_roles(db, sp.org_id, [RoleName.FINANCE_ANALYST], "payment.held", f"Payment held: {c.title}",
                     "; ".join(o.reasons), f"/payments/{sp.id}")
    else:  # BLOCK
        terminal = any(not r.passed and r.terminal and r.on_fail == Decision.BLOCK.value for r in a.rules)
        dup = any(r.rule_id == "PAY_NOT_DUPLICATE" and not r.passed for r in a.rules)
        if terminal:
            _set_status(db, actor, sp, SPS.BLOCKED, "; ".join(o.reasons), decision_id=str(gd.id))
            notify_roles(db, sp.org_id, [RoleName.FINANCE_ANALYST, RoleName.RISK_OFFICER], "payment.blocked",
                         f"Payment blocked: {c.title}", "; ".join(o.reasons), f"/payments/{sp.id}")
        elif not dup:
            sp.status = prior_status
            _audit(db, actor, "payment.execution_refused", sp, prior_status, prior_status, reasons=o.reasons,
                   decision_id=str(gd.id))
        else:
            _audit(db, actor, "payment.duplicate_rejected", sp, sp.status, sp.status, reasons=o.reasons,
                   decision_id=str(gd.id))
            if sp.status == SPS.PROCESSING:
                sp.status = prior_status
    if o.decision == Decision.ESCALATE and any(r.rule_id == "PAY_BENEFICIARY_UNCHANGED" and not r.passed for r in a.rules):
        notify_user(db, sp.org_id, c.contractor_id, "payment.escalated", "Payment escalated: payout account changed",
                    "Your payout account changed since the last payment, so a risk officer must review this payment.",
                    f"/payments/{sp.id}")
    return o.decision.value


def _open_request(db: Session, actor: Principal, sp: ScheduledPayment, kind: str, role: str | None, approvals: int,
                  gd_id: uuid.UUID, title: str, summary: str, *, amount: Decimal | None = None,
                  payee: str | None = None) -> ApprovalRequest:
    c = db.get(Contract, sp.contract_id)
    if amount is None:
        amount = Decimal(sp.amount)
    if payee is None and c.beneficiary_id:
        payee = db.get(Beneficiary, c.beneficiary_id).provider_fund_account_id
    existing = db.execute(select(ApprovalRequest).where(ApprovalRequest.resource_type == "scheduled_payment",
                                                        ApprovalRequest.resource_id == sp.id, ApprovalRequest.kind == kind,
                                                        ApprovalRequest.status == ApprovalStatus.OPEN)).scalar_one_or_none()
    if existing and existing.amount is not None and Decimal(existing.amount) == Decimal(amount) and existing.payee_ref == payee:
        existing.governance_decision_id = gd_id
        return existing
    if existing:   # what is being approved changed → the old request must not be approvable any more
        existing.status, existing.resolution_reason = ApprovalStatus.CANCELLED, "superseded: amount or payee changed"
        existing.resolved_at = _now()
        AuditService(db).record("approval.superseded", actor=actor, org_id=sp.org_id, resource_type="approval_request",
                                resource_id=existing.id, contract_id=sp.contract_id, new_state="CANCELLED",
                                payload={"old_amount": str(existing.amount), "new_amount": str(amount)})
        db.flush()
    ar = ApprovalRequest(org_id=sp.org_id, kind=kind, resource_type="scheduled_payment", resource_id=sp.id,
                         contract_id=sp.contract_id, governance_decision_id=gd_id,
                         required_role=role or RoleName.FINANCE_APPROVER, required_approvals=max(1, approvals),
                         amount=amount, currency=sp.currency, payee_ref=payee, title=title[:300],
                         summary=summary[:2000], requested_by=actor.user_id)
    db.add(ar)
    db.flush()
    sp.approval_request_id = ar.id
    AuditService(db).record("approval.requested", actor=actor, org_id=sp.org_id, resource_type="approval_request",
                            resource_id=ar.id, contract_id=sp.contract_id, new_state="OPEN",
                            payload={"kind": kind, "required_role": ar.required_role, "required_approvals": ar.required_approvals,
                                     "scheduled_payment_id": str(sp.id), "decision_id": str(gd_id)})
    notify_roles(db, sp.org_id, [ar.required_role], "approval.requested", ar.title, ar.summary, f"/approvals/{ar.id}",
                 exclude=[c.contractor_id])
    return ar


# ------------------------------------------------------------------------------------------- execution
def execute_payout(db: Session, actor: Principal, sp: ScheduledPayment, decision_id: uuid.UUID, *,
                   amount: Decimal | None = None) -> PaymentAttempt | None:
    amount = Decimal(sp.amount) if amount is None else Decimal(amount)
    provider = get_payout_provider()
    c = db.get(Contract, sp.contract_id)
    ben = db.get(Beneficiary, c.beneficiary_id)
    n = (db.execute(select(func.coalesce(func.max(PaymentAttempt.attempt_no), 0))
                    .where(PaymentAttempt.scheduled_payment_id == sp.id)).scalar_one()) + 1
    attempt = PaymentAttempt(org_id=sp.org_id, scheduled_payment_id=sp.id, attempt_no=n,
                             idempotency_key=f"{sp.idempotency_key}-a{n}", provider=provider.name,
                             provider_mode=provider.mode, amount=amount, currency=sp.currency,
                             fund_account_id=ben.provider_fund_account_id, status=AS.CREATED,
                             initiated_by=actor.user_id, governance_decision_id=decision_id)
    db.add(attempt)
    try:
        db.flush()  # partial unique index: at most one live attempt per occurrence
    except IntegrityError:
        db.rollback()
        log.warning("duplicate execution attempt rejected for %s", sp.id)
        return None
    _set_status(db, actor, sp, SPS.SUBMITTED, None, attempt_id=str(attempt.id), attempt_no=n,
                attempt_idempotency_key=attempt.idempotency_key, provider=provider.name, mode=provider.mode)
    db.commit()  # durable intent BEFORE contacting the provider
    req = PayoutRequest(idempotency_key=attempt.idempotency_key, reference_id=_reference(sp, n),
                        fund_account_id=attempt.fund_account_id, amount_minor=to_minor_units(amount, sp.currency),
                        currency=sp.currency, narration=f"FinGuard {sp.sequence_no}",
                        notes={"scheduled_payment_id": str(sp.id), "contract_id": str(sp.contract_id)})
    try:
        res = provider.create_payout(req)
    except ProviderOutcomeUnknown as exc:
        attempt.status, attempt.failure_reason, attempt.updated_at = AS.UNKNOWN, str(exc)[:500], _now()
        _set_status(db, actor, sp, SPS.UNKNOWN, "provider outcome unknown; reconciliation required",
                    attempt_id=str(attempt.id))
        notify_roles(db, sp.org_id, [RoleName.FINANCE_ANALYST], "payment.unknown", "Payout outcome unknown",
                     f"Payment #{sp.sequence_no} for {c.title} must be reconciled before any retry.", f"/payments/{sp.id}")
        db.commit()
        return attempt
    except (ProviderRejected, ProviderNotSent) as exc:
        code = getattr(exc, "code", "not_sent")
        attempt.status, attempt.failure_code, attempt.failure_reason, attempt.updated_at = AS.FAILED, code, str(exc)[:500], _now()
        _set_status(db, actor, sp, SPS.FAILED, f"payout failed ({code}): {exc}", attempt_id=str(attempt.id))
        notify_roles(db, sp.org_id, [RoleName.FINANCE_ANALYST], "payment.failed", "Payout failed",
                     f"Payment #{sp.sequence_no} for {c.title}: {exc}", f"/payments/{sp.id}")
        db.commit()
        return attempt
    _apply_result(db, actor, sp, attempt, res.status, res.provider_status, res.provider_payout_id, res.failure_code,
                  res.failure_reason, res.raw, res.amount_minor, res.currency)
    db.commit()
    return attempt


def _reference(sp: ScheduledPayment, n: int) -> str:
    return f"{sp.id.hex[:24]}-{n}"[:40]


def _apply_result(db: Session, actor: Principal, sp: ScheduledPayment, attempt: PaymentAttempt, status: str,
                  provider_status: str | None, payout_id: str | None, failure_code: str | None,
                  failure_reason: str | None, raw: dict, amount_minor: int | None, currency: str | None) -> None:
    if amount_minor is not None and (amount_minor != to_minor_units(Decimal(attempt.amount), attempt.currency)
                                     or (currency and currency != attempt.currency)):
        attempt.status, attempt.failure_reason = AS.UNKNOWN, "provider amount/currency mismatch"
        _set_status(db, actor, sp, SPS.UNKNOWN, "provider response does not match the authorized amount; escalated",
                    provider_amount=str(from_minor_units(amount_minor, currency or attempt.currency)))
        notify_roles(db, sp.org_id, [RoleName.RISK_OFFICER], "payment.mismatch", "Provider amount mismatch",
                     f"Payment {sp.id} reported a different amount by the provider.", f"/payments/{sp.id}")
        return
    attempt.provider_payout_id = payout_id or attempt.provider_payout_id
    attempt.provider_status = provider_status
    attempt.response_snapshot = raw
    attempt.updated_at = _now()
    prev_attempt = attempt.status
    if status == "PROCESSED":
        attempt.status = AS.PROCESSED
        sp.paid_at = _now()
        _set_status(db, actor, sp, SPS.PAID, None, attempt_id=str(attempt.id), provider_payout_id=payout_id)
        c = db.get(Contract, sp.contract_id)
        notify_user(db, sp.org_id, c.contractor_id, "payment.paid", "Payment processed",
                    f"{attempt.amount} {sp.currency} for {sp.period_start} – {sp.period_end} was paid.", f"/payments/{sp.id}")
    elif status == "PENDING":
        attempt.status = AS.PENDING
        if sp.status != SPS.SUBMITTED:
            _set_status(db, actor, sp, SPS.SUBMITTED, None, attempt_id=str(attempt.id))
    elif status in ("FAILED", "CANCELLED"):
        attempt.status = AS.FAILED if status == "FAILED" else AS.CANCELLED
        attempt.failure_code, attempt.failure_reason = failure_code, failure_reason
        _set_status(db, actor, sp, SPS.FAILED, f"payout {status.lower()}: {failure_reason or failure_code or ''}",
                    attempt_id=str(attempt.id))
        notify_roles(db, sp.org_id, [RoleName.FINANCE_ANALYST], "payment.failed", "Payout failed",
                     f"Payment #{sp.sequence_no}: {failure_reason or status}", f"/payments/{sp.id}")
    elif status == "REVERSED":
        attempt.status = AS.REVERSED
        attempt.failure_reason = failure_reason or "reversed by bank"
        sp.paid_at = None
        _set_status(db, actor, sp, SPS.FAILED, f"payout reversed: {attempt.failure_reason}", attempt_id=str(attempt.id))
        notify_roles(db, sp.org_id, [RoleName.FINANCE_ANALYST, RoleName.RISK_OFFICER], "payment.reversed",
                     "Payout reversed", f"Payment #{sp.sequence_no} was reversed by the bank.", f"/payments/{sp.id}")
    AuditService(db).record("payment.attempt_updated", actor=actor, org_id=sp.org_id, resource_type="payment_attempt",
                            resource_id=attempt.id, contract_id=sp.contract_id, previous_state=prev_attempt,
                            new_state=attempt.status, payload={"provider_status": provider_status,
                                                               "provider_payout_id": payout_id,
                                                               "idempotency_key": attempt.idempotency_key,
                                                               "failure_code": failure_code})


# ------------------------------------------------------------------------------------------- scheduler tick
def claim_due(db: Session, now: datetime, worker_id: str, limit: int = 50) -> list[uuid.UUID]:
    rows = db.execute(select(ScheduledPayment).where(ScheduledPayment.status.in_(CLAIMABLE),
                                                     ScheduledPayment.due_at <= now)
                      .order_by(ScheduledPayment.due_at).limit(limit).with_for_update(skip_locked=True, of=ScheduledPayment)).scalars().all()
    ids = []
    for sp in rows:
        sp.status, sp.claimed_at, sp.claimed_by = SPS.PROCESSING, now, worker_id
        ids.append(sp.id)
    db.commit()
    return ids


def recover_stale(db: Session, now: datetime) -> int:
    """Recovery after a worker crash/restart: release stale claims and turn orphaned CREATED attempts into UNKNOWN."""
    lease = timedelta(seconds=get_settings().scheduler_lease_seconds)
    n = 0
    for sp in db.execute(select(ScheduledPayment).where(ScheduledPayment.status == SPS.PROCESSING,
                                                        ScheduledPayment.claimed_at < now - lease)
                         .with_for_update(skip_locked=True, of=ScheduledPayment)).scalars():
        sp.status, sp.claimed_at, sp.claimed_by = SPS.SCHEDULED, None, None
        n += 1
    for at in db.execute(select(PaymentAttempt).where(PaymentAttempt.status == AS.CREATED,
                                                      PaymentAttempt.created_at < now - lease)
                         .with_for_update(skip_locked=True, of=PaymentAttempt)).scalars():
        at.status, at.failure_reason = AS.UNKNOWN, "worker stopped after recording intent; outcome unknown"
        sp = db.get(ScheduledPayment, at.scheduled_payment_id)
        sp.status = SPS.UNKNOWN
        n += 1
    db.commit()
    return n


def process_due(db: Session, now: datetime | None = None, worker_id: str = "worker") -> dict[str, int]:
    now = now or _now()
    stats = {"recovered": recover_stale(db, now), "claimed": 0}
    for sp_id in claim_due(db, now, worker_id):
        stats["claimed"] += 1
        sp = db.execute(select(ScheduledPayment).where(ScheduledPayment.id == sp_id).with_for_update(of=ScheduledPayment)).scalar_one()
        system = service_principal(db, sp.org_id, RoleName.SCHEDULER)
        try:
            approved = sp.status == SPS.PROCESSING and _was_approved(db, sp)
            decision = govern_and_route(db, system, sp, human_approved=approved, now=now)
            stats[decision] = stats.get(decision, 0) + 1
            db.commit()
        except Exception:
            log.exception("processing scheduled payment %s failed", sp_id)
            db.rollback()
            sp = db.get(ScheduledPayment, sp_id)
            if sp.status == SPS.PROCESSING:
                sp.status = SPS.SCHEDULED
                db.commit()
    return stats


def _was_approved(db: Session, sp: ScheduledPayment) -> bool:
    return db.execute(select(ApprovalRequest.id).where(
        ApprovalRequest.resource_type == "scheduled_payment", ApprovalRequest.resource_id == sp.id,
        ApprovalRequest.kind == ApprovalKind.PAYMENT_APPROVAL, ApprovalRequest.status == ApprovalStatus.APPROVED)).first() is not None


# ------------------------------------------------------------------------------------------- approvals hook
def decide_payment(db: Session, actor: Principal, ar: ApprovalRequest, decision: str, reason: str) -> None:
    sp = db.execute(select(ScheduledPayment).where(ScheduledPayment.id == ar.resource_id).with_for_update(of=ScheduledPayment)).scalar_one()
    c = db.get(Contract, sp.contract_id)
    abac.ensure_same_org(actor, sp.org_id)
    abac.require(actor, "payments:approve")
    abac.ensure_not_self(actor, c.contractor_id, what="approve your own payment")
    if sp.status != SPS.AWAITING_APPROVAL:
        raise InvalidState(f"payment is {sp.status}, not awaiting approval")
    if decision == "REJECT":
        _set_status(db, actor, sp, SPS.BLOCKED, f"rejected by approver: {reason}")
        return
    if decision == "APPROVE_FINAL":
        pv = active_policy(db, sp.org_id)
        snap = build_snapshot(db, actor, sp, pv.config, human_approved=True)
        a = assess_payment(snap, pv.config, {"human_approved": True, "escalation_cleared": _escalation_cleared(db, sp)})
        blocks = [r for r in a.rules if not r.passed and r.on_fail in (Decision.BLOCK.value,)]
        if blocks:
            gd = record_assessment(db, actor, a, pv, resource_type="scheduled_payment", resource_id=sp.id,
                                   contract_id=sp.contract_id)
            raise Forbidden(blocks[0].message, code=blocks[0].rule_id.lower(), details={"decision_id": str(gd.id)})
        _set_status(db, actor, sp, SPS.APPROVED_FOR_EXECUTION, f"approved: {reason}")
        govern_and_route(db, actor, sp, human_approved=True)


# ------------------------------------------------------------------------------------------- reconciliation
def reconcile_attempt(db: Session, actor: Principal, attempt: PaymentAttempt) -> PaymentAttempt:
    if attempt.status not in (AS.UNKNOWN, AS.PENDING, AS.CREATED):
        return attempt
    provider = get_payout_provider()
    sp = db.execute(select(ScheduledPayment).where(ScheduledPayment.id == attempt.scheduled_payment_id).with_for_update(of=ScheduledPayment)).scalar_one()
    try:
        res = provider.get_payout_status(attempt.provider_payout_id) if attempt.provider_payout_id \
            else provider.find_by_reference(_reference(sp, attempt.attempt_no))
    except (ProviderOutcomeUnknown, ProviderNotSent) as exc:
        AuditService(db).record("payment.reconcile_deferred", actor=actor, org_id=sp.org_id,
                                resource_type="payment_attempt", resource_id=attempt.id, contract_id=sp.contract_id,
                                outcome="FAILURE", payload={"error": str(exc)[:200]})
        db.commit()
        return attempt
    if res is None:
        # Provider has no record of this payout: it was never created. A new attempt is now safe.
        attempt.status, attempt.failure_code = AS.FAILED, "not_found_at_provider"
        attempt.failure_reason = "reconciliation: provider has no payout for this reference"
        _set_status(db, actor, sp, SPS.FAILED, "reconciled: payout was never created; safe to retry",
                    attempt_id=str(attempt.id))
    else:
        _apply_result(db, actor, sp, attempt, res.status, res.provider_status, res.provider_payout_id,
                      res.failure_code, res.failure_reason, res.raw, res.amount_minor, res.currency)
    AuditService(db).record("payment.reconciled", actor=actor, org_id=sp.org_id, resource_type="payment_attempt",
                            resource_id=attempt.id, contract_id=sp.contract_id, new_state=attempt.status,
                            payload={"found": res is not None, "provider_status": res.provider_status if res else None})
    db.commit()
    return attempt


def reconcile_open(db: Session) -> int:
    n = 0
    for at in db.execute(select(PaymentAttempt).where(PaymentAttempt.status.in_([AS.UNKNOWN, AS.PENDING]))).scalars().all():
        system = service_principal(db, at.org_id, RoleName.SCHEDULER)
        reconcile_attempt(db, system, at)
        n += 1
    return n


def retry_failed(db: Session, actor: Principal, sp_id: Any) -> str:
    """Re-run governance for a payment whose previous attempt is CONFIRMED failed (never for UNKNOWN)."""
    abac.require(actor, "payments:prepare")
    sp = abac.load_scoped(db, ScheduledPayment, sp_id, actor)
    sp = db.execute(select(ScheduledPayment).where(ScheduledPayment.id == sp.id).with_for_update(of=ScheduledPayment)).scalar_one()
    if sp.status not in (SPS.FAILED, SPS.HELD):
        raise InvalidState(f"only FAILED or HELD payments can be re-submitted (status {sp.status})")
    if any(a.status in (AS.UNKNOWN, AS.CREATED, AS.PENDING) for a in sp.attempts):
        raise InvalidState("an attempt is unresolved; reconcile first")
    _set_status(db, actor, sp, SPS.SCHEDULED, f"re-submitted by {actor.full_name}")
    db.flush()
    system = service_principal(db, sp.org_id, RoleName.SCHEDULER, request_id=actor.request_id)
    return govern_and_route(db, system, sp, human_approved=_was_approved(db, sp))


def requeue_held(db: Session, contract_id: uuid.UUID, reason: str, actor: Principal) -> int:
    """Re-evaluate HELD occurrences after the blocking condition changed (invoice, beneficiary, amendment)."""
    n = 0
    for sp in db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == contract_id,
                                                        ScheduledPayment.status == SPS.HELD)).scalars():
        _set_status(db, actor, sp, SPS.SCHEDULED, f"re-queued: {reason}")
        n += 1
    return n


# ------------------------------------------------------------------------------------------- webhooks
def handle_webhook(db: Session, provider_name: str, raw_body: bytes, headers: dict[str, str]) -> dict[str, str]:
    provider = get_payout_provider()
    headers = {k.lower(): v for k, v in headers.items()}
    if provider.name != provider_name:
        return {"outcome": "IGNORED", "detail": "provider not configured"}
    valid = provider.verify_webhook(raw_body, headers)
    event_id = headers.get("x-razorpay-event-id") or ""
    if not valid:
        db.add(PaymentProviderEvent(provider=provider_name, event_id=f"invalid-{uuid.uuid4().hex}", event_type="unknown",
                                    signature_valid=False, outcome="REJECTED", detail="invalid signature",
                                    payload={"size": len(raw_body), "claimed_event_id": event_id[:100]}))
        AuditService(db).record("webhook.rejected", outcome="DENIED", resource_type="provider_event",
                                payload={"provider": provider_name, "reason": "invalid signature", "event_id": event_id[:100]})
        db.commit()
        return {"outcome": "REJECTED", "detail": "invalid signature"}
    try:
        ev: WebhookEvent = provider.parse_webhook(raw_body, headers)
    except (ValueError, KeyError) as exc:
        return {"outcome": "REJECTED", "detail": f"malformed event: {exc}"}
    row = PaymentProviderEvent(provider=provider_name, event_id=ev.event_id, event_type=ev.event_type,
                               provider_payout_id=ev.provider_payout_id, signature_valid=True, outcome="RECEIVED",
                               payload={"event": ev.event_type, "payout_id": ev.provider_payout_id, "status": ev.status,
                                        "amount": ev.amount_minor, "currency": ev.currency, "reference_id": ev.reference_id})
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"outcome": "DUPLICATE", "detail": "event already processed"}
    attempt = db.execute(select(PaymentAttempt).where(PaymentAttempt.provider_payout_id == ev.provider_payout_id)
                         .with_for_update(of=PaymentAttempt)).scalar_one_or_none() if ev.provider_payout_id else None
    if attempt is None:
        row.outcome, row.detail = "IGNORED", "no matching payout attempt"
        db.commit()
        return {"outcome": "IGNORED", "detail": row.detail}
    sp = db.get(ScheduledPayment, attempt.scheduled_payment_id)
    row.payment_attempt_id = attempt.id
    expected_minor = to_minor_units(Decimal(attempt.amount), attempt.currency)
    if (ev.amount_minor is not None and ev.amount_minor != expected_minor) or (ev.currency and ev.currency != attempt.currency) \
            or (ev.reference_id and ev.reference_id != _reference(sp, attempt.attempt_no)):
        row.outcome, row.detail = "MISMATCH", "amount/currency/reference does not match the internal attempt"
        system = service_principal(db, sp.org_id, RoleName.SCHEDULER)
        _set_status(db, system, sp, SPS.UNKNOWN, "webhook data mismatch; investigation required", event_id=ev.event_id)
        attempt.status = AS.UNKNOWN
        notify_roles(db, sp.org_id, [RoleName.RISK_OFFICER], "payment.mismatch", "Webhook mismatch",
                     f"Event {ev.event_id} does not match payment {sp.id}.", f"/payments/{sp.id}")
        db.commit()
        return {"outcome": "MISMATCH", "detail": row.detail}
    if attempt.status in (AS.PROCESSED, AS.FAILED, AS.REVERSED, AS.CANCELLED) and not (
            attempt.status == AS.PROCESSED and ev.status == "REVERSED"):
        row.outcome, row.detail = "IGNORED", f"attempt already final ({attempt.status})"
        db.commit()
        return {"outcome": "IGNORED", "detail": row.detail}
    system = service_principal(db, sp.org_id, RoleName.SCHEDULER)
    if ev.status:
        _apply_result(db, system, sp, attempt, ev.status, ev.event_type.replace("payout.", ""), ev.provider_payout_id,
                      ev.failure_reason, ev.failure_reason, {"event_id": ev.event_id}, ev.amount_minor, ev.currency)
    row.outcome = "APPLIED"
    db.commit()
    return {"outcome": "APPLIED", "detail": f"attempt {attempt.status}"}
