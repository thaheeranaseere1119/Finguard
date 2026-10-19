"""Allowlisted agent tools.

Each tool runs with the DELEGATING USER's principal for data access (so tenant isolation, ownership and role rules
apply exactly as for that user) and is recorded under the AI agent's machine identity. Tools never accept an amount
for payment: amounts always come from the approved contract version and schedule. No tool can call a URL, change
permissions, approve anything, alter contract terms, or execute a payout.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.errors import AppError, NotFound, ValidationFailed
from finguard.extraction.injection import scan_text
from finguard.extraction.pipeline import effective_fields
from finguard.governance.service import decision_view
from finguard.models import (Contract, ContractAmendment, ContractVersion, ExtractionResult, GovernanceDecision, PaymentAttempt, PolicyVersion, RiskAssessment,
                             ScheduledPayment)
from finguard.models.enums import AttemptStatus, RoleName, ScheduledPaymentStatus as SPS
from finguard.services.audit import AuditService
from finguard.services.notifications import notify_roles


@dataclass
class ToolResult:
    data: dict[str, Any]
    evidence: list[dict[str, Any]] = field(default_factory=list)
    sensitivity: int = 2
    coverage: Decimal = Decimal(1)
    conflicting: bool = False
    injection: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    policy_key: str
    description: str
    params: dict[str, str]
    user_permission: tuple[str, ...]
    writes: bool
    handler: Callable[[Session, Principal, Principal, dict], ToolResult]


def _uuid(v: Any, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError) as exc:
        raise ValidationFailed(f"{name} must be a UUID") from exc


def _readable_contracts(db: Session, user: Principal):
    q = select(Contract).where(Contract.org_id == user.org_id)
    if not user.has("contracts:read_org"):
        q = q.where(Contract.contractor_id == user.user_id)
    return q


def _contract(db: Session, user: Principal, args: dict) -> Contract:
    from finguard.services.contracts import get_contract

    if args.get("contract_id"):
        return get_contract(db, user, _uuid(args["contract_id"], "contract_id"))
    c = db.execute(_readable_contracts(db, user).order_by(Contract.updated_at.desc())).scalars().first()
    if c is None:
        raise NotFound("no accessible contract")
    return c


def t_contract_terms(db, user, agent, args) -> ToolResult:
    c = _contract(db, user, args)
    v = db.get(ContractVersion, c.current_version_id) if c.current_version_id else None
    fields = {}
    ev = []
    if c.latest_extraction_id:
        eff = effective_fields(db, db.get(ExtractionResult, c.latest_extraction_id))
        for k in ("payment_amount", "currency", "frequency", "due_day_of_month", "start_date", "end_date", "total_value",
                  "invoice_required", "termination_terms", "notice_period_days", "payment_timing"):
            f = eff.get(k)
            if f and f["value"] is not None:
                fields[k] = f["value"]
                ev.append({"id": f"field:{c.latest_extraction_id}:{k}", "type": "extracted_field", "field": k,
                           "page": f["source_page"], "text": (f["source_text"] or "")[:200], "corrected": f["corrected"]})
    terms = None
    if v:
        terms = {"version_no": v.version_no, "status": v.status, "payment_amount": str(v.payment_amount),
                 "currency": v.currency, "frequency": v.frequency, "interval_count": v.interval_count,
                 "due_day_of_month": v.due_day_of_month, "start_date": str(v.start_date), "end_date": str(v.end_date),
                 "total_value": str(v.total_value) if v.total_value else None, "invoice_required": v.invoice_required,
                 "auto_execute_permitted": v.auto_execute_permitted, "effective_from": str(v.effective_from) if v.effective_from else None}
        ev.insert(0, {"id": f"version:{v.id}", "type": "contract_version", "version_no": v.version_no,
                      "terms_hash": v.terms_hash})
    return ToolResult({"contract_id": str(c.id), "title": c.title, "status": c.status, "approved_terms": terms,
                       "extracted_fields": fields}, ev, sensitivity=3,
                      coverage=Decimal(1) if terms else Decimal("0.6"))


def t_search_evidence(db, user, agent, args) -> ToolResult:
    query = str(args.get("query") or "").strip()[:200]
    if len(query) < 2:
        raise ValidationFailed("query required")
    ids = [c.id for c in db.execute(_readable_contracts(db, user)).scalars().unique()]
    if args.get("contract_id"):
        ids = [i for i in ids if str(i) == str(args["contract_id"])]
    if not ids:
        return ToolResult({"matches": []}, [], coverage=Decimal(0))
    rows = db.execute(text("""
        SELECT er.id, c.id AS contract_id, c.title, (p->>'page')::int AS page, p->>'text' AS body,
               ts_rank(to_tsvector('english', p->>'text'), websearch_to_tsquery('english', :q)) AS rank
        FROM extraction_results er
        JOIN contracts c ON c.latest_extraction_id = er.id
        CROSS JOIN LATERAL jsonb_array_elements(er.pages) p
        WHERE c.id = ANY(:ids) AND to_tsvector('english', p->>'text') @@ websearch_to_tsquery('english', :q)
        ORDER BY rank DESC LIMIT 5"""), {"q": query, "ids": ids}).all()
    matches, ev, injected = [], [], False
    terms = [t.lower() for t in query.split() if len(t) > 2]
    for r in rows:
        body = r.body or ""
        idx = min((body.lower().find(t) for t in terms if body.lower().find(t) >= 0), default=0)
        snippet = " ".join(body[max(0, idx - 160): idx + 260].split())
        rep = scan_text(snippet)
        injected |= rep.detected
        eid = f"page:{r.id}:{r.page}"
        matches.append({"evidence_id": eid, "contract_id": str(r.contract_id), "contract": r.title, "page": r.page,
                        "snippet": snippet, "untrusted_instruction_like_content": rep.detected})
        ev.append({"id": eid, "type": "document_page", "contract_id": str(r.contract_id), "page": r.page,
                   "text": snippet[:200]})
    return ToolResult({"matches": matches}, ev, sensitivity=3,
                      coverage=Decimal(1) if matches else Decimal(0), injection=injected)


def _sp_view(sp: ScheduledPayment) -> dict:
    return {"id": str(sp.id), "contract_id": str(sp.contract_id), "sequence_no": sp.sequence_no,
            "period": f"{sp.period_start}..{sp.period_end}", "due_date": str(sp.due_date), "amount": str(sp.amount),
            "currency": sp.currency, "status": sp.status, "hold_reason": sp.hold_reason}


def t_list_payments(db, user, agent, args) -> ToolResult:
    from finguard.services.payments import list_payments

    rows, total = list_payments(db, user, status=args.get("status"), contract_id=args.get("contract_id"), limit=25)
    return ToolResult({"total": total, "payments": [_sp_view(s) for s in rows]},
                      [{"id": f"payment:{s.id}", "type": "scheduled_payment", "status": s.status} for s in rows[:10]])


def t_upcoming(db, user, agent, args) -> ToolResult:
    from finguard.services.payments import list_payments

    days = max(1, min(int(args.get("days") or 30), 365))
    rows, _ = list_payments(db, user, status="SCHEDULED,AWAITING_APPROVAL,HELD,ESCALATED,APPROVED_FOR_EXECUTION",
                            due_after=date.today(), due_before=date.today() + timedelta(days=days), limit=25)
    return ToolResult({"window_days": days, "payments": [_sp_view(s) for s in rows]},
                      [{"id": f"payment:{s.id}", "type": "scheduled_payment", "due_date": str(s.due_date)} for s in rows[:10]])


def t_totals(db, user, agent, args) -> ToolResult:
    c = _contract(db, user, args)
    live = db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id,
                                                     ScheduledPayment.status.not_in([SPS.SUPERSEDED, SPS.CANCELLED]))).scalars().all()
    paid = db.execute(select(func.coalesce(func.sum(PaymentAttempt.amount), 0)).join(
        ScheduledPayment, ScheduledPayment.id == PaymentAttempt.scheduled_payment_id).where(
        ScheduledPayment.contract_id == c.id, PaymentAttempt.status == AttemptStatus.PROCESSED)).scalar_one()
    scheduled = sum((Decimal(s.amount) for s in live), Decimal(0))
    nxt = min((s for s in live if s.status not in (SPS.PAID, SPS.BLOCKED)), key=lambda s: s.due_date, default=None)
    v = db.get(ContractVersion, c.current_version_id) if c.current_version_id else None
    data = {"contract_id": str(c.id), "currency": v.currency if v else None, "scheduled_total": str(scheduled),
            "paid_total": str(Decimal(paid).quantize(Decimal("0.01"))), "remaining": str((scheduled - Decimal(paid)).quantize(Decimal("0.01"))),
            "occurrences": len(live), "paid_count": sum(1 for s in live if s.status == SPS.PAID),
            "total_contract_value": str(v.total_value) if v and v.total_value else None,
            "next_due": _sp_view(nxt) if nxt else None, "method": "deterministic Decimal sum over database records"}
    return ToolResult(data, [{"id": f"contract:{c.id}:totals", "type": "calculation", "inputs": len(live)}])


def t_compare_amendment(db, user, agent, args) -> ToolResult:
    if args.get("amendment_id"):
        am = abac.load_scoped(db, ContractAmendment, _uuid(args["amendment_id"], "amendment_id"), user)
        from finguard.services.contracts import get_contract

        get_contract(db, user, am.contract_id)
    else:
        c = _contract(db, user, args)
        am = db.execute(select(ContractAmendment).where(ContractAmendment.contract_id == c.id)
                        .order_by(ContractAmendment.created_at.desc())).scalars().first()
        if am is None:
            return ToolResult({"amendment": None, "note": "no amendment found for this contract"}, [], coverage=Decimal(0))
    ra = db.get(RiskAssessment, am.risk_assessment_id) if am.risk_assessment_id else None
    return ToolResult({"amendment_id": str(am.id), "status": am.status, "diff": am.diff, "reason": am.reason,
                       "effective_date": str(am.requested_effective_date),
                       "risk_score": str(ra.risk_score) if ra else None},
                      [{"id": f"amendment:{am.id}", "type": "amendment", "diff": am.diff}], sensitivity=3)


def t_explain(db, user, agent, args) -> ToolResult:
    abac.require_any(user, "risk:read", "payments:read_own", "contracts:read_own")
    q = select(GovernanceDecision).join(RiskAssessment, RiskAssessment.id == GovernanceDecision.risk_assessment_id) \
        .where(GovernanceDecision.org_id == user.org_id)
    if args.get("resource_id"):
        q = q.where(RiskAssessment.resource_id == _uuid(args["resource_id"], "resource_id"))
    elif args.get("decision_id"):
        q = q.where(GovernanceDecision.id == _uuid(args["decision_id"], "decision_id"))
    else:
        wanted = [d for d in str(args.get("decision") or "").upper().split(",") if d in
                  {"ACT", "APPROVE", "ESCALATE", "VERIFY", "BLOCK"}] or ["BLOCK", "ESCALATE", "VERIFY", "APPROVE"]
        q = q.where(GovernanceDecision.decision.in_(wanted))
    if args.get("resource_type") in {"scheduled_payment", "contract", "contract_amendment"}:
        q = q.where(RiskAssessment.resource_type == args["resource_type"])
    candidates = db.execute(q.order_by(GovernanceDecision.created_at.desc()).limit(20)).scalars().all()
    gd = None
    for cand in candidates:  # enforce resource readability for the delegating user
        ra = cand.risk_assessment
        try:
            if ra.resource_type == "scheduled_payment":
                from finguard.services.payments import get_payment

                get_payment(db, user, ra.resource_id)
            elif ra.resource_type == "contract":
                from finguard.services.contracts import get_contract

                get_contract(db, user, ra.resource_id)
            elif not user.has("risk:read"):
                continue
            gd = cand
            break
        except AppError:
            continue
    if gd is None:
        return ToolResult({"decision": None, "note": "no accessible governance decision found"}, [], coverage=Decimal(0))
    pv = db.get(PolicyVersion, gd.risk_assessment.policy_version_id)
    view = decision_view(gd, pv.version if pv else None)
    if not user.has("risk:read"):  # contractors see outcome and reasons, not internal factor scoring
        view = {k: view[k] for k in ("decision", "reasons", "required_next_actions", "resource_type", "resource_id", "created_at")}
    return ToolResult(view, [{"id": f"decision:{gd.id}", "type": "governance_decision", "decision": gd.decision}])


def t_prepare_payment(db, user, agent, args) -> ToolResult:
    """Governance preview for a scheduled occurrence; opens an approval request if one is required. Never pays."""
    from finguard.governance.assessors import assess_payment
    from finguard.governance.service import active_policy, record_assessment
    from finguard.services.payments import _open_request, build_snapshot, get_payment

    abac.require(user, "payments:prepare")
    sp = get_payment(db, user, _uuid(args.get("scheduled_payment_id"), "scheduled_payment_id"))
    pv = active_policy(db, sp.org_id)
    snap = build_snapshot(db, agent, sp, pv.config)
    snap.actor_authorized = True  # preview of the scheduler's evaluation; execution authority is not exercised here
    a = assess_payment(snap, pv.config)
    gd = record_assessment(db, agent, a, pv, resource_type="scheduled_payment", resource_id=sp.id, contract_id=sp.contract_id)
    opened = None
    if a.outcome.decision.value == "APPROVE" and sp.status in (SPS.SCHEDULED, SPS.HELD, SPS.FAILED) and snap.is_due:
        ar = _open_request(db, agent, sp, "PAYMENT_APPROVAL", a.outcome.required_approver_role,
                           a.outcome.required_approvals, gd.id, f"Approve payment #{sp.sequence_no} (prepared by agent)",
                           "; ".join(a.outcome.reasons))
        sp.status = SPS.AWAITING_APPROVAL
        opened = str(ar.id)
    note = {"ACT": "eligible; the scheduler executes it at the due time through the governed payout path",
            "APPROVE": "requires human approval" + (" — approval request opened" if opened else ""),
            "ESCALATE": "requires risk/compliance review", "VERIFY": "evidence missing — see required actions",
            "BLOCK": "not permitted"}[a.outcome.decision.value]
    return ToolResult(
        {"scheduled_payment_id": str(sp.id), "amount": str(sp.amount), "currency": sp.currency,
         "amount_source": "approved contract version schedule (not user input)", "decision": a.outcome.decision.value,
         "risk_score": str(a.score), "reasons": a.outcome.reasons, "approval_request_id": opened, "note": note},
        [{"id": f"decision:{gd.id}", "type": "governance_decision", "decision": a.outcome.decision.value}])


def t_incident(db, user, agent, args) -> ToolResult:
    title = str(args.get("title") or "Agent-reported exception")[:200]
    details = str(args.get("details") or "")[:2000]
    resource = str(args.get("resource_id") or "")[:64] or None
    row = AuditService(db).record("incident.reported", actor=agent, resource_type="incident", resource_id=resource,
                                  payload={"title": title, "details": details, "reported_for": str(user.user_id)})
    notify_roles(db, user.org_id, [RoleName.RISK_OFFICER], "incident.reported", f"Incident: {title}", details[:500],
                 "/audit")
    return ToolResult({"incident_audit_seq": row.seq, "title": title, "status": "REPORTED"},
                      [{"id": f"audit:{row.seq}", "type": "audit_event"}], sensitivity=2)


def t_company_financials(db, user, agent, args) -> ToolResult:
    """Grounded financial Q&A over the organization's own ledger (XYZ Ltd demo). Numbers are computed, never generated."""
    from finguard.api.v1.financials import answer_view, has_ledger
    from finguard.xyz.metrics import Calc, SQLSource
    from finguard.xyz.qa import answer

    abac.require(user, "financials:read")
    if not has_ledger(db, user.org_id):
        raise NotFound("no financial ledger is loaded for this organization")
    a = answer(Calc(SQLSource(db, user.org_id)), str(args.get("question") or "")[:1000])
    view = answer_view(a)
    ev = [{"id": e["id"], "type": "ledger_aggregate", "period": e["period"], "accounts": e["accounts"],
           "journal_lines": e.get("journal_lines"), "sample_documents": e.get("sample_documents"), "text": e["formula"]}
          for e in a.evidence]
    return ToolResult(view, ev, sensitivity=3, coverage=Decimal(1) if a.status == "ANSWERED" and ev else Decimal(0))


def t_invoice_draft(db, user, agent, args) -> ToolResult:
    """Draft (never submit) the contractor's invoice for a month — amounts from approved schedules only."""
    from finguard.services.invoicing import claimable

    data = claimable(db, user, args.get("month"))
    ev = [{"id": f"payment:{d['scheduled_payment_id']}", "type": "scheduled_payment", "period": f"{d['period_start']}..{d['period_end']}",
           "text": f"{d['amount']} {d['currency']} from approved contract version {d['contract']['version']}"} for d in data["items"]]
    return ToolResult(data, ev, sensitivity=2, coverage=Decimal(1) if data["items"] else Decimal(0))


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("invoice_draft", "read.contract",
         "Draft the contractor's invoice(s) for a month (default last month) from approved schedules; submission is a separate human action",
         {"month": "YYYY-MM (optional, default last month)"}, ("invoices:submit_own",), False, t_invoice_draft),
    Tool("company_financials", "read.financials",
         "Answer any question about the organization: exact figures computed from the ledger (revenue, profit, margins, "
         "cash flow, balance sheet, customers, transactions) and company knowledge retrieved from its records (leadership, "
         "policies, board minutes, events, customer/vendor dossiers, operations logs) with citations",
         {"question": "the user's financial question verbatim"}, ("financials:read",), False, t_company_financials),
    Tool("get_contract_terms", "read.contract", "Approved contract terms and extracted fields with source evidence",
         {"contract_id": "uuid (optional; defaults to most recent accessible contract)"},
         ("contracts:read_own", "contracts:read_org"), False, t_contract_terms),
    Tool("search_contract_evidence", "read.evidence", "Full-text search over the user's accessible contract documents",
         {"query": "text", "contract_id": "uuid (optional)"}, ("contracts:read_own", "contracts:read_org"), False,
         t_search_evidence),
    Tool("list_payments", "read.payments", "Scheduled payments and their statuses",
         {"contract_id": "uuid (optional)", "status": "comma-separated statuses (optional)"},
         ("payments:read_own", "payments:read_org"), False, t_list_payments),
    Tool("upcoming_payments", "read.payments", "Payments due in the next N days", {"days": "int 1-365"},
         ("payments:read_own", "payments:read_org"), False, t_upcoming),
    Tool("calculate_contract_totals", "calc.totals", "Deterministic scheduled/paid/remaining totals for a contract",
         {"contract_id": "uuid (optional)"}, ("payments:read_own", "payments:read_org"), False, t_totals),
    Tool("compare_amendment", "read.contract", "Compare an amendment with the current approved version",
         {"amendment_id": "uuid (optional)", "contract_id": "uuid (optional)"}, ("contracts:read_own", "contracts:read_org"),
         False, t_compare_amendment),
    Tool("explain_decision", "read.contract", "Explain why an operation was executed, held, blocked or escalated",
         {"resource_id": "uuid (optional)", "decision_id": "uuid (optional)",
          "resource_type": "scheduled_payment|contract|contract_amendment (optional)",
          "decision": "comma-separated ACT/APPROVE/ESCALATE/VERIFY/BLOCK (optional)"},
         ("risk:read", "payments:read_own", "contracts:read_own"), False, t_explain),
    Tool("prepare_payment_request", "payment.prepare",
         "Run the governance check for a scheduled payment and open an approval request if required (never pays)",
         {"scheduled_payment_id": "uuid"}, ("payments:prepare",), True, t_prepare_payment),
    Tool("create_incident_report", "incident.create", "Record a structured exception/incident for risk review",
         {"title": "text", "details": "text", "resource_id": "uuid (optional)"}, ("agent:use",), True, t_incident),
]}
