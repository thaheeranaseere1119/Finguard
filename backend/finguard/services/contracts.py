"""Contract lifecycle: upload → OCR → review → validation → approval → activation (+ suspension/expiry).

DRAFT → UPLOADED → OCR_PROCESSING → NEEDS_REVIEW → PENDING_VALIDATION → PENDING_APPROVAL → APPROVED → ACTIVE
Exceptions: OCR_FAILED, VALIDATION_FAILED, REJECTED, SUSPENDED, EXPIRED, TERMINATED.
Every transition is checked against ALLOWED_TRANSITIONS and audited with previous/new state.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from finguard.documents import storage
from finguard.documents.text_extraction import OcrUnavailable, extract
from finguard.extraction import parsers as P
from finguard.extraction.pipeline import effective_fields, persist, run_extraction, unresolved_critical
from finguard.extraction.schema import FIELD_MAP, normalize_value
from finguard.governance.assessors import ContractApprovalSnapshot, assess_contract_approval
from finguard.governance.policy import to_base
from finguard.governance.service import active_policy, record_assessment
from finguard.models import (ApprovalRequest, Contract, ContractApproval, ContractDocument, ContractValidation,
                             ContractVersion, ExtractionResult, FieldCorrection, OcrJob, Organization, PaymentSchedule,
                             ScheduledPayment)
from finguard.models.enums import (ApprovalKind, ApprovalStatus, ContractStatus as CS, Decision, OcrJobStatus, RoleName,
                                   ScheduledPaymentStatus as SPS, VersionStatus)
from finguard.scheduling.generator import GENERATOR_VERSION, ScheduleError, generate, schedule_total
from finguard.services.audit import AuditService
from finguard.services.common import org_calendar, terms_hash, terms_of
from finguard.services.notifications import notify_roles, notify_user
from finguard.worker.dispatch import after_commit

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    CS.DRAFT: {CS.UPLOADED},
    CS.UPLOADED: {CS.OCR_PROCESSING, CS.OCR_FAILED},
    CS.OCR_PROCESSING: {CS.NEEDS_REVIEW, CS.OCR_FAILED},
    CS.OCR_FAILED: {CS.OCR_PROCESSING},
    CS.NEEDS_REVIEW: {CS.PENDING_VALIDATION, CS.OCR_PROCESSING},
    CS.PENDING_VALIDATION: {CS.PENDING_APPROVAL, CS.VALIDATION_FAILED, CS.NEEDS_REVIEW, CS.REJECTED},
    CS.VALIDATION_FAILED: {CS.PENDING_VALIDATION, CS.NEEDS_REVIEW},
    CS.PENDING_APPROVAL: {CS.APPROVED, CS.REJECTED, CS.NEEDS_REVIEW},
    CS.APPROVED: {CS.ACTIVE},
    CS.ACTIVE: {CS.SUSPENDED, CS.EXPIRED, CS.TERMINATED},
    CS.SUSPENDED: {CS.ACTIVE, CS.TERMINATED, CS.EXPIRED},
    CS.REJECTED: set(), CS.EXPIRED: set(), CS.TERMINATED: set(),
}
POST_EXPIRY_SETTLEMENT_DAYS = 45


def transition(db: Session, actor: Principal, c: Contract, new: str, reason: str | None = None,
               payload: dict | None = None) -> None:
    if new not in ALLOWED_TRANSITIONS.get(c.status, set()):
        raise InvalidState(f"contract cannot move from {c.status} to {new}")
    prev = c.status
    c.status = new
    c.status_reason = reason
    c.updated_at = datetime.now(timezone.utc)
    AuditService(db).record("contract.status_changed", actor=actor, resource_type="contract", resource_id=c.id,
                            contract_id=c.id, previous_state=prev, new_state=new,
                            payload={"reason": reason, **(payload or {})})


def get_contract(db: Session, actor: Principal, contract_id: Any) -> Contract:
    c = abac.load_scoped(db, Contract, contract_id, actor)
    abac.ensure_can_read_contract(actor, c)
    return c


def list_contracts(db: Session, actor: Principal, status: str | None = None, mine: bool = False,
                   limit: int = 50, offset: int = 0) -> tuple[list[Contract], int]:
    q = select(Contract).where(Contract.org_id == actor.org_id)
    if not actor.has("contracts:read_org") or mine:
        abac.require_any(actor, "contracts:read_own", "contracts:read_org")
        q = q.where(Contract.contractor_id == actor.user_id)
    if status:
        q = q.where(Contract.status.in_(status.split(",")))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(Contract.updated_at.desc()).limit(min(limit, 200)).offset(offset)).scalars().unique().all()
    return list(rows), total


# ------------------------------------------------------------------------------------------- upload & OCR
def create_with_document(db: Session, actor: Principal, title: str, filename: str, data: bytes) -> Contract:
    abac.require(actor, "contracts:create")
    upload = storage.validate_upload(filename, data)
    dup = db.execute(select(ContractDocument.contract_id).join(Contract, Contract.id == ContractDocument.contract_id)
                     .where(ContractDocument.org_id == actor.org_id, ContractDocument.sha256 == upload.sha256,
                            ContractDocument.kind == "CONTRACT",
                            Contract.status.not_in([CS.REJECTED, CS.TERMINATED, CS.EXPIRED]))).first()
    if dup:
        AuditService(db).record("contract.duplicate_upload_rejected", actor=actor, outcome="DENIED",
                                resource_type="contract", resource_id=dup[0], payload={"sha256": upload.sha256})
        db.commit()
        raise Conflict("an identical document is already attached to an open contract", code="duplicate_document")
    c = Contract(org_id=actor.org_id, contractor_id=actor.user_id, title=title.strip()[:300] or upload.safe_filename,
                 status=CS.DRAFT, created_by=actor.user_id)
    db.add(c)
    db.flush()
    AuditService(db).record("contract.created", actor=actor, resource_type="contract", resource_id=c.id,
                            contract_id=c.id, new_state=CS.DRAFT, payload={"title": c.title})
    doc = _store_document(db, actor, c, upload, "CONTRACT")
    c.primary_document_id = doc.id
    transition(db, actor, c, CS.UPLOADED, payload={"document_id": str(doc.id), "sha256": upload.sha256})
    job = OcrJob(document_id=doc.id, status=OcrJobStatus.QUEUED, pages_total=upload.page_count)
    db.add(job)
    db.flush()
    after_commit(db, "finguard.worker.jobs.run_ocr_job", job.id)
    return c


def _store_document(db: Session, actor: Principal, c: Contract, upload: storage.ValidatedUpload, kind: str,
                    amendment_id: uuid.UUID | None = None) -> ContractDocument:
    key = storage.store(actor.org_id, upload)
    doc = ContractDocument(org_id=actor.org_id, contract_id=c.id, amendment_id=amendment_id, kind=kind,
                           original_filename=upload.safe_filename, storage_key=key, mime_type=upload.mime_type,
                           size_bytes=len(upload.data), sha256=upload.sha256, page_count=upload.page_count,
                           uploaded_by=actor.user_id)
    db.add(doc)
    db.flush()
    AuditService(db).record("document.uploaded", actor=actor, resource_type="document", resource_id=doc.id,
                            contract_id=c.id, payload={"kind": kind, "mime": upload.mime_type, "sha256": upload.sha256,
                                                       "pages": upload.page_count, "size": len(upload.data)})
    return doc


def process_ocr_job(db: Session, job_id: uuid.UUID, system: Principal) -> OcrJob:
    """Run text extraction + field extraction for one job. Idempotent: a finished job is not re-run."""
    job = db.execute(select(OcrJob).where(OcrJob.id == job_id).with_for_update()).scalar_one()
    if job.status in (OcrJobStatus.SUCCEEDED,):
        return job
    doc = db.get(ContractDocument, job.document_id)
    c = db.get(Contract, doc.contract_id)
    job.status, job.attempts, job.started_at = OcrJobStatus.RUNNING, job.attempts + 1, datetime.now(timezone.utc)
    if doc.kind == "CONTRACT" and c.status in (CS.UPLOADED, CS.OCR_FAILED, CS.NEEDS_REVIEW):
        transition(db, system, c, CS.OCR_PROCESSING, payload={"job_id": str(job.id)})
    db.commit()
    try:
        data = storage.read(doc.storage_key)
        if hashlib_sha256(data) != doc.sha256:
            raise ValidationFailed("stored document failed integrity check")
        layer = extract(data, doc.mime_type)
        day_first = (db.get(Organization, c.org_id).settings or {}).get("date_day_first", True)
        out = run_extraction(layer, day_first=day_first)
        er = persist(db, org_id=c.org_id, document_id=doc.id, ocr_job_id=job.id, layer=layer, out=out)
        job.status, job.method = OcrJobStatus.SUCCEEDED, layer.method
        job.pages_total = len(layer.pages)
        job.pages_ocr = sum(1 for p in layer.pages if p.method == "OCR")
        job.mean_ocr_confidence = Decimal(str(layer.mean_ocr_confidence)) if layer.mean_ocr_confidence is not None else None
        job.finished_at = datetime.now(timezone.utc)
        AuditService(db).record("ocr.completed", actor=system, org_id=c.org_id, resource_type="ocr_job",
                                resource_id=job.id, contract_id=c.id,
                                payload={"method": layer.method, "pages": len(layer.pages), "pages_ocr": job.pages_ocr,
                                         "mean_ocr_confidence": layer.mean_ocr_confidence, "extraction_id": str(er.id),
                                         "extractor": out.extractor, "injection_score": str(out.injection.score),
                                         "quality_flags": out.quality_flags})
        if doc.kind == "CONTRACT":
            c.latest_extraction_id = er.id
            fields = effective_fields(db, er)
            if fields.get("contract_id", {}).get("value") and not c.external_ref:
                c.external_ref = fields["contract_id"]["value"][:120]
            transition(db, system, c, CS.NEEDS_REVIEW, payload={"extraction_id": str(er.id),
                                                                "unresolved_critical": unresolved_critical(fields)})
            notify_user(db, c.org_id, c.contractor_id, "ocr.completed", "Contract fields extracted",
                        f"Review the extracted fields for “{c.title}” before submitting.", f"/contracts/{c.id}")
        else:
            from finguard.services.amendments import on_amendment_document_extracted

            on_amendment_document_extracted(db, system, doc, er)
        db.commit()
    except Exception as exc:  # any failure leaves a FAILED job and OCR_FAILED contract, never a half-state
        db.rollback()
        job = db.get(OcrJob, job_id)
        c = db.get(Contract, doc.contract_id)
        job.status, job.error, job.finished_at = OcrJobStatus.FAILED, str(exc)[:500], datetime.now(timezone.utc)
        if doc.kind == "CONTRACT" and c.status == CS.OCR_PROCESSING:
            transition(db, system, c, CS.OCR_FAILED, reason=str(exc)[:300])
            notify_user(db, c.org_id, c.contractor_id, "ocr.failed", "Document processing failed", str(exc)[:300],
                        f"/contracts/{c.id}")
        AuditService(db).record("ocr.failed", actor=system, org_id=c.org_id, resource_type="ocr_job", resource_id=job.id,
                                contract_id=c.id, outcome="FAILURE", payload={"error": str(exc)[:300]})
        db.commit()
    return job


def hashlib_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def retry_ocr(db: Session, actor: Principal, contract_id: Any) -> OcrJob:
    c = get_contract(db, actor, contract_id)
    if c.contractor_id != actor.user_id and not actor.has("contracts:validate"):
        raise Forbidden("not permitted")
    if c.status != CS.OCR_FAILED:
        raise InvalidState("OCR can be retried only after a failure")
    job = OcrJob(document_id=c.primary_document_id, status=OcrJobStatus.QUEUED)
    db.add(job)
    db.flush()
    after_commit(db, "finguard.worker.jobs.run_ocr_job", job.id)
    return job


# ------------------------------------------------------------------------------------------- review & correction
def latest_extraction(db: Session, c: Contract) -> ExtractionResult | None:
    return db.get(ExtractionResult, c.latest_extraction_id) if c.latest_extraction_id else None


def correct_field(db: Session, actor: Principal, contract_id: Any, field_name: str, value: str | None,
                  reason: str) -> dict:
    c = get_contract(db, actor, contract_id)
    if field_name not in FIELD_MAP:
        raise ValidationFailed(f"unknown field {field_name}")
    if not reason or len(reason.strip()) < 3:
        raise ValidationFailed("a correction reason is required")
    is_owner = c.contractor_id == actor.user_id
    if is_owner and actor.has("contracts:correct_fields_own") and c.status in (CS.NEEDS_REVIEW, CS.VALIDATION_FAILED):
        pass
    elif actor.has("contracts:correct_fields") and not is_owner and c.status == CS.PENDING_VALIDATION:
        pass
    else:
        AuditService(db).denied(actor, "contract.correct_field", f"status {c.status}", resource_type="contract",
                                resource_id=c.id)
        raise Forbidden("fields can be corrected by the contractor during review or by a validator during validation")
    er = latest_extraction(db, c)
    if er is None:
        raise InvalidState("no extraction to correct")
    try:
        normalized = normalize_value(field_name, value)
    except P.ParseError as exc:
        raise ValidationFailed(str(exc)) from exc
    current = effective_fields(db, er)[field_name]["value"]
    fc = FieldCorrection(extraction_id=er.id, field_name=field_name, previous_value=current, new_value=normalized,
                         reason=reason.strip()[:1000], corrected_by=actor.user_id)
    db.add(fc)
    db.flush()
    AuditService(db).record("extraction.field_corrected", actor=actor, resource_type="extraction", resource_id=er.id,
                            contract_id=c.id, payload={"field": field_name, "previous": current, "new": normalized,
                                                       "reason": reason})
    return effective_fields(db, er)[field_name]


def _version_from_fields(db: Session, actor: Principal, c: Contract, fields: dict[str, dict], org: Organization) -> ContractVersion:
    v = lambda k: fields.get(k, {}).get("value")  # noqa: E731
    start = date.fromisoformat(v("start_date"))
    end = date.fromisoformat(v("end_date"))
    freq = v("frequency")
    terms = {
        "contractor_legal_name": v("contractor_legal_name"), "counterparty_name": v("counterparty_name"),
        "total_value": Decimal(v("total_value")) if v("total_value") else None,
        "payment_amount": Decimal(v("payment_amount")), "currency": v("currency"), "frequency": freq,
        "interval_count": int(v("interval_count") or 1), "custom_unit": v("custom_unit"),
        "due_day_of_month": int(v("due_day_of_month")) if v("due_day_of_month") else None,
        "due_weekday": int(v("due_weekday")) if v("due_weekday") else None, "start_date": start, "end_date": end,
        "first_payment_date": date.fromisoformat(v("first_payment_date")) if v("first_payment_date") else None,
        "payment_timing": v("payment_timing") or "ARREARS", "grace_period_days": int(v("grace_period_days") or 0),
        "proration_rule": v("proration_rule") or "NONE",
        "business_day_rule": (org.settings or {}).get("business_day_rule", "FOLLOWING"),
        "timezone": org.timezone, "invoice_required": v("invoice_required") == "true",
        "auto_execute_permitted": v("auto_execute_permitted") == "true",
        "tax_withholding_note": v("tax_withholding"), "termination_terms": v("termination_terms"),
        "notice_period_days": int(v("notice_period_days")) if v("notice_period_days") else None,
        "effective_from": None,
    }
    for old in db.execute(select(ContractVersion).where(ContractVersion.contract_id == c.id,
                                                        ContractVersion.status == VersionStatus.PROPOSED)).scalars():
        old.status = VersionStatus.SUPERSEDED
    cv = ContractVersion(contract_id=c.id, status=VersionStatus.PROPOSED, source_extraction_id=c.latest_extraction_id,
                         terms_hash=terms_hash(terms), created_by=actor.user_id, **terms)
    db.add(cv)
    db.flush()
    return cv


def submit(db: Session, actor: Principal, contract_id: Any) -> Contract:
    c = get_contract(db, actor, contract_id)
    abac.require(actor, "contracts:submit_own")
    abac.ensure_owner(actor, c)
    if c.status not in (CS.NEEDS_REVIEW, CS.VALIDATION_FAILED):
        raise InvalidState(f"contract in {c.status} cannot be submitted")
    er = latest_extraction(db, c)
    fields = effective_fields(db, er)
    unresolved = unresolved_critical(fields)
    if unresolved:
        raise ValidationFailed("resolve all critical fields before submitting", code="unresolved_fields",
                               details={"unresolved": unresolved})
    org = db.get(Organization, c.org_id)
    try:
        cv = _version_from_fields(db, actor, c, fields, org)
    except (ValueError, TypeError) as exc:
        raise ValidationFailed(f"extracted terms are inconsistent: {exc}") from exc
    c.submitted_at = datetime.now(timezone.utc)
    transition(db, actor, c, CS.PENDING_VALIDATION, payload={"proposed_version_id": str(cv.id), "terms_hash": cv.terms_hash})
    notify_roles(db, c.org_id, [RoleName.FINANCE_ANALYST], "contract.submitted", "Contract awaiting validation",
                 f"“{c.title}” was submitted by {actor.full_name}.", f"/contracts/{c.id}")
    return c


# ------------------------------------------------------------------------------------------- validation
def proposed_version(db: Session, c: Contract) -> ContractVersion | None:
    return db.execute(select(ContractVersion).where(ContractVersion.contract_id == c.id,
                                                    ContractVersion.status == VersionStatus.PROPOSED)
                      .order_by(ContractVersion.created_at.desc())).scalars().first()


def validation_checks(db: Session, c: Contract, cv: ContractVersion, fields: dict[str, dict]) -> list[dict]:
    org = db.get(Organization, c.org_id)
    er = latest_extraction(db, c)
    checks: list[dict] = []

    def add(check_id: str, ok: bool, msg: str, severity: str = "error") -> None:
        checks.append({"id": check_id, "passed": bool(ok), "severity": severity, "message": msg})

    unresolved = unresolved_critical(fields)
    add("critical_fields", not unresolved, "All critical payment fields resolved" if not unresolved
        else f"Unresolved: {', '.join(unresolved)}")
    add("dates", cv.end_date is not None and cv.end_date >= cv.start_date, "End date after start date")
    add("currency_supported", cv.currency in (active_policy(db, c.org_id).config.get("fx_to_base") or {}),
        f"Currency {cv.currency} supported")
    try:
        occ = generate(terms_of(cv), org_calendar(org))
        add("schedule_generates", bool(occ), f"Schedule generates {len(occ)} occurrence(s)")
        total = schedule_total(terms_of(cv), org_calendar(org))
        if cv.total_value is not None:
            add("total_value_consistent", total <= cv.total_value,
                f"Scheduled total {total} vs stated total value {cv.total_value}")
            if total < cv.total_value:
                add("total_value_gap", False, f"Scheduled total {total} is below stated total {cv.total_value}", "warning")
    except ScheduleError as exc:
        add("schedule_generates", False, f"Schedule cannot be generated: {exc}")
    party = (cv.contractor_legal_name or "").lower()
    contractor = c.contractor
    names = {contractor.full_name.lower(), (contractor.company_name or "").lower()} - {""}
    add("party_identity", any(n and (n in party or party in n) for n in names),
        f"Contract party “{cv.contractor_legal_name}” vs registered contractor “{contractor.full_name}”"
        + (f" / “{contractor.company_name}”" if contractor.company_name else ""), "warning")
    inj = (er.injection_report or {}) if er else {}
    add("no_injection", not inj.get("detected"), "No instruction-like content aimed at AI systems"
        if not inj.get("detected") else f"Instruction-like content detected (score {inj.get('score')})", "warning")
    dup = _duplicate_active(db, c, cv)
    add("no_duplicate_active", not dup, "No conflicting active contract" if not dup else
        f"Conflicting active contract {dup}")
    add("signature", fields.get("signature_present", {}).get("value") == "true", "Signature block detected", "warning")
    for flag in (er.quality_flags if er else []):
        add("quality", False, flag, "warning")
    return checks


def _duplicate_active(db: Session, c: Contract, cv: ContractVersion) -> str | None:
    q = (select(Contract.id).join(ContractVersion, ContractVersion.id == Contract.current_version_id)
         .where(Contract.org_id == c.org_id, Contract.contractor_id == c.contractor_id, Contract.id != c.id,
                Contract.status.in_([CS.ACTIVE, CS.SUSPENDED, CS.APPROVED]),
                ContractVersion.start_date <= (cv.end_date or cv.start_date),
                (ContractVersion.end_date.is_(None)) | (ContractVersion.end_date >= cv.start_date)))
    if c.external_ref:
        q2 = select(Contract.id).where(Contract.org_id == c.org_id, Contract.external_ref == c.external_ref,
                                       Contract.id != c.id, Contract.status.in_([CS.ACTIVE, CS.SUSPENDED, CS.APPROVED]))
        if r := db.execute(q2).first():
            return str(r[0])
    rows = db.execute(q).all()
    for (cid,) in rows:
        other = db.get(Contract, cid)
        ov = other.current_version
        if ov and ov.payment_amount == cv.payment_amount and ov.frequency == cv.frequency:
            return str(cid)
    return None


def validate(db: Session, actor: Principal, contract_id: Any, decision: str, reason: str) -> Contract:
    c = get_contract(db, actor, contract_id)
    abac.require(actor, "contracts:validate")
    abac.ensure_not_self(actor, c.contractor_id, what="validate your own contract")
    if c.status != CS.PENDING_VALIDATION:
        raise InvalidState(f"contract is {c.status}, not PENDING_VALIDATION")
    if decision not in ("PASSED", "FAILED", "CHANGES_REQUESTED"):
        raise ValidationFailed("decision must be PASSED, FAILED or CHANGES_REQUESTED")
    if not reason or len(reason.strip()) < 3:
        raise ValidationFailed("a validation reason is required")
    er = latest_extraction(db, c)
    fields = effective_fields(db, er)
    org = db.get(Organization, c.org_id)
    # Rebuild the proposed version so validator corrections are reflected in the terms being approved.
    if unresolved_critical(fields):
        if decision == "PASSED":
            raise ValidationFailed("cannot pass validation with unresolved critical fields",
                                   details={"unresolved": unresolved_critical(fields)})
        cv = proposed_version(db, c)
    else:
        cv = _version_from_fields(db, actor, c, fields, org)
    checks = validation_checks(db, c, cv, fields) if cv else []
    errors = [ch for ch in checks if not ch["passed"] and ch["severity"] == "error"]
    if decision == "PASSED" and errors:
        raise ValidationFailed("validation checks failed", details={"failed": errors})
    db.add(ContractValidation(contract_id=c.id, contract_version_id=cv.id, validator_id=actor.user_id,
                              decision=decision, checks=checks, reason=reason.strip()))
    AuditService(db).record("contract.validation", actor=actor, resource_type="contract", resource_id=c.id,
                            contract_id=c.id, new_state=decision,
                            payload={"version_id": str(cv.id), "checks": checks, "reason": reason})
    if decision == "FAILED":
        transition(db, actor, c, CS.VALIDATION_FAILED, reason)
        notify_user(db, c.org_id, c.contractor_id, "contract.validation_failed", "Contract validation failed", reason,
                    f"/contracts/{c.id}")
        return c
    if decision == "CHANGES_REQUESTED":
        transition(db, actor, c, CS.NEEDS_REVIEW, reason)
        notify_user(db, c.org_id, c.contractor_id, "contract.changes_requested", "Changes requested", reason,
                    f"/contracts/{c.id}")
        return c
    transition(db, actor, c, CS.PENDING_APPROVAL, reason, payload={"version_id": str(cv.id)})
    _route_for_approval(db, actor, c, cv, fields)
    return c


def _snapshot(db: Session, c: Contract, cv: ContractVersion, fields: dict, policy: dict, actor: Principal | None,
              preview: bool) -> ContractApprovalSnapshot:
    org = db.get(Organization, c.org_id)
    er = latest_extraction(db, c)
    job = db.execute(select(OcrJob).where(OcrJob.id == er.ocr_job_id)).scalar_one() if er else None
    try:
        commitment = cv.total_value or schedule_total(terms_of(cv), org_calendar(org))
    except ScheduleError:
        commitment = Decimal(cv.payment_amount) * 12
    validation = db.execute(select(ContractValidation).where(ContractValidation.contract_id == c.id)
                            .order_by(ContractValidation.created_at.desc())).scalars().first()
    months = (relativedelta(cv.end_date, cv.start_date).years * 12 + relativedelta(cv.end_date, cv.start_date).months) if cv.end_date else 120
    evidence = [{"type": "extracted_field", "field": k, "page": f.get("source_page"), "text": (f.get("source_text") or "")[:160],
                 "confidence": f.get("extraction_confidence"), "corrected": f.get("corrected")}
                for k, f in fields.items() if f.get("is_critical") and f.get("value") is not None]
    if c.primary_document_id:
        doc = db.get(ContractDocument, c.primary_document_id)
        evidence.append({"type": "document", "document_id": str(doc.id), "sha256": doc.sha256})
    return ContractApprovalSnapshot(
        commitment_base=to_base(Decimal(commitment), cv.currency, policy),
        payment_amount_base=to_base(Decimal(cv.payment_amount), cv.currency, policy),
        contract_status=c.status,
        approver_authorized=bool(actor and actor.has("contracts:approve")),
        same_org=actor is None or actor.org_id == c.org_id,
        approver_is_contractor=bool(actor and actor.user_id == c.contractor_id),
        approver_is_validator=bool(actor and validation and validation.validator_id == actor.user_id),
        allow_validator_as_approver=bool(policy.get("allow_validator_as_approver")),
        validation_passed=bool(validation and validation.decision == "PASSED"),
        unresolved_critical_fields=unresolved_critical(fields),
        low_confidence_fields=[k for k, f in fields.items() if f["status"] == "LOW_CONFIDENCE"],
        conflicting_fields=[k for k, f in fields.items() if f["status"] == "CONFLICT"],
        corrected_fields=sum(1 for f in fields.values() if f.get("corrected")),
        injection_detected=bool((er.injection_report or {}).get("detected")) if er else False,
        duplicate_active_contract=_duplicate_active(db, c, cv) is not None,
        dates_valid=cv.end_date is not None and cv.end_date >= cv.start_date,
        auto_execute_requested=cv.auto_execute_permitted, duration_months=months,
        bank_details_in_document=fields.get("bank_details_present", {}).get("value") == "true",
        signatures_detected=(fields.get("signature_present", {}).get("value") == "true") if fields.get("signature_present") else None,
        ocr_used=bool(job and job.method in ("OCR", "MIXED")),
        ocr_mean_confidence=Decimal(job.mean_ocr_confidence) if job and job.mean_ocr_confidence is not None else None,
        evidence_refs=evidence, preview=preview,
    )


def _route_for_approval(db: Session, actor: Principal, c: Contract, cv: ContractVersion, fields: dict) -> ApprovalRequest:
    pv = active_policy(db, c.org_id)
    a = assess_contract_approval(_snapshot(db, c, cv, fields, pv.config, None, preview=True), pv.config)
    gd = record_assessment(db, actor, a, pv, resource_type="contract", resource_id=c.id, contract_id=c.id)
    o = a.outcome
    if o.decision == Decision.BLOCK:
        transition(db, actor, c, CS.REJECTED, "; ".join(o.reasons)[:500])
        notify_user(db, c.org_id, c.contractor_id, "contract.rejected", "Contract rejected by policy", o.reasons[0],
                    f"/contracts/{c.id}")
        return None  # type: ignore[return-value]
    commitment = Decimal(a.inputs["commitment_base"])
    role = RoleName.FINANCE_MANAGER if commitment > Decimal(str(pv.config["contract_manager_threshold"])) else RoleName.FINANCE_APPROVER
    kind = ApprovalKind.ESCALATION if o.decision == Decision.ESCALATE else ApprovalKind.CONTRACT_APPROVAL
    if o.decision == Decision.ESCALATE:
        role = RoleName.RISK_OFFICER
    ar = ApprovalRequest(org_id=c.org_id, kind=kind, resource_type="contract", resource_id=c.id, contract_id=c.id,
                         governance_decision_id=gd.id, required_role=role, required_approvals=1,
                         amount=cv.total_value or cv.payment_amount, currency=cv.currency,
                         title=f"{'Escalated' if kind == ApprovalKind.ESCALATION else 'Approve'} contract: {c.title}",
                         summary=f"{cv.payment_amount} {cv.currency} {cv.frequency.lower()} · {cv.start_date} → {cv.end_date} · "
                                 f"risk {a.score} · {o.decision.value}", requested_by=actor.user_id)
    db.add(ar)
    db.flush()
    AuditService(db).record("approval.requested", actor=actor, resource_type="approval_request", resource_id=ar.id,
                            contract_id=c.id, new_state=ApprovalStatus.OPEN,
                            payload={"kind": kind, "required_role": role, "decision_id": str(gd.id)})
    notify_roles(db, c.org_id, [role], "approval.requested", ar.title, ar.summary, f"/approvals/{ar.id}",
                 exclude=[c.contractor_id, actor.user_id])
    return ar


# ------------------------------------------------------------------------------------------- approval & activation
def decide_contract(db: Session, actor: Principal, ar: ApprovalRequest, decision: str, reason: str,
                    escalation_cleared: bool = False) -> None:
    """Called by the approvals service for CONTRACT_APPROVAL requests (permission & SoD re-checked here)."""
    c = db.get(Contract, ar.resource_id)
    abac.ensure_same_org(actor, c.org_id)
    abac.require(actor, "contracts:approve")
    cv = proposed_version(db, c)
    if cv is None:
        raise InvalidState("no proposed version to approve")
    pv = active_policy(db, c.org_id)
    fields = effective_fields(db, latest_extraction(db, c))
    a = assess_contract_approval(_snapshot(db, c, cv, fields, pv.config, actor, preview=False), pv.config,
                                 {"escalation_cleared": escalation_cleared})
    gd = record_assessment(db, actor, a, pv, resource_type="contract", resource_id=c.id, contract_id=c.id)
    blocks = [r for r in a.rules if not r.passed and r.on_fail == Decision.BLOCK.value]
    if decision == "APPROVE":
        if blocks:
            AuditService(db).denied(actor, "contract.approve", "; ".join(r.message for r in blocks),
                                    resource_type="contract", resource_id=c.id, contract_id=c.id)
            raise Forbidden(blocks[0].message, code=blocks[0].rule_id.lower(), details={"decision_id": str(gd.id)})
        if a.outcome.decision in (Decision.ESCALATE, Decision.VERIFY):
            raise InvalidState(f"policy requires {a.outcome.decision.value} before approval: {a.outcome.reasons[0]}")
        commitment = Decimal(a.inputs["commitment_base"])
        limit = abac.approval_limit_for(actor.roles, pv.config["approval_limits"])
        if commitment > Decimal(str(pv.config["contract_manager_threshold"])) and not actor.has_role(RoleName.FINANCE_MANAGER):
            raise Forbidden("contract commitment requires a Finance Manager", code="approval_limit")
        if Decimal(cv.payment_amount) and to_base(Decimal(cv.payment_amount), cv.currency, pv.config) > limit:
            raise Forbidden("per-payment amount exceeds your approval limit", code="approval_limit")
    db.add(ContractApproval(contract_id=c.id, contract_version_id=cv.id, approver_id=actor.user_id,
                            decision="APPROVED" if decision == "APPROVE" else "REJECTED", reason=reason,
                            risk_assessment_id=gd.risk_assessment_id))
    if decision == "REJECT":
        cv.status = VersionStatus.REJECTED
        transition(db, actor, c, CS.REJECTED, reason)
        notify_user(db, c.org_id, c.contractor_id, "contract.rejected", "Contract rejected", reason, f"/contracts/{c.id}")
        return
    now = datetime.now(timezone.utc)
    cv.status, cv.version_no, cv.approved_by, cv.approved_at = VersionStatus.APPROVED, 1, actor.user_id, now
    c.current_version_id = cv.id
    transition(db, actor, c, CS.APPROVED, reason, payload={"version_id": str(cv.id), "terms_hash": cv.terms_hash,
                                                           "decision_id": str(gd.id)})
    activate(db, actor, c)


def activate(db: Session, actor: Principal, c: Contract) -> PaymentSchedule:
    if c.status != CS.APPROVED:
        raise InvalidState("only approved contracts can be activated")
    cv = db.get(ContractVersion, c.current_version_id)
    if cv is None or cv.status != VersionStatus.APPROVED:
        raise InvalidState("contract has no approved version")
    sched = create_schedule(db, actor, c, cv, cv.start_date)
    c.activated_at = datetime.now(timezone.utc)
    transition(db, actor, c, CS.ACTIVE, payload={"schedule_id": str(sched.id)})
    notify_user(db, c.org_id, c.contractor_id, "contract.active", "Contract approved and active",
                f"“{c.title}” is active. Your payment schedule is available.", f"/contracts/{c.id}")
    return sched


def create_schedule(db: Session, actor: Principal, c: Contract, cv: ContractVersion, effective_from: date) -> PaymentSchedule:
    org = db.get(Organization, c.org_id)
    t = terms_of(cv)
    occ = generate(t, org_calendar(org), from_date=effective_from)
    sched = PaymentSchedule(contract_id=c.id, contract_version_id=cv.id, effective_from=effective_from,
                            generator_version=GENERATOR_VERSION, created_by=actor.user_id,
                            parameters={"terms": {k: str(v) if v is not None else None for k, v in t.__dict__.items()},
                                        "calendar": {"holidays": sorted((org.settings or {}).get("holidays", [])),
                                                     "execution_hour": org_calendar(org).execution_hour}})
    db.add(sched)
    db.flush()
    # Sequence numbers continue after the retained (not superseded) history.
    seq_offset = db.execute(select(func.count()).select_from(ScheduledPayment).where(
        ScheduledPayment.contract_id == c.id, ScheduledPayment.period_start < effective_from,
        ScheduledPayment.status.not_in([SPS.SUPERSEDED, SPS.CANCELLED]))).scalar_one()
    for i, o in enumerate(occ, start=1):
        db.add(ScheduledPayment(
            org_id=c.org_id, schedule_id=sched.id, contract_id=c.id, contract_version_id=cv.id,
            sequence_no=seq_offset + i, period_start=o.period_start,
            period_end=o.period_end, nominal_date=o.nominal_date, due_date=o.due_date, due_at=o.due_at,
            amount=o.amount, currency=cv.currency, is_prorated=o.is_prorated, status=SPS.SCHEDULED,
            idempotency_key=f"sp-{c.id.hex[:12]}-{o.period_start:%Y%m%d}-v{cv.version_no}"))
    db.flush()
    AuditService(db).record("schedule.generated", actor=actor, resource_type="payment_schedule", resource_id=sched.id,
                            contract_id=c.id, payload={"version_id": str(cv.id), "occurrences": len(occ),
                                                       "effective_from": effective_from.isoformat(),
                                                       "total": str(sum((o.amount for o in occ), Decimal(0))),
                                                       "generator": GENERATOR_VERSION})
    return sched


# ------------------------------------------------------------------------------------------- holds & expiry
def set_hold(db: Session, actor: Principal, contract_id: Any, action: str, reason: str) -> Contract:
    c = get_contract(db, actor, contract_id)
    abac.require(actor, "contracts:suspend")
    if not reason or len(reason) < 3:
        raise ValidationFailed("a reason is required")
    target = {"SUSPEND": CS.SUSPENDED, "REACTIVATE": CS.ACTIVE, "TERMINATE": CS.TERMINATED}.get(action)
    if target is None:
        raise ValidationFailed("action must be SUSPEND, REACTIVATE or TERMINATE")
    transition(db, actor, c, target, reason)
    if target == CS.TERMINATED:
        for sp in db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id,
                                                            ScheduledPayment.status.in_([SPS.SCHEDULED, SPS.HELD]),
                                                            ScheduledPayment.period_start > date.today())).scalars():
            sp.status, sp.hold_reason = SPS.CANCELLED, f"contract terminated: {reason}"
    if target == CS.ACTIVE:
        for sp in db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id,
                                                            ScheduledPayment.status.in_([SPS.HELD, SPS.ESCALATED]))).scalars():
            sp.status = SPS.SCHEDULED
    notify_user(db, c.org_id, c.contractor_id, "contract.status", f"Contract {target.lower()}", reason, f"/contracts/{c.id}")
    return c


def expire_contracts(db: Session, system_for_org) -> int:
    today = date.today()
    n = 0
    rows = db.execute(select(Contract).join(ContractVersion, ContractVersion.id == Contract.current_version_id)
                      .where(Contract.status.in_([CS.ACTIVE, CS.SUSPENDED]),
                             ContractVersion.end_date < today - timedelta(days=POST_EXPIRY_SETTLEMENT_DAYS))).scalars().unique().all()
    for c in rows:
        transition(db, system_for_org(c.org_id), c, CS.EXPIRED, "end date plus settlement window passed")
        n += 1
    return n
