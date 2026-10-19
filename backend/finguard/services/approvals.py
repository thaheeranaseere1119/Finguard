"""Approval queues and escalation management.

* CONTRACT_APPROVAL / AMENDMENT_APPROVAL / PAYMENT_APPROVAL: decided by users holding the required role (or a role
  with a higher approval limit). A mandatory reason is recorded. Separation of duties: requester, contractor and
  prior approvers of the same request cannot approve. Payments above the dual-approval threshold need two distinct
  approvers, at least one of whom has a sufficient limit.
* ESCALATION: resolved by RISK_OFFICER or FINANCE_MANAGER with CLEAR (case proceeds to normal approval, which a
  different person must give), REJECT, or HOLD (stays open with a note).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.errors import Forbidden, InvalidState, ValidationFailed
from finguard.governance.policy import to_base
from finguard.governance.service import active_policy
from finguard.models import ApprovalDecision, ApprovalRequest, Contract, ContractAmendment, ScheduledPayment
from finguard.models.enums import ApprovalKind, ApprovalStatus, ContractStatus, RoleName, ScheduledPaymentStatus
from finguard.services.audit import AuditService
from finguard.services.notifications import notify_roles, notify_user

ROLE_RANK = {RoleName.FINANCE_APPROVER: 1, RoleName.FINANCE_MANAGER: 2}
ESCALATION_ROLES = {RoleName.RISK_OFFICER, RoleName.FINANCE_MANAGER}


def list_requests(db: Session, actor: Principal, status: str = "OPEN", kind: str | None = None,
                  mine_only: bool = False, limit: int = 100, offset: int = 0) -> tuple[list[ApprovalRequest], int]:
    abac.require(actor, "approvals:read")
    q = select(ApprovalRequest).where(ApprovalRequest.org_id == actor.org_id)
    if status:
        q = q.where(ApprovalRequest.status.in_(status.split(",")))
    if kind:
        q = q.where(ApprovalRequest.kind.in_(kind.split(",")))
    if mine_only:
        q = q.where(ApprovalRequest.required_role.in_(_actionable_roles(actor)))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(ApprovalRequest.created_at.desc()).limit(min(limit, 200)).offset(offset)).scalars().unique().all()
    return list(rows), total


def _actionable_roles(actor: Principal) -> list[str]:
    roles = set()
    if actor.has_role(RoleName.FINANCE_APPROVER):
        roles.add(RoleName.FINANCE_APPROVER)
    if actor.has_role(RoleName.FINANCE_MANAGER):
        roles |= {RoleName.FINANCE_APPROVER, RoleName.FINANCE_MANAGER, RoleName.RISK_OFFICER}
    if actor.has_role(RoleName.RISK_OFFICER):
        roles.add(RoleName.RISK_OFFICER)
    if actor.has_role(RoleName.FINANCE_ANALYST):
        roles.add(RoleName.FINANCE_ANALYST)
    return sorted(roles)


def get_request(db: Session, actor: Principal, request_id: Any) -> ApprovalRequest:
    abac.require(actor, "approvals:read")
    return abac.load_scoped(db, ApprovalRequest, request_id, actor)


def _requester_ids(db: Session, ar: ApprovalRequest) -> set:
    ids = {ar.requested_by}
    if ar.contract_id:
        c = db.get(Contract, ar.contract_id)
        ids.add(c.contractor_id)
    if ar.resource_type == "contract_amendment":
        am = db.get(ContractAmendment, ar.resource_id)
        ids.add(am.requested_by)
    # People who cleared an escalation on the same resource cannot also give the financial approval.
    for d in db.execute(select(ApprovalDecision).join(ApprovalRequest, ApprovalRequest.id == ApprovalDecision.approval_request_id)
                        .where(ApprovalRequest.resource_id == ar.resource_id, ApprovalRequest.kind == ApprovalKind.ESCALATION,
                               ApprovalRequest.id != ar.id)).scalars():
        ids.add(d.decided_by)
    return ids


def decide(db: Session, actor: Principal, request_id: Any, decision: str, reason: str) -> ApprovalRequest:
    ar = abac.load_scoped(db, ApprovalRequest, request_id, actor)
    ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.id == ar.id).with_for_update(of=ApprovalRequest)).scalar_one()
    if ar.status != ApprovalStatus.OPEN:
        raise InvalidState(f"request is {ar.status}")
    if not reason or len(reason.strip()) < 5:
        raise ValidationFailed("a decision reason of at least 5 characters is required")
    decision = decision.upper()
    try:
        if ar.kind == ApprovalKind.ESCALATION:
            _decide_escalation(db, actor, ar, decision, reason.strip())
        elif ar.kind == ApprovalKind.VERIFICATION:
            raise InvalidState("verification requests are resolved by providing the requested evidence")
        else:
            _decide_approval(db, actor, ar, decision, reason.strip())
    except Forbidden as exc:
        AuditService(db).denied(actor, f"approval.{ar.kind.lower()}", exc.message, resource_type="approval_request",
                                resource_id=ar.id, contract_id=ar.contract_id)
        raise
    return ar


def _check_role(actor: Principal, ar: ApprovalRequest) -> None:
    need = ROLE_RANK.get(ar.required_role, 99)
    have = max((ROLE_RANK.get(r, 0) for r in actor.roles), default=0)
    if have < need:
        raise Forbidden(f"requires role {ar.required_role}", code="insufficient_role")


def _decide_approval(db: Session, actor: Principal, ar: ApprovalRequest, decision: str, reason: str) -> None:
    if decision not in ("APPROVE", "REJECT"):
        raise ValidationFailed("decision must be APPROVE or REJECT")
    perm = {ApprovalKind.CONTRACT_APPROVAL: "contracts:approve", ApprovalKind.AMENDMENT_APPROVAL: "amendments:approve",
            ApprovalKind.PAYMENT_APPROVAL: "payments:approve"}[ar.kind]
    abac.require(actor, perm)
    abac.ensure_not_self(actor, *_requester_ids(db, ar), what="approve a request you raised, own, or cleared")
    if any(d.decided_by == actor.user_id for d in ar.decisions):
        raise Forbidden("you already recorded a decision on this request", code="separation_of_duties")
    _check_role(actor, ar)
    pv = active_policy(db, ar.org_id)
    if decision == "APPROVE" and ar.kind == ApprovalKind.PAYMENT_APPROVAL and ar.amount is not None:
        amount_base = to_base(Decimal(ar.amount), ar.currency or "INR", pv.config)
        limit = abac.approval_limit_for(actor.roles, pv.config["approval_limits"])
        approvals_so_far = [d for d in ar.decisions if d.decision == "APPROVE"]
        final = len(approvals_so_far) + 1 >= ar.required_approvals
        anyone_within = limit >= amount_base or any(
            abac.approval_limit_for(frozenset({d.decided_role}), pv.config["approval_limits"]) >= amount_base
            for d in approvals_so_far)
        if final and not anyone_within:
            raise Forbidden(f"amount {amount_base} exceeds the approval limits of all approvers", code="approval_limit")
    role_used = max(actor.roles, key=lambda r: ROLE_RANK.get(r, 0))
    db.add(ApprovalDecision(approval_request_id=ar.id, decided_by=actor.user_id, decided_role=role_used,
                            decision=decision, reason=reason))
    db.flush()
    db.refresh(ar)
    AuditService(db).record("approval.decision", actor=actor, resource_type="approval_request", resource_id=ar.id,
                            contract_id=ar.contract_id, new_state=decision,
                            payload={"kind": ar.kind, "reason": reason, "resource_id": str(ar.resource_id),
                                     "approvals": sum(1 for d in ar.decisions if d.decision == "APPROVE"),
                                     "required": ar.required_approvals})
    approvals = sum(1 for d in ar.decisions if d.decision == "APPROVE")
    if decision == "REJECT":
        _close(db, actor, ar, ApprovalStatus.REJECTED, reason)
        _dispatch(db, actor, ar, "REJECT", reason)
    elif approvals >= ar.required_approvals:
        _close(db, actor, ar, ApprovalStatus.APPROVED, reason)
        _dispatch(db, actor, ar, "APPROVE", reason)
    else:
        notify_roles(db, ar.org_id, [ar.required_role, RoleName.FINANCE_MANAGER], "approval.second_needed",
                     f"Second approval needed: {ar.title}", f"First approval by {actor.full_name}", f"/approvals/{ar.id}",
                     exclude=[actor.user_id])


def _dispatch(db: Session, actor: Principal, ar: ApprovalRequest, decision: str, reason: str) -> None:
    cleared = db.execute(select(ApprovalRequest.id).where(
        ApprovalRequest.resource_id == ar.resource_id, ApprovalRequest.kind == ApprovalKind.ESCALATION,
        ApprovalRequest.status == ApprovalStatus.APPROVED)).first() is not None
    if ar.kind == ApprovalKind.CONTRACT_APPROVAL:
        from finguard.services.contracts import decide_contract

        decide_contract(db, actor, ar, decision, reason, escalation_cleared=cleared)
    elif ar.kind == ApprovalKind.AMENDMENT_APPROVAL:
        from finguard.services.amendments import decide_amendment

        decide_amendment(db, actor, ar, decision, reason, escalation_cleared=cleared)
    elif ar.kind == ApprovalKind.PAYMENT_APPROVAL:
        from finguard.services.payments import decide_payment

        decide_payment(db, actor, ar, "APPROVE_FINAL" if decision == "APPROVE" else "REJECT", reason)


def _close(db: Session, actor: Principal, ar: ApprovalRequest, status: str, reason: str) -> None:
    ar.status, ar.resolution_reason, ar.resolved_at = status, reason, datetime.now(timezone.utc)
    AuditService(db).record("approval.resolved", actor=actor, resource_type="approval_request", resource_id=ar.id,
                            contract_id=ar.contract_id, previous_state=ApprovalStatus.OPEN, new_state=status,
                            payload={"kind": ar.kind, "reason": reason})


def _decide_escalation(db: Session, actor: Principal, ar: ApprovalRequest, decision: str, reason: str) -> None:
    abac.require(actor, "escalations:resolve")
    if not (actor.roles & ESCALATION_ROLES):
        raise Forbidden("escalations are resolved by risk/compliance or a finance manager", code="insufficient_role")
    abac.ensure_not_self(actor, *({ar.requested_by} | ({db.get(Contract, ar.contract_id).contractor_id} if ar.contract_id else set())),
                         what="resolve an escalation on your own request")
    if decision not in ("CLEAR", "REJECT", "HOLD"):
        raise ValidationFailed("decision must be CLEAR, REJECT or HOLD")
    db.add(ApprovalDecision(approval_request_id=ar.id, decided_by=actor.user_id,
                            decided_role=RoleName.RISK_OFFICER if actor.has_role(RoleName.RISK_OFFICER) else RoleName.FINANCE_MANAGER,
                            decision=decision, reason=reason))
    AuditService(db).record("escalation.decision", actor=actor, resource_type="approval_request", resource_id=ar.id,
                            contract_id=ar.contract_id, new_state=decision, payload={"reason": reason,
                                                                                     "resource_type": ar.resource_type,
                                                                                     "resource_id": str(ar.resource_id)})
    if decision == "HOLD":
        return
    if decision == "REJECT":
        _close(db, actor, ar, ApprovalStatus.REJECTED, reason)
        _reject_resource(db, actor, ar, reason)
        return
    _close(db, actor, ar, ApprovalStatus.APPROVED, reason)
    _proceed_after_clearance(db, actor, ar, reason)


def _reject_resource(db: Session, actor: Principal, ar: ApprovalRequest, reason: str) -> None:
    if ar.resource_type == "contract":
        from finguard.services.contracts import transition

        c = db.get(Contract, ar.resource_id)
        transition(db, actor, c, ContractStatus.REJECTED, f"escalation rejected: {reason}")
        notify_user(db, c.org_id, c.contractor_id, "contract.rejected", "Contract rejected after compliance review",
                    reason, f"/contracts/{c.id}")
    elif ar.resource_type == "scheduled_payment":
        from finguard.models import Invoice
        from finguard.services.payments import _set_status

        sp = db.get(ScheduledPayment, ar.resource_id)
        inv = db.execute(select(Invoice).where(Invoice.scheduled_payment_id == sp.id, Invoice.status != "REJECTED")
                         .order_by(Invoice.created_at.desc())).scalars().first()
        if inv is not None:
            # a rejected CLAIM: reject the invoice and let the contractor submit a corrected one (payment not killed)
            inv.status, inv.reviewed_by, inv.review_note = "REJECTED", actor.user_id, f"Rejected in risk review: {reason}"
            _set_status(db, actor, sp, ScheduledPaymentStatus.HELD, f"claim rejected in risk review: {reason}; "
                                                                     f"submit a corrected invoice")
            c = db.get(Contract, sp.contract_id)
            notify_user(db, sp.org_id, c.contractor_id, "invoice.reviewed", "Invoice rejected",
                        f"{inv.invoice_number}: {reason}. Please submit a corrected invoice.", f"/payments/{sp.id}")
        else:
            _set_status(db, actor, sp, ScheduledPaymentStatus.BLOCKED, f"escalation rejected: {reason}")
    elif ar.resource_type == "contract_amendment":
        from finguard.services.amendments import reject_amendment

        reject_amendment(db, actor, db.get(ContractAmendment, ar.resource_id), f"escalation rejected: {reason}")


def _proceed_after_clearance(db: Session, actor: Principal, ar: ApprovalRequest, reason: str) -> None:
    """Escalation cleared. Payments are re-assessed with the clearance (bound to the exact amount and payee): a claim
    within the contract amount is then paid, anything above it goes to an approver. Contracts/amendments open the
    normal approval step (a different person must approve)."""
    if ar.resource_type == "scheduled_payment":
        from finguard.services.common import service_principal
        from finguard.services.payments import _set_status, govern_and_route

        sp = db.get(ScheduledPayment, ar.resource_id)
        _set_status(db, actor, sp, ScheduledPaymentStatus.SCHEDULED, f"escalation cleared: {reason}")
        db.flush()
        govern_and_route(db, service_principal(db, ar.org_id, RoleName.SCHEDULER), sp)
        return
    kind = {"contract": ApprovalKind.CONTRACT_APPROVAL, "contract_amendment": ApprovalKind.AMENDMENT_APPROVAL,
            "scheduled_payment": ApprovalKind.PAYMENT_APPROVAL}[ar.resource_type]
    pv = active_policy(db, ar.org_id)
    role, approvals = RoleName.FINANCE_APPROVER, 1
    if ar.amount is not None:
        amount_base = to_base(Decimal(ar.amount), ar.currency or "INR", pv.config)
        threshold_key = "contract_manager_threshold" if kind != ApprovalKind.PAYMENT_APPROVAL else None
        if threshold_key and amount_base > Decimal(str(pv.config[threshold_key])):
            role = RoleName.FINANCE_MANAGER
        if kind == ApprovalKind.PAYMENT_APPROVAL:
            if amount_base > Decimal(str(pv.config["approval_limits"][RoleName.FINANCE_APPROVER])):
                role = RoleName.FINANCE_MANAGER
            if amount_base > Decimal(str(pv.config["dual_approval_threshold"])):
                approvals = 2
    nxt = ApprovalRequest(org_id=ar.org_id, kind=kind, resource_type=ar.resource_type, resource_id=ar.resource_id,
                          contract_id=ar.contract_id, governance_decision_id=ar.governance_decision_id,
                          required_role=role, required_approvals=approvals, amount=ar.amount, currency=ar.currency,
                          title=ar.title.replace("Escalated", "Approve (cleared)"),
                          summary=f"Cleared by {actor.full_name}: {reason}. {ar.summary}"[:2000], requested_by=ar.requested_by)
    db.add(nxt)
    db.flush()
    if ar.resource_type == "scheduled_payment":
        from finguard.services.payments import _set_status

        sp = db.get(ScheduledPayment, ar.resource_id)
        sp.approval_request_id = nxt.id
        _set_status(db, actor, sp, ScheduledPaymentStatus.AWAITING_APPROVAL, f"escalation cleared: {reason}")
    AuditService(db).record("approval.requested", actor=actor, resource_type="approval_request", resource_id=nxt.id,
                            contract_id=ar.contract_id, new_state="OPEN",
                            payload={"kind": kind, "after_escalation": str(ar.id), "required_role": role})
    notify_roles(db, ar.org_id, [role], "approval.requested", nxt.title, nxt.summary, f"/approvals/{nxt.id}",
                 exclude=[actor.user_id])
