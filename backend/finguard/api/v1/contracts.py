"""Contracts, documents, extraction review, validation, versions, schedules, amendments, beneficiaries."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.api.deps import current_principal
from finguard.api.schemas import AmendmentIn, BeneficiaryIn, FieldCorrectionIn, HoldIn, ValidationIn, VerifyIn
from finguard.api.serializers import (amendment_view, contract_view, document_view, ocr_job_view, payment_view,
                                      version_view)
from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.config import get_settings
from finguard.core.db import get_db
from finguard.core.errors import NotFound, RateLimited, ValidationFailed
from finguard.core.ratelimit import hit
from finguard.documents import storage
from finguard.documents.text_extraction import render_page_png
from finguard.extraction.pipeline import effective_fields, unresolved_critical
from finguard.governance.service import decision_view
from finguard.models import (ApprovalRequest, ContractAmendment, ContractApproval, ContractDocument, ContractValidation,
                             ContractVersion, ExtractionResult, GovernanceDecision, OcrJob, PolicyVersion, RiskAssessment,
                             ScheduledPayment)
from finguard.services import amendments as amend_svc
from finguard.services import beneficiaries as ben_svc
from finguard.services import contracts as svc

router = APIRouter(tags=["contracts"])


async def _read_upload(file: UploadFile) -> bytes:
    limit = get_settings().max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed("file too large")
    return data


@router.post("/contracts", status_code=201)
async def upload_contract(title: str = Form(..., min_length=2, max_length=300), file: UploadFile = File(...),
                          p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    if not hit(f"upload:{p.user_id}", 20, 3600):
        raise RateLimited("upload limit reached")
    data = await _read_upload(file)
    c = svc.create_with_document(db, p, title, file.filename or "document", data)
    db.commit()
    db.refresh(c)
    return contract_view(c, p)


@router.get("/contracts")
def list_contracts(status: str | None = None, mine: bool = False, limit: int = Query(50, le=200), offset: int = 0,
                   p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    rows, total = svc.list_contracts(db, p, status, mine, limit, offset)
    return {"items": [contract_view(c, p) for c in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    c = svc.get_contract(db, p, contract_id)
    out = contract_view(c, p)
    docs = db.execute(select(ContractDocument).where(ContractDocument.contract_id == c.id)
                      .order_by(ContractDocument.created_at)).scalars().all()
    out["documents"] = [document_view(d) for d in docs]
    jobs = db.execute(select(OcrJob).where(OcrJob.document_id.in_([d.id for d in docs]))
                      .order_by(OcrJob.created_at.desc())).scalars().all() if docs else []
    out["ocr_jobs"] = [ocr_job_view(j) for j in jobs]
    pv = svc.proposed_version(db, c)
    out["proposed_version"] = version_view(pv)
    out["validations"] = [{"id": str(v.id), "decision": v.decision, "reason": v.reason, "checks": v.checks,
                           "validator_id": str(v.validator_id), "created_at": v.created_at.isoformat()}
                          for v in db.execute(select(ContractValidation).where(ContractValidation.contract_id == c.id)
                                              .order_by(ContractValidation.created_at)).scalars()]
    out["approvals"] = [{"id": str(a.id), "decision": a.decision, "reason": a.reason, "approver_id": str(a.approver_id),
                         "version_id": str(a.contract_version_id), "amendment_id": str(a.amendment_id) if a.amendment_id else None,
                         "created_at": a.created_at.isoformat()}
                        for a in db.execute(select(ContractApproval).where(ContractApproval.contract_id == c.id)
                                            .order_by(ContractApproval.created_at)).scalars()]
    out["open_requests"] = [{"id": str(r.id), "kind": r.kind, "required_role": r.required_role, "title": r.title}
                            for r in db.execute(select(ApprovalRequest).where(ApprovalRequest.contract_id == c.id,
                                                                              ApprovalRequest.status == "OPEN")).scalars()]
    if p.has("risk:read"):
        gds = db.execute(select(GovernanceDecision).join(RiskAssessment).where(
            RiskAssessment.resource_type == "contract", RiskAssessment.resource_id == c.id)
            .order_by(GovernanceDecision.created_at.desc()).limit(1)).scalars().all()
        out["latest_decision"] = decision_view(gds[0], db.get(PolicyVersion, gds[0].risk_assessment.policy_version_id).version) if gds else None
    return out


@router.get("/contracts/{contract_id}/extraction")
def get_extraction(contract_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    c = svc.get_contract(db, p, contract_id)
    er = svc.latest_extraction(db, c)
    if er is None:
        return {"extraction": None}
    fields = effective_fields(db, er)
    return {"extraction": {"id": str(er.id), "extractor": er.extractor, "model_name": er.model_name,
                           "prompt_version": er.prompt_version, "quality_flags": er.quality_flags,
                           "injection_report": er.injection_report, "created_at": er.created_at.isoformat(),
                           "document_id": str(er.document_id),
                           "pages": [{"page": pg["page"], "method": pg["method"], "ocr_confidence": pg.get("ocr_confidence"),
                                      "text": pg["text"]} for pg in er.pages]},
            "fields": list(fields.values()), "unresolved_critical": unresolved_critical(fields)}


@router.post("/contracts/{contract_id}/fields")
def correct_field(contract_id: str, body: FieldCorrectionIn, p: Principal = Depends(current_principal),
                  db: Session = Depends(get_db)) -> dict:
    f = svc.correct_field(db, p, contract_id, body.field_name, body.value, body.reason)
    db.commit()
    return f


@router.post("/contracts/{contract_id}/submit")
def submit(contract_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    c = svc.submit(db, p, contract_id)
    db.commit()
    return contract_view(c, p)


@router.post("/contracts/{contract_id}/retry-ocr")
def retry_ocr(contract_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    job = svc.retry_ocr(db, p, contract_id)
    db.commit()
    return ocr_job_view(job)


@router.get("/contracts/{contract_id}/validation-checks")
def validation_preview(contract_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "contracts:validate")
    c = svc.get_contract(db, p, contract_id)
    cv = svc.proposed_version(db, c)
    if cv is None:
        return {"checks": []}
    return {"checks": svc.validation_checks(db, c, cv, effective_fields(db, svc.latest_extraction(db, c)))}


@router.post("/contracts/{contract_id}/validate")
def validate(contract_id: str, body: ValidationIn, p: Principal = Depends(current_principal),
             db: Session = Depends(get_db)) -> dict:
    c = svc.validate(db, p, contract_id, body.decision, body.reason)
    db.commit()
    return contract_view(c, p)


@router.post("/contracts/{contract_id}/hold")
def hold(contract_id: str, body: HoldIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    c = svc.set_hold(db, p, contract_id, body.action, body.reason)
    db.commit()
    return contract_view(c, p)


@router.get("/contracts/{contract_id}/versions")
def versions(contract_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict]:
    c = svc.get_contract(db, p, contract_id)
    rows = db.execute(select(ContractVersion).where(ContractVersion.contract_id == c.id)
                      .order_by(ContractVersion.created_at)).scalars().all()
    return [version_view(v) for v in rows]


@router.get("/contracts/{contract_id}/schedule")
def schedule(contract_id: str, include_superseded: bool = False, p: Principal = Depends(current_principal),
             db: Session = Depends(get_db)) -> list[dict]:
    c = svc.get_contract(db, p, contract_id)
    q = select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id)
    if not include_superseded:
        q = q.where(ScheduledPayment.status != "SUPERSEDED")
    return [payment_view(sp) for sp in db.execute(q.order_by(ScheduledPayment.period_start, ScheduledPayment.created_at)).scalars()]


@router.get("/documents/{document_id}/pages/{page}")
def document_page(document_id: str, page: int, p: Principal = Depends(current_principal),
                  db: Session = Depends(get_db)) -> Response:
    doc = abac.load_scoped(db, ContractDocument, document_id, p)
    svc.get_contract(db, p, doc.contract_id)
    try:
        png = render_page_png(storage.read(doc.storage_key), doc.mime_type, page)
    except ValueError as exc:
        raise NotFound(str(exc)) from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300",
                                                          "X-Content-Type-Options": "nosniff"})


@router.get("/documents/{document_id}/download")
def document_download(document_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> Response:
    doc = abac.load_scoped(db, ContractDocument, document_id, p)
    svc.get_contract(db, p, doc.contract_id)
    return Response(storage.read(doc.storage_key), media_type=doc.mime_type,
                    headers={"Content-Disposition": f'attachment; filename="{doc.original_filename}"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


# ------------------------------------------------------------------ amendments
@router.post("/contracts/{contract_id}/amendments", status_code=201)
async def request_amendment(contract_id: str, payload: str = Form(...), file: UploadFile | None = File(None),
                            p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    try:
        body = AmendmentIn.model_validate(json.loads(payload))
    except (ValueError, TypeError) as exc:
        raise ValidationFailed(f"invalid amendment payload: {exc}") from exc
    doc = (file.filename or "amendment", await _read_upload(file)) if file else None
    am = amend_svc.request_amendment(db, p, contract_id, body.changes, body.reason, body.effective_date, doc)
    db.commit()
    db.refresh(am)
    return amendment_view(am)


@router.get("/contracts/{contract_id}/amendments")
def list_amendments(contract_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict]:
    c = svc.get_contract(db, p, contract_id)
    return [amendment_view(a) for a in db.execute(select(ContractAmendment).where(ContractAmendment.contract_id == c.id)
                                                  .order_by(ContractAmendment.created_at.desc())).scalars()]


@router.get("/amendments/{amendment_id}")
def get_amendment(amendment_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    am = abac.load_scoped(db, ContractAmendment, amendment_id, p)
    c = svc.get_contract(db, p, am.contract_id)
    out = amendment_view(am)
    out["base_version"] = version_view(db.get(ContractVersion, am.base_version_id))
    out["proposed_version"] = version_view(db.get(ContractVersion, am.proposed_version_id)) if am.proposed_version_id else None
    if am.extraction_id:
        out["document_fields"] = {k: v["value"] for k, v in effective_fields(db, db.get(ExtractionResult, am.extraction_id)).items()
                                  if v["value"] is not None}
    future = db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id,
                                                       ScheduledPayment.period_start >= am.requested_effective_date)
                        .order_by(ScheduledPayment.period_start)).scalars().all()
    out["affected_occurrences"] = [{"id": str(s.id), "period_start": s.period_start.isoformat(), "amount": str(s.amount),
                                    "status": s.status} for s in future[:24]]
    return out


@router.post("/amendments/{amendment_id}/document")
async def amendment_document(amendment_id: str, file: UploadFile = File(...), p: Principal = Depends(current_principal),
                             db: Session = Depends(get_db)) -> dict:
    am = abac.load_scoped(db, ContractAmendment, amendment_id, p)
    amend_svc.attach_document(db, p, am, file.filename or "amendment", await _read_upload(file))
    db.commit()
    return amendment_view(am)


@router.post("/amendments/{amendment_id}/withdraw")
def withdraw_amendment(amendment_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    am = amend_svc.withdraw(db, p, amendment_id)
    db.commit()
    return amendment_view(am)


# ------------------------------------------------------------------ beneficiaries
@router.post("/contracts/{contract_id}/beneficiary", status_code=201)
def add_beneficiary(contract_id: str, body: BeneficiaryIn, p: Principal = Depends(current_principal),
                    db: Session = Depends(get_db)) -> dict:
    b = ben_svc.register_beneficiary(db, p, contract_id, body.account_holder_name, body.account_number, body.ifsc)
    db.commit()
    return {"id": str(b.id), "account_masked": f"XXXX{b.account_last4}", "verified": b.verified}


@router.post("/beneficiaries/{beneficiary_id}/verify")
def verify_beneficiary(beneficiary_id: str, body: VerifyIn, p: Principal = Depends(current_principal),
                       db: Session = Depends(get_db)) -> dict:
    b = ben_svc.verify_beneficiary(db, p, beneficiary_id, body.approve, body.note)
    db.commit()
    return {"id": str(b.id), "verified": b.verified, "is_active": b.is_active}
