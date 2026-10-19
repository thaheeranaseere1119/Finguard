"""Persists risk assessments and governance decisions (with audit) and manages policy versions."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.authz.principal import Principal
from finguard.core.errors import NotFound, ValidationFailed
from finguard.core.money import canonical_hash, to_jsonable
from finguard.governance.assessors import Assessment
from finguard.governance.policy import DEFAULT_POLICY, default_policy
from finguard.models import GovernanceDecision, PolicyVersion, RiskAssessment
from finguard.models.enums import ActionType
from finguard.risk.engine import FACTOR_KEYS, factor_contributions
from finguard.services.audit import AuditService


def active_policy(db: Session, org_id: uuid.UUID) -> PolicyVersion:
    pv = db.execute(select(PolicyVersion).where(PolicyVersion.org_id == org_id, PolicyVersion.status == "ACTIVE")
                    .order_by(PolicyVersion.created_at.desc())).scalars().first()
    if pv is None:
        raise NotFound("no active policy for organization")
    return pv


def validate_policy_config(config: dict[str, Any]) -> None:
    problems = []
    for key in ("weights", "thresholds", "financial_bands", "approval_limits", "tool_risk", "fx_to_base"):
        if key not in config:
            problems.append(f"missing '{key}'")
    if problems:
        raise ValidationFailed("invalid policy", details=problems)
    if set(config["weights"]) != set(FACTOR_KEYS) or any(float(v) < 0 for v in config["weights"].values()):
        problems.append("weights must define non-negative S,F,I,E,T,U")
    for action in ActionType:
        th = config["thresholds"].get(action.value)
        if th is None:
            problems.append(f"thresholds missing for {action.value}")
            continue
        if not (0 <= float(th["act_max"]) <= float(th["escalate_min"]) <= 100):
            problems.append(f"{action.value}: require 0 ≤ act_max ≤ escalate_min ≤ 100")
    if problems:
        raise ValidationFailed("invalid policy", details=problems)


def create_policy_version(db: Session, actor: Principal, version: str, config: dict[str, Any], note: str,
                          activate: bool) -> PolicyVersion:
    merged = default_policy()
    merged.update(config)
    validate_policy_config(merged)
    pv = PolicyVersion(org_id=actor.org_id, version=version, status="DRAFT", config=to_jsonable(merged),
                       config_hash=canonical_hash(merged), change_note=note, created_by=actor.user_id)
    db.add(pv)
    db.flush()
    if activate:
        activate_policy(db, actor, pv)
    AuditService(db).record("policy.created", actor=actor, resource_type="policy_version", resource_id=pv.id,
                            new_state=pv.status, payload={"version": version, "hash": pv.config_hash, "note": note})
    return pv


def activate_policy(db: Session, actor: Principal, pv: PolicyVersion) -> None:
    for other in db.execute(select(PolicyVersion).where(PolicyVersion.org_id == pv.org_id,
                                                        PolicyVersion.status == "ACTIVE")).scalars():
        other.status = "RETIRED"
    pv.status = "ACTIVE"
    AuditService(db).record("policy.activated", actor=actor, resource_type="policy_version", resource_id=pv.id,
                            new_state="ACTIVE", payload={"version": pv.version, "hash": pv.config_hash})


def bootstrap_policy(db: Session, org_id: uuid.UUID, created_by: uuid.UUID) -> PolicyVersion:
    cfg = to_jsonable(DEFAULT_POLICY)
    pv = PolicyVersion(org_id=org_id, version="v1.0.0-example", status="ACTIVE", config=cfg,
                       config_hash=canonical_hash(cfg), change_note="Bootstrap: prototype EXAMPLE thresholds",
                       created_by=created_by)
    db.add(pv)
    db.flush()
    return pv


def record_assessment(db: Session, actor: Principal, assessment: Assessment, policy: PolicyVersion, *,
                      resource_type: str, resource_id: uuid.UUID, contract_id: uuid.UUID | None = None) -> GovernanceDecision:
    v = assessment.vector
    ra = RiskAssessment(
        org_id=actor.org_id, action_type=assessment.action_type.value, resource_type=resource_type,
        resource_id=resource_id, s_sensitivity=v.S.value, f_financial=v.F.value, i_irreversibility=v.I.value,
        e_evidence=v.E.value, t_tool=v.T.value, u_uncertainty=v.U.value, risk_score=assessment.score,
        factor_rationales={f.key: f.to_dict() for f in v.factors()} | {
            "_contributions": factor_contributions(v, policy.config.get("weights"))},
        evidence_refs=to_jsonable(v.evidence_refs), inputs=to_jsonable(assessment.inputs),
        policy_version_id=policy.id, requested_by=actor.user_id, request_id=actor.request_id,
    )
    db.add(ra)
    db.flush()
    o = assessment.outcome
    gd = GovernanceDecision(
        org_id=actor.org_id, risk_assessment_id=ra.id, decision=o.decision.value,
        hard_rule_results=[r.to_dict() for r in assessment.rules], reasons=o.reasons,
        required_approver_role=o.required_approver_role, required_approvals=o.required_approvals,
        required_next_actions=o.required_next_actions, thresholds=o.thresholds,
    )
    db.add(gd)
    db.flush()
    AuditService(db).record(
        "governance.decision", actor=actor, resource_type=resource_type, resource_id=resource_id,
        contract_id=contract_id, new_state=o.decision.value,
        payload={"action_type": assessment.action_type.value, "risk_score": str(assessment.score),
                 "factors": {k: str(val) for k, val in v.values().items()}, "decision_id": str(gd.id),
                 "policy_version": policy.version, "failed_rules": [r.rule_id for r in assessment.rules if not r.passed],
                 "evidence_refs": to_jsonable(v.evidence_refs)[:20]},
    )
    return gd


def decision_view(gd: GovernanceDecision, policy_version: str | None = None) -> dict[str, Any]:
    """Structured decision response (no internal secrets, no model chain-of-thought)."""
    ra = gd.risk_assessment
    return {
        "decision_id": str(gd.id),
        "decision": gd.decision,
        "risk_score": str(ra.risk_score),
        "risk_factors": {k: ra.factor_rationales.get(k) for k in FACTOR_KEYS},
        "contributions": ra.factor_rationales.get("_contributions", {}),
        "policy_id": str(ra.policy_version_id),
        "policy_version": policy_version,
        "hard_rule_results": gd.hard_rule_results,
        "evidence_refs": ra.evidence_refs,
        "reasons": gd.reasons,
        "required_approver_role": gd.required_approver_role,
        "required_approvals": gd.required_approvals,
        "required_next_actions": gd.required_next_actions,
        "thresholds": gd.thresholds,
        "action_type": ra.action_type,
        "resource_type": ra.resource_type,
        "resource_id": str(ra.resource_id),
        "created_at": gd.created_at.isoformat() if gd.created_at else None,
    }
