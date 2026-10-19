"""Explicit serializers (no ORM leakage; banking data masked)."""

from __future__ import annotations

from typing import Any

from finguard.authz.principal import Principal
from finguard.models import (ApprovalRequest, AuditLog, Beneficiary, Contract, ContractAmendment, ContractDocument,
                             ContractVersion, Invoice, Notification, OcrJob, PaymentAttempt, ScheduledPayment, User)


def _s(v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v) if not isinstance(v, (bool, int, float, str, list, dict)) else v


def user_view(u: User, perms: frozenset[str] | None = None) -> dict:
    d = {"id": str(u.id), "email": u.email, "full_name": u.full_name, "company_name": u.company_name,
         "roles": u.role_names, "kind": u.kind, "is_active": u.is_active, "org_id": str(u.org_id),
         "last_login_at": _s(u.last_login_at), "created_at": _s(u.created_at)}
    if perms is not None:
        d["permissions"] = sorted(perms)
    return d


def version_view(v: ContractVersion | None) -> dict | None:
    if v is None:
        return None
    keys = ("version_no", "status", "contractor_legal_name", "counterparty_name", "total_value", "payment_amount",
            "currency", "frequency", "interval_count", "custom_unit", "due_day_of_month", "due_weekday", "start_date",
            "end_date", "first_payment_date", "payment_timing", "grace_period_days", "proration_rule",
            "business_day_rule", "timezone", "invoice_required", "auto_execute_permitted", "tax_withholding_note",
            "termination_terms", "notice_period_days", "effective_from", "terms_hash", "approved_at")
    d = {k: _s(getattr(v, k)) for k in keys}
    d.update(id=str(v.id), approved_by=_s(v.approved_by), amendment_id=_s(v.amendment_id), created_at=_s(v.created_at))
    return d


def beneficiary_view(b: Beneficiary | None) -> dict | None:
    if b is None:
        return None
    return {"id": str(b.id), "account_holder_name": b.account_holder_name, "ifsc": b.ifsc,
            "account_masked": f"XXXX{b.account_last4}", "provider": b.provider, "verified": b.verified,
            "verified_at": _s(b.verified_at), "is_active": b.is_active}


def contract_view(c: Contract, actor: Principal | None = None) -> dict:
    return {"id": str(c.id), "title": c.title, "external_ref": c.external_ref, "status": c.status,
            "status_reason": c.status_reason, "contractor": {"id": str(c.contractor_id), "name": c.contractor.full_name,
                                                             "company": c.contractor.company_name},
            "current_version": version_view(c.current_version), "beneficiary": beneficiary_view(c.beneficiary),
            "primary_document_id": _s(c.primary_document_id), "latest_extraction_id": _s(c.latest_extraction_id),
            "submitted_at": _s(c.submitted_at), "activated_at": _s(c.activated_at), "created_at": _s(c.created_at),
            "updated_at": _s(c.updated_at),
            "is_owner": bool(actor and actor.user_id == c.contractor_id)}


def document_view(d: ContractDocument) -> dict:
    return {"id": str(d.id), "kind": d.kind, "filename": d.original_filename, "mime_type": d.mime_type,
            "size_bytes": d.size_bytes, "sha256": d.sha256, "page_count": d.page_count, "created_at": _s(d.created_at),
            "amendment_id": _s(d.amendment_id)}


def ocr_job_view(j: OcrJob) -> dict:
    return {"id": str(j.id), "status": j.status, "method": j.method, "pages_total": j.pages_total,
            "pages_ocr": j.pages_ocr, "mean_ocr_confidence": _s(j.mean_ocr_confidence), "attempts": j.attempts,
            "error": j.error, "started_at": _s(j.started_at), "finished_at": _s(j.finished_at),
            "document_id": str(j.document_id)}


def attempt_view(a: PaymentAttempt) -> dict:
    return {"id": str(a.id), "attempt_no": a.attempt_no, "idempotency_key": a.idempotency_key, "provider": a.provider,
            "provider_mode": a.provider_mode, "provider_payout_id": a.provider_payout_id, "amount": _s(a.amount),
            "currency": a.currency, "status": a.status, "provider_status": a.provider_status,
            "failure_code": a.failure_code, "failure_reason": a.failure_reason,
            "governance_decision_id": str(a.governance_decision_id), "created_at": _s(a.created_at),
            "updated_at": _s(a.updated_at)}


def payment_view(sp: ScheduledPayment, contract: Contract | None = None) -> dict:
    d = {"id": str(sp.id), "contract_id": str(sp.contract_id), "contract_version_id": str(sp.contract_version_id),
         "schedule_id": str(sp.schedule_id), "sequence_no": sp.sequence_no, "period_start": _s(sp.period_start),
         "period_end": _s(sp.period_end), "nominal_date": _s(sp.nominal_date), "due_date": _s(sp.due_date),
         "due_at": _s(sp.due_at), "amount": _s(sp.amount), "currency": sp.currency, "is_prorated": sp.is_prorated,
         "status": sp.status, "hold_reason": sp.hold_reason, "idempotency_key": sp.idempotency_key,
         "last_decision_id": _s(sp.last_decision_id), "approval_request_id": _s(sp.approval_request_id),
         "paid_at": _s(sp.paid_at), "attempts": [attempt_view(a) for a in sp.attempts]}
    if contract is not None:
        d["contract_title"] = contract.title
        d["contractor_name"] = contract.contractor.full_name
    return d


def invoice_view(i: Invoice) -> dict:
    return {"id": str(i.id), "invoice_number": i.invoice_number, "amount": _s(i.amount), "currency": i.currency,
            "status": i.status, "review_note": i.review_note, "scheduled_payment_id": str(i.scheduled_payment_id),
            "document_id": str(i.document_id) if i.document_id else None,
            "created_at": _s(i.created_at)}


def approval_view(a: ApprovalRequest) -> dict:
    return {"id": str(a.id), "kind": a.kind, "status": a.status, "resource_type": a.resource_type,
            "resource_id": str(a.resource_id), "contract_id": _s(a.contract_id), "required_role": a.required_role,
            "required_approvals": a.required_approvals, "amount": _s(a.amount), "currency": a.currency,
            "title": a.title, "summary": a.summary, "governance_decision_id": _s(a.governance_decision_id),
            "decision": a.governance_decision.decision if a.governance_decision else None,
            "risk_score": _s(a.governance_decision.risk_assessment.risk_score) if a.governance_decision else None,
            "requested_by": str(a.requested_by), "resolution_reason": a.resolution_reason,
            "resolved_at": _s(a.resolved_at), "created_at": _s(a.created_at),
            "decisions": [{"decided_by": str(d.decided_by), "role": d.decided_role, "decision": d.decision,
                           "reason": d.reason, "at": _s(d.created_at)} for d in a.decisions]}


def amendment_view(a: ContractAmendment) -> dict:
    return {"id": str(a.id), "contract_id": str(a.contract_id), "status": a.status, "reason": a.reason,
            "requested_effective_date": _s(a.requested_effective_date), "proposed_changes": a.proposed_changes,
            "diff": a.diff, "base_version_id": str(a.base_version_id), "proposed_version_id": _s(a.proposed_version_id),
            "document_id": _s(a.document_id), "extraction_id": _s(a.extraction_id),
            "risk_assessment_id": _s(a.risk_assessment_id), "requested_by": str(a.requested_by),
            "decided_by": _s(a.decided_by), "decided_at": _s(a.decided_at), "decision_reason": a.decision_reason,
            "created_at": _s(a.created_at)}


def audit_view(r: AuditLog) -> dict:
    return {"id": str(r.id), "seq": r.seq, "event_type": r.event_type, "actor_id": _s(r.actor_id),
            "actor_roles": r.actor_roles, "actor_kind": r.actor_kind, "resource_type": r.resource_type,
            "resource_id": r.resource_id, "contract_id": _s(r.contract_id), "outcome": r.outcome,
            "previous_state": r.previous_state, "new_state": r.new_state, "request_id": r.request_id,
            "payload": r.payload, "prev_hash": r.prev_hash, "entry_hash": r.entry_hash, "created_at": _s(r.created_at)}


def notification_view(n: Notification) -> dict:
    return {"id": str(n.id), "kind": n.kind, "title": n.title, "body": n.body, "link": n.link,
            "read": n.read_at is not None, "created_at": _s(n.created_at)}
