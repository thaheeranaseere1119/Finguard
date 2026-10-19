"""Contract amendments (e.g. ₹20,000 → ₹25,000 per month).

* The request references the current approved version; approved terms are never changed in place.
* The requested effective date is aligned to the next unpaid period boundary of the existing schedule, and must not
  touch periods that are paid or in flight.
* Risk is assessed as a separate AMENDMENT_APPROVAL decision; an authorized approver (not the requester) decides.
* On approval a NEW immutable version is created (version_no + 1, effective_from); only future, not-yet-executed
  occurrences from the effective date are superseded and regenerated. Completed payments stay untouched.
* On rejection the original terms remain; payments held because of the pending amendment are re-queued.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.errors import Forbidden, InvalidState, ValidationFailed
from finguard.documents import storage
from finguard.extraction import parsers as P
from finguard.extraction.pipeline import effective_fields
from finguard.extraction.schema import normalize_value
from finguard.governance.assessors import AmendmentSnapshot, assess_amendment
from finguard.governance.policy import to_base
from finguard.governance.service import active_policy, record_assessment
from finguard.models import (ApprovalRequest, Contract, ContractAmendment, ContractApproval, ContractDocument,
                             ContractVersion, ExtractionResult, OcrJob, Organization, ScheduledPayment)
from finguard.models.enums import (AmendmentStatus as AMS, ApprovalKind, ApprovalStatus, ContractStatus as CS, Decision,
                                   OcrJobStatus, RoleName, ScheduledPaymentStatus as SPS, VersionStatus)
from finguard.scheduling.generator import ScheduleError, schedule_total
from finguard.services.audit import AuditService
from finguard.services.common import TERM_FIELDS, org_calendar, terms_hash, terms_of, version_terms_dict
from finguard.services.notifications import notify_roles, notify_user
from finguard.worker.dispatch import after_commit

AMENDABLE = {"payment_amount": "amount", "frequency": "frequency", "interval_count": "int", "custom_unit": "enum",
             "due_day_of_month": "int", "due_weekday": "int", "end_date": "date", "total_value": "amount",
             "invoice_required": "bool", "payment_timing": "enum", "currency": "currency"}
EXECUTED = (SPS.PAID, SPS.SUBMITTED, SPS.UNKNOWN, SPS.PROCESSING)


def _coerce(name: str, value: Any) -> Any:
    norm = normalize_value(name, value)
    if norm is None:
        return None
    kind = AMENDABLE[name]
    if kind == "amount":
        return Decimal(norm)
    if kind == "int":
        return int(norm)
    if kind == "date":
        return date.fromisoformat(norm)
    if kind == "bool":
        return norm == "true"
    return norm


def compute_diff(base: ContractVersion, changes: dict[str, Any]) -> list[dict]:
    out = []
    for k, v in changes.items():
        old = getattr(base, k)
        if str(old) != str(v):
            out.append({"field": k, "old": None if old is None else str(old), "new": None if v is None else str(v)})
    return out


def aligned_effective_date(db: Session, c: Contract, requested: date) -> date:
    """First not-yet-executed occurrence period start on/after the requested date."""
    sp = db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id,
                                                   ScheduledPayment.status.not_in([SPS.SUPERSEDED, SPS.CANCELLED]),
                                                   ScheduledPayment.period_start >= requested)
                    .order_by(ScheduledPayment.period_start)).scalars().first()
    if sp is None:
        raise ValidationFailed("no remaining contract period on or after the requested effective date")
    return sp.period_start


def request_amendment(db: Session, actor: Principal, contract_id: Any, changes: dict[str, Any], reason: str,
                      effective_date: date, document: tuple[str, bytes] | None = None) -> ContractAmendment:
    from finguard.services.contracts import _store_document, get_contract

    c = get_contract(db, actor, contract_id)
    abac.require(actor, "amendments:request_own")
    abac.ensure_owner(actor, c)
    if c.status != CS.ACTIVE:
        raise InvalidState("only active contracts can be amended")
    if not reason or len(reason.strip()) < 5:
        raise ValidationFailed("a reason is required")
    unknown = set(changes) - set(AMENDABLE)
    if unknown:
        raise ValidationFailed(f"fields cannot be amended: {', '.join(sorted(unknown))}")
    try:
        coerced = {k: _coerce(k, v) for k, v in changes.items() if v is not None and str(v).strip() != ""}
    except P.ParseError as exc:
        raise ValidationFailed(str(exc)) from exc
    base = db.get(ContractVersion, c.current_version_id)
    diff = compute_diff(base, coerced)
    if not diff:
        raise ValidationFailed("the proposed changes are identical to the current terms")
    if db.execute(select(ContractAmendment.id).where(ContractAmendment.contract_id == c.id, ContractAmendment.status.in_(
            [AMS.PENDING_APPROVAL, AMS.OCR_PROCESSING, AMS.DRAFT]))).first():
        raise InvalidState("another amendment is already pending for this contract")
    eff = aligned_effective_date(db, c, effective_date)
    terms = version_terms_dict(base) | coerced | {"effective_from": eff}
    proposed = ContractVersion(contract_id=c.id, status=VersionStatus.PROPOSED, created_by=actor.user_id,
                               source_extraction_id=base.source_extraction_id, terms_hash=terms_hash(terms),
                               **{k: terms[k] for k in TERM_FIELDS})
    db.add(proposed)
    db.flush()
    am = ContractAmendment(org_id=c.org_id, contract_id=c.id, base_version_id=base.id, proposed_version_id=proposed.id,
                           requested_by=actor.user_id, reason=reason.strip(), requested_effective_date=eff,
                           proposed_changes={k: str(v) for k, v in coerced.items()}, diff=diff, status=AMS.DRAFT)
    db.add(am)
    db.flush()
    proposed.amendment_id = am.id
    AuditService(db).record("amendment.requested", actor=actor, resource_type="contract_amendment", resource_id=am.id,
                            contract_id=c.id, new_state=AMS.DRAFT,
                            payload={"diff": diff, "requested_effective_date": effective_date.isoformat(),
                                     "aligned_effective_date": eff.isoformat(), "reason": reason})
    if document:
        attach_document(db, actor, am, *document, _store=_store_document)
    else:
        am.status = AMS.PENDING_APPROVAL
        route_amendment(db, actor, am)
    return am


def attach_document(db: Session, actor: Principal, am: ContractAmendment, filename: str, data: bytes, _store=None) -> None:
    from finguard.services.contracts import _store_document

    c = db.get(Contract, am.contract_id)
    abac.ensure_owner(actor, c)
    if am.status not in (AMS.DRAFT, AMS.PENDING_APPROVAL):
        raise InvalidState(f"amendment is {am.status}")
    upload = storage.validate_upload(filename, data)
    doc = (_store or _store_document)(db, actor, c, upload, "AMENDMENT", am.id)
    am.document_id = doc.id
    am.status = AMS.OCR_PROCESSING
    for ar in db.execute(select(ApprovalRequest).where(ApprovalRequest.resource_id == am.id,
                                                       ApprovalRequest.status == ApprovalStatus.OPEN)).scalars():
        ar.status, ar.resolution_reason = ApprovalStatus.CANCELLED, "superseded: new amendment evidence attached"
    job = OcrJob(document_id=doc.id, status=OcrJobStatus.QUEUED, pages_total=upload.page_count)
    db.add(job)
    db.flush()
    after_commit(db, "finguard.worker.jobs.run_ocr_job", job.id)


def on_amendment_document_extracted(db: Session, system: Principal, doc: ContractDocument, er: ExtractionResult) -> None:
    am = db.get(ContractAmendment, doc.amendment_id)
    am.extraction_id = er.id
    am.status = AMS.PENDING_APPROVAL
    route_amendment(db, system, am)


def _document_matches(db: Session, am: ContractAmendment) -> bool | None:
    if not am.extraction_id:
        return None
    fields = effective_fields(db, db.get(ExtractionResult, am.extraction_id))
    checks = []
    for k, v in am.proposed_changes.items():
        if k in fields and fields[k]["value"] is not None:
            checks.append(str(fields[k]["value"]) == str(v) or (
                k in ("payment_amount", "total_value") and Decimal(fields[k]["value"]) == Decimal(v)))
    if not checks:
        return False  # the document does not state any of the proposed values
    return all(checks)


def _snapshot(db: Session, am: ContractAmendment, actor: Principal | None, policy: dict, preview: bool) -> AmendmentSnapshot:
    c = db.get(Contract, am.contract_id)
    base = db.get(ContractVersion, am.base_version_id)
    prop = db.get(ContractVersion, am.proposed_version_id)
    org = db.get(Organization, c.org_id)
    remaining_old = [sp for sp in db.execute(select(ScheduledPayment).where(
        ScheduledPayment.contract_id == c.id, ScheduledPayment.period_start >= am.requested_effective_date,
        ScheduledPayment.status.not_in([SPS.SUPERSEDED, SPS.CANCELLED]))).scalars()]
    old_commit = sum((Decimal(sp.amount) for sp in remaining_old), Decimal(0))
    try:
        new_commit = schedule_total(terms_of(prop), org_calendar(org))
    except ScheduleError:
        new_commit = old_commit
    executed_after = db.execute(select(func.count()).select_from(ScheduledPayment).where(
        ScheduledPayment.contract_id == c.id, ScheduledPayment.period_start >= am.requested_effective_date,
        ScheduledPayment.status.in_(EXECUTED))).scalar_one()
    er = db.get(ExtractionResult, am.extraction_id) if am.extraction_id else None
    return AmendmentSnapshot(
        old_amount=Decimal(base.payment_amount), new_amount=Decimal(prop.payment_amount), old_currency=base.currency,
        new_currency=prop.currency, commitment_delta_base=to_base(new_commit - old_commit, prop.currency, policy),
        new_amount_base=to_base(Decimal(prop.payment_amount), prop.currency, policy), amendment_status=am.status,
        approver_authorized=bool(actor and actor.has("amendments:approve")),
        same_org=actor is None or actor.org_id == am.org_id,
        approver_is_requester=bool(actor and actor.user_id == am.requested_by),
        approver_is_contractor=bool(actor and actor.user_id == c.contractor_id),
        base_is_current=c.current_version_id == am.base_version_id, contract_active=c.status == CS.ACTIVE,
        frequency_changed=base.frequency != prop.frequency or base.interval_count != prop.interval_count,
        retroactive_affects_paid=executed_after > 0, has_document=am.document_id is not None,
        document_matches=_document_matches(db, am),
        injection_detected=bool(er and (er.injection_report or {}).get("detected")),
        evidence_refs=[{"type": "contract_version", "id": str(base.id), "version_no": base.version_no},
                       {"type": "amendment_diff", "diff": am.diff}]
        + ([{"type": "document", "id": str(am.document_id)}] if am.document_id else []),
        preview=preview,
    )


def route_amendment(db: Session, actor: Principal, am: ContractAmendment) -> None:
    pv = active_policy(db, am.org_id)
    a = assess_amendment(_snapshot(db, am, None, pv.config, preview=True), pv.config)
    gd = record_assessment(db, actor, a, pv, resource_type="contract_amendment", resource_id=am.id,
                           contract_id=am.contract_id)
    am.risk_assessment_id = gd.risk_assessment_id
    o = a.outcome
    c = db.get(Contract, am.contract_id)
    prop = db.get(ContractVersion, am.proposed_version_id)
    if o.decision == Decision.BLOCK:
        reject_amendment(db, actor, am, "; ".join(o.reasons))
        return
    if o.decision == Decision.VERIFY:
        notify_user(db, c.org_id, c.contractor_id, "amendment.evidence_required", "Amendment needs supporting evidence",
                    "; ".join(o.reasons), f"/contracts/{c.id}")
        kind, role = ApprovalKind.VERIFICATION, RoleName.FINANCE_ANALYST
    elif o.decision == Decision.ESCALATE:
        kind, role = ApprovalKind.ESCALATION, RoleName.RISK_OFFICER
    else:
        kind = ApprovalKind.AMENDMENT_APPROVAL
        role = RoleName.FINANCE_MANAGER if abs(Decimal(a.inputs["commitment_delta_base"])) > Decimal(
            str(pv.config["contract_manager_threshold"])) else RoleName.FINANCE_APPROVER
    ar = ApprovalRequest(org_id=am.org_id, kind=kind, resource_type="contract_amendment", resource_id=am.id,
                         contract_id=c.id, governance_decision_id=gd.id, required_role=role, required_approvals=1,
                         amount=prop.payment_amount, currency=prop.currency,
                         title=f"{'Escalated' if kind == ApprovalKind.ESCALATION else 'Approve'} amendment: {c.title}",
                         summary="; ".join(f"{d['field']}: {d['old']} → {d['new']}" for d in am.diff)
                                 + f" from {am.requested_effective_date} · risk {a.score} · {o.decision.value}",
                         requested_by=am.requested_by)
    db.add(ar)
    db.flush()
    AuditService(db).record("approval.requested", actor=actor, resource_type="approval_request", resource_id=ar.id,
                            contract_id=c.id, new_state="OPEN", payload={"kind": kind, "amendment_id": str(am.id)})
    if kind != ApprovalKind.VERIFICATION:
        notify_roles(db, am.org_id, [role], "approval.requested", ar.title, ar.summary, f"/approvals/{ar.id}",
                     exclude=[c.contractor_id])


def decide_amendment(db: Session, actor: Principal, ar: ApprovalRequest, decision: str, reason: str,
                     escalation_cleared: bool = False) -> None:
    am = db.execute(select(ContractAmendment).where(ContractAmendment.id == ar.resource_id).with_for_update(of=ContractAmendment)).scalar_one()
    abac.ensure_same_org(actor, am.org_id)
    abac.require(actor, "amendments:approve")
    if decision == "REJECT":
        reject_amendment(db, actor, am, reason)
        return
    pv = active_policy(db, am.org_id)
    a = assess_amendment(_snapshot(db, am, actor, pv.config, preview=False), pv.config,
                         {"escalation_cleared": escalation_cleared})
    gd = record_assessment(db, actor, a, pv, resource_type="contract_amendment", resource_id=am.id,
                           contract_id=am.contract_id)
    blocks = [r for r in a.rules if not r.passed and r.on_fail == Decision.BLOCK.value]
    if blocks:
        raise Forbidden(blocks[0].message, code=blocks[0].rule_id.lower(), details={"decision_id": str(gd.id)})
    if a.outcome.decision in (Decision.ESCALATE, Decision.VERIFY):
        raise InvalidState(f"policy requires {a.outcome.decision.value}: {a.outcome.reasons[0]}")
    apply_amendment(db, actor, am, reason, gd.risk_assessment_id)


def apply_amendment(db: Session, actor: Principal, am: ContractAmendment, reason: str, ra_id) -> ContractVersion:
    from finguard.services.contracts import create_schedule

    c = db.execute(select(Contract).where(Contract.id == am.contract_id).with_for_update(of=Contract)).scalar_one()
    if c.current_version_id != am.base_version_id:
        raise InvalidState("contract changed since the amendment was requested")
    base = db.get(ContractVersion, am.base_version_id)
    new = db.get(ContractVersion, am.proposed_version_id)
    now = datetime.now(timezone.utc)
    next_no = (db.execute(select(func.max(ContractVersion.version_no)).where(ContractVersion.contract_id == c.id)).scalar_one() or 0) + 1
    new.status, new.version_no, new.approved_by, new.approved_at = VersionStatus.APPROVED, next_no, actor.user_id, now
    base.status = VersionStatus.SUPERSEDED  # trigger permits only APPROVED→SUPERSEDED on approved rows
    c.current_version_id = new.id
    superseded = 0
    for sp in db.execute(select(ScheduledPayment).where(
            ScheduledPayment.contract_id == c.id, ScheduledPayment.period_start >= am.requested_effective_date,
            ScheduledPayment.status.not_in([SPS.SUPERSEDED, SPS.CANCELLED, *EXECUTED]))).scalars():
        for ar in db.execute(select(ApprovalRequest).where(ApprovalRequest.resource_id == sp.id,
                                                           ApprovalRequest.status == ApprovalStatus.OPEN)).scalars():
            ar.status, ar.resolution_reason = ApprovalStatus.CANCELLED, "superseded by amendment"
        sp.status, sp.hold_reason = SPS.SUPERSEDED, f"superseded by amendment {am.id}"
        superseded += 1
    db.flush()
    sched = create_schedule(db, actor, c, new, am.requested_effective_date)
    am.status, am.decided_by, am.decided_at, am.decision_reason = AMS.APPROVED, actor.user_id, now, reason
    db.add(ContractApproval(contract_id=c.id, contract_version_id=new.id, amendment_id=am.id, approver_id=actor.user_id,
                            decision="APPROVED", reason=reason, risk_assessment_id=ra_id))
    AuditService(db).record("amendment.approved", actor=actor, resource_type="contract_amendment", resource_id=am.id,
                            contract_id=c.id, previous_state=AMS.PENDING_APPROVAL, new_state=AMS.APPROVED,
                            payload={"new_version_id": str(new.id), "version_no": next_no, "superseded_occurrences": superseded,
                                     "new_schedule_id": str(sched.id), "effective_from": am.requested_effective_date.isoformat(),
                                     "diff": am.diff, "reason": reason})
    notify_user(db, c.org_id, c.contractor_id, "amendment.approved", "Amendment approved",
                f"New terms apply from {am.requested_effective_date}.", f"/contracts/{c.id}")
    return new


def reject_amendment(db: Session, actor: Principal, am: ContractAmendment, reason: str) -> None:
    from finguard.services.payments import requeue_held

    prev = am.status
    am.status, am.decided_by, am.decided_at, am.decision_reason = AMS.REJECTED, actor.user_id, datetime.now(timezone.utc), reason
    if am.proposed_version_id:
        db.get(ContractVersion, am.proposed_version_id).status = VersionStatus.REJECTED
    for ar in db.execute(select(ApprovalRequest).where(ApprovalRequest.resource_id == am.id,
                                                       ApprovalRequest.status == ApprovalStatus.OPEN)).scalars():
        ar.status, ar.resolution_reason = ApprovalStatus.CANCELLED, "amendment rejected"
    AuditService(db).record("amendment.rejected", actor=actor, resource_type="contract_amendment", resource_id=am.id,
                            contract_id=am.contract_id, previous_state=prev, new_state=AMS.REJECTED, payload={"reason": reason})
    requeue_held(db, am.contract_id, "amendment rejected; original terms apply", actor)
    c = db.get(Contract, am.contract_id)
    notify_user(db, c.org_id, c.contractor_id, "amendment.rejected", "Amendment rejected", reason, f"/contracts/{c.id}")


def withdraw(db: Session, actor: Principal, amendment_id: Any) -> ContractAmendment:
    am = abac.load_scoped(db, ContractAmendment, amendment_id, actor)
    if am.requested_by != actor.user_id:
        raise Forbidden("only the requester can withdraw an amendment")
    if am.status not in (AMS.DRAFT, AMS.PENDING_APPROVAL, AMS.OCR_PROCESSING):
        raise InvalidState(f"amendment is {am.status}")
    reject_amendment(db, actor, am, "withdrawn by requester")
    am.status = AMS.WITHDRAWN
    return am
