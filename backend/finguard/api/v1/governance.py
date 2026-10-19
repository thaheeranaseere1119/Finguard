"""Approval queues, governance decisions, audit explorer, agent, notifications, analytics."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from finguard.agent.service import run_agent
from finguard.api.deps import current_principal
from finguard.api.schemas import AgentIn, ApprovalDecisionIn
from finguard.api.serializers import approval_view, audit_view, notification_view
from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.db import get_db
from finguard.core.errors import NotFound
from finguard.governance.service import decision_view
from finguard.models import (AgentRun, ApprovalRequest, AuditLog, Contract, ExtractedField, ExtractionResult,
                             FieldCorrection, GovernanceDecision, Notification, OcrJob, PaymentAttempt, PolicyVersion,
                             RiskAssessment, ScheduledPayment)
from finguard.services import approvals as svc
from finguard.services.audit import AuditService

router = APIRouter(tags=["governance"])


# ------------------------------------------------------------------ approvals
@router.get("/approvals")
def list_approvals(status: str = "OPEN", kind: str | None = None, mine: bool = False, limit: int = Query(100, le=200),
                   offset: int = 0, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    rows, total = svc.list_requests(db, p, status, kind, mine, limit, offset)
    return {"items": [approval_view(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/approvals/{request_id}")
def get_approval(request_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    ar = svc.get_request(db, p, request_id)
    out = approval_view(ar)
    if ar.governance_decision and p.has("risk:read"):
        pv = db.get(PolicyVersion, ar.governance_decision.risk_assessment.policy_version_id)
        out["governance"] = decision_view(ar.governance_decision, pv.version if pv else None)
    return out


@router.post("/approvals/{request_id}/decision")
def decide(request_id: str, body: ApprovalDecisionIn, p: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> dict:
    ar = svc.decide(db, p, request_id, body.decision, body.reason)
    db.commit()
    db.refresh(ar)
    return approval_view(ar)


# ------------------------------------------------------------------ decisions & risk
@router.get("/decisions")
def list_decisions(decision: str | None = None, action_type: str | None = None, resource_id: str | None = None,
                   limit: int = Query(100, le=500), offset: int = 0, p: Principal = Depends(current_principal),
                   db: Session = Depends(get_db)) -> dict:
    abac.require(p, "risk:read")
    q = select(GovernanceDecision).join(RiskAssessment).where(GovernanceDecision.org_id == p.org_id)
    if decision:
        q = q.where(GovernanceDecision.decision.in_(decision.split(",")))
    if action_type:
        q = q.where(RiskAssessment.action_type == action_type)
    if resource_id:
        q = q.where(RiskAssessment.resource_id == resource_id)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(GovernanceDecision.created_at.desc()).limit(limit).offset(offset)).scalars().unique().all()
    versions = {pv.id: pv.version for pv in db.execute(select(PolicyVersion).where(PolicyVersion.org_id == p.org_id)).scalars()}
    return {"items": [decision_view(g, versions.get(g.risk_assessment.policy_version_id)) for g in rows], "total": total,
            "limit": limit, "offset": offset}


@router.get("/decisions/{decision_id}")
def get_decision(decision_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "risk:read")
    g = abac.load_scoped(db, GovernanceDecision, decision_id, p)
    pv = db.get(PolicyVersion, g.risk_assessment.policy_version_id)
    out = decision_view(g, pv.version if pv else None)
    out["inputs"] = g.risk_assessment.inputs
    return out


# ------------------------------------------------------------------ audit
def _audit_query(p: Principal, event_type, resource_id, contract_id, outcome, since, until):
    q = select(AuditLog).where(AuditLog.org_id == p.org_id)
    if event_type:
        q = q.where(AuditLog.event_type.like(f"{event_type}%"))
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)
    if contract_id:
        q = q.where(AuditLog.contract_id == contract_id)
    if outcome:
        q = q.where(AuditLog.outcome == outcome)
    if since:
        q = q.where(AuditLog.created_at >= since)
    if until:
        q = q.where(AuditLog.created_at <= until)
    return q


@router.get("/audit")
def audit(event_type: str | None = None, resource_id: str | None = None, contract_id: str | None = None,
          outcome: str | None = None, since: datetime | None = None, until: datetime | None = None,
          limit: int = Query(100, le=500), offset: int = 0, p: Principal = Depends(current_principal),
          db: Session = Depends(get_db)) -> dict:
    abac.require(p, "audit:read")
    q = _audit_query(p, event_type, resource_id, contract_id, outcome, since, until)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(AuditLog.seq.desc()).limit(limit).offset(offset)).scalars().all()
    return {"items": [audit_view(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/audit/verify")
def audit_verify(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "audit:read")
    return AuditService(db).verify_chain()


@router.get("/audit/export")
def audit_export(event_type: str | None = None, contract_id: str | None = None, since: datetime | None = None,
                 until: datetime | None = None, p: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> Response:
    abac.require(p, "audit:export")
    rows = db.execute(_audit_query(p, event_type, None, contract_id, None, since, until)
                      .order_by(AuditLog.seq).limit(50000)).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seq", "created_at", "event_type", "actor_id", "actor_roles", "resource_type", "resource_id",
                "contract_id", "outcome", "previous_state", "new_state", "request_id", "entry_hash", "payload"])
    import json

    for r in rows:
        w.writerow([r.seq, r.created_at.isoformat(), r.event_type, r.actor_id, "|".join(r.actor_roles), r.resource_type,
                    r.resource_id, r.contract_id, r.outcome, r.previous_state, r.new_state, r.request_id, r.entry_hash,
                    json.dumps(r.payload, sort_keys=True)])
    AuditService(db).record("audit.exported", actor=p, payload={"rows": len(rows), "filters": {
        "event_type": event_type, "contract_id": contract_id}})
    db.commit()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="finguard-audit.csv"'})


# ------------------------------------------------------------------ agent
@router.post("/agent/chat")
def agent_chat(body: AgentIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "agent:use")
    run = run_agent(db, p, body.message)
    db.commit()
    return _run_view(run)


def _run_view(run: AgentRun) -> dict:
    return {"id": str(run.id), "prompt": run.prompt, "answer": run.answer, "status": run.status, "provider": run.provider,
            "model": run.model_name, "evidence": run.evidence_refs, "limitations": run.limitations,
            "structured": run.structured,
            "latency_ms": run.latency_ms, "created_at": run.created_at.isoformat(),
            "tool_calls": [{"id": str(t.id), "tool": t.tool_name, "arguments": t.arguments, "status": t.status,
                            "error": t.error, "latency_ms": t.latency_ms} for t in run.tool_calls]}


@router.get("/agent/runs")
def agent_runs(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[dict]:
    abac.require(p, "agent:use")
    rows = db.execute(select(AgentRun).where(AgentRun.org_id == p.org_id, AgentRun.user_id == p.user_id)
                      .order_by(AgentRun.created_at.desc()).limit(30)).scalars().all()
    return [_run_view(r) for r in rows]


# ------------------------------------------------------------------ notifications
@router.get("/notifications")
def notifications(unread: bool = False, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    q = select(Notification).where(Notification.user_id == p.user_id)
    if unread:
        q = q.where(Notification.read_at.is_(None))
    rows = db.execute(q.order_by(Notification.created_at.desc()).limit(100)).scalars().all()
    n_unread = db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == p.user_id, Notification.read_at.is_(None))).scalar_one()
    return {"items": [notification_view(n) for n in rows], "unread": n_unread}


@router.post("/notifications/read")
def mark_read(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)
    for n in db.execute(select(Notification).where(Notification.user_id == p.user_id, Notification.read_at.is_(None))).scalars():
        n.read_at = now
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ analytics
@router.get("/analytics/overview")
def overview(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require_any(p, "reports:read", "risk:read")
    org = p.org_id
    contracts = dict(db.execute(select(Contract.status, func.count()).where(Contract.org_id == org)
                                .group_by(Contract.status)).all())
    payments = dict(db.execute(select(ScheduledPayment.status, func.count()).where(ScheduledPayment.org_id == org)
                               .group_by(ScheduledPayment.status)).all())
    today = datetime.now(timezone.utc).date()
    due_today = db.execute(select(func.count(), func.coalesce(func.sum(ScheduledPayment.amount), 0)).where(
        ScheduledPayment.org_id == org, ScheduledPayment.due_date == today,
        ScheduledPayment.status.not_in(["SUPERSEDED", "CANCELLED"]))).one()
    upcoming = db.execute(select(func.count(), func.coalesce(func.sum(ScheduledPayment.amount), 0)).where(
        ScheduledPayment.org_id == org, ScheduledPayment.due_date > today,
        ScheduledPayment.due_date <= today + timedelta(days=30), ScheduledPayment.status == "SCHEDULED")).one()
    paid = db.execute(select(func.coalesce(func.sum(PaymentAttempt.amount), 0)).where(
        PaymentAttempt.org_id == org, PaymentAttempt.status == "PROCESSED")).scalar_one()
    approvals = dict(db.execute(select(ApprovalRequest.kind, func.count()).where(
        ApprovalRequest.org_id == org, ApprovalRequest.status == "OPEN").group_by(ApprovalRequest.kind)).all())
    return {"contracts_by_status": contracts, "payments_by_status": payments,
            "due_today": {"count": due_today[0], "amount": str(due_today[1])},
            "upcoming_30d": {"count": upcoming[0], "amount": str(upcoming[1])}, "paid_total": str(paid),
            "open_approvals": approvals}


@router.get("/analytics/risk")
def risk_analytics(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require(p, "risk:read")
    org = p.org_id
    by_decision = db.execute(select(RiskAssessment.action_type, GovernanceDecision.decision, func.count(),
                                    func.avg(RiskAssessment.risk_score))
                             .join(GovernanceDecision, GovernanceDecision.risk_assessment_id == RiskAssessment.id)
                             .where(RiskAssessment.org_id == org)
                             .group_by(RiskAssessment.action_type, GovernanceDecision.decision)).all()
    factors = db.execute(select(RiskAssessment.action_type, func.avg(RiskAssessment.s_sensitivity),
                                func.avg(RiskAssessment.f_financial), func.avg(RiskAssessment.i_irreversibility),
                                func.avg(RiskAssessment.e_evidence), func.avg(RiskAssessment.t_tool),
                                func.avg(RiskAssessment.u_uncertainty), func.count())
                         .where(RiskAssessment.org_id == org).group_by(RiskAssessment.action_type)).all()
    buckets = db.execute(select((func.floor(RiskAssessment.risk_score / 10) * 10).label("b"), GovernanceDecision.decision,
                                func.count())
                         .join(GovernanceDecision, GovernanceDecision.risk_assessment_id == RiskAssessment.id)
                         .where(RiskAssessment.org_id == org).group_by("b", GovernanceDecision.decision)).all()
    rules: dict[str, int] = {}
    for (hr,) in db.execute(select(GovernanceDecision.hard_rule_results).where(GovernanceDecision.org_id == org)
                            .order_by(GovernanceDecision.created_at.desc()).limit(2000)).all():
        for r in hr:
            if not r["passed"]:
                rules[r["rule_id"]] = rules.get(r["rule_id"], 0) + 1
    evidence_cov = db.execute(select(func.count(), func.sum(case((func.jsonb_array_length(RiskAssessment.evidence_refs) > 0, 1), else_=0)))
                              .where(RiskAssessment.org_id == org)).one()
    recent = db.execute(select(RiskAssessment.created_at, RiskAssessment.risk_score, GovernanceDecision.decision,
                               RiskAssessment.action_type)
                        .join(GovernanceDecision, GovernanceDecision.risk_assessment_id == RiskAssessment.id)
                        .where(RiskAssessment.org_id == org).order_by(RiskAssessment.created_at.desc()).limit(300)).all()
    return {
        "by_action_decision": [{"action_type": a, "decision": d, "count": n, "avg_score": float(s or 0)} for a, d, n, s in by_decision],
        "avg_factors": [{"action_type": a, "S": float(s), "F": float(f), "I": float(i), "E": float(e), "T": float(t),
                         "U": float(u), "count": n} for a, s, f, i, e, t, u, n in factors],
        "score_histogram": [{"bucket": int(b), "decision": d, "count": n} for b, d, n in buckets],
        "hard_rule_violations": sorted(({"rule_id": k, "count": v} for k, v in rules.items()), key=lambda x: -x["count"]),
        "evidence_coverage": {"assessments": evidence_cov[0], "with_evidence": int(evidence_cov[1] or 0)},
        "recent": [{"at": c.isoformat(), "score": float(s), "decision": d, "action_type": a} for c, s, d, a in recent],
    }


@router.get("/analytics/ocr")
def ocr_analytics(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require_any(p, "reports:read", "risk:read")
    org = p.org_id
    jobs = db.execute(select(OcrJob.status, OcrJob.method, func.count(), func.avg(OcrJob.mean_ocr_confidence))
                      .join(ExtractionResult, ExtractionResult.ocr_job_id == OcrJob.id, isouter=True)
                      .where((ExtractionResult.org_id == org) | (ExtractionResult.org_id.is_(None)))
                      .group_by(OcrJob.status, OcrJob.method)).all()
    fields = db.execute(select(ExtractedField.field_name, ExtractedField.status, func.count())
                        .join(ExtractionResult, ExtractionResult.id == ExtractedField.extraction_id)
                        .where(ExtractionResult.org_id == org, ExtractedField.is_critical.is_(True))
                        .group_by(ExtractedField.field_name, ExtractedField.status)).all()
    n_extr = db.execute(select(func.count()).select_from(ExtractionResult).where(ExtractionResult.org_id == org)).scalar_one()
    corrections = db.execute(select(FieldCorrection.field_name, func.count()).join(
        ExtractionResult, ExtractionResult.id == FieldCorrection.extraction_id).where(ExtractionResult.org_id == org)
        .group_by(FieldCorrection.field_name)).all()
    return {"jobs": [{"status": s, "method": m, "count": n, "avg_ocr_confidence": float(c) if c is not None else None}
                     for s, m, n, c in jobs],
            "critical_field_status": [{"field": f, "status": s, "count": n} for f, s, n in fields],
            "extractions": n_extr, "corrections": [{"field": f, "count": n} for f, n in corrections]}


@router.get("/analytics/workload")
def workload(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    abac.require_any(p, "reports:read", "risk:read")
    rows = db.execute(select(ApprovalRequest.kind, ApprovalRequest.status, func.count(),
                             func.avg(func.extract("epoch", ApprovalRequest.resolved_at - ApprovalRequest.created_at)))
                      .where(ApprovalRequest.org_id == p.org_id).group_by(ApprovalRequest.kind, ApprovalRequest.status)).all()
    return {"approvals": [{"kind": k, "status": s, "count": n, "avg_resolution_seconds": float(t) if t else None}
                          for k, s, n, t in rows]}
