"""Risk assessments, governance decisions, approval requests, audit log, agent runs, policies, notifications, evaluations."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finguard.core.db import Base
from finguard.models.common import Money, Score, created_at_col, enum_check, uuid_pk
from finguard.models.enums import ActionType, ApprovalKind, ApprovalStatus, Decision


def _fk(table: str, nullable: bool = False) -> Mapped:
    return mapped_column(PG_UUID(as_uuid=True), ForeignKey(f"{table}.id"), nullable=nullable)


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("org_id", "version", name="uq_policy_org_version"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")  # ACTIVE | RETIRED | DRAFT
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[uuid.UUID] = _fk("users")
    created_at: Mapped[datetime] = created_at_col()


class RiskAssessment(Base):
    """Immutable. Every factor, its rationale and evidence are stored, not just the score."""

    __tablename__ = "risk_assessments"
    __table_args__ = (
        enum_check("action_type", ActionType, "ck_ra_action"),
        Index("ix_ra_resource", "resource_type", "resource_id"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    s_sensitivity: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    f_financial: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    i_irreversibility: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    e_evidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    t_tool: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    u_uncertainty: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    risk_score: Mapped[Decimal] = mapped_column(Score, nullable=False)
    factor_rationales: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    policy_version_id: Mapped[uuid.UUID] = _fk("policy_versions")
    requested_by: Mapped[uuid.UUID] = _fk("users")
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()


class GovernanceDecision(Base):
    __tablename__ = "governance_decisions"
    __table_args__ = (enum_check("decision", Decision, "ck_gd_decision"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    risk_assessment_id: Mapped[uuid.UUID] = _fk("risk_assessments")
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    hard_rule_results: Mapped[list] = mapped_column(JSONB, nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False)
    required_approver_role: Mapped[str | None] = mapped_column(String(40))
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    required_next_actions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    thresholds: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at_col()

    risk_assessment: Mapped[RiskAssessment] = relationship(lazy="joined")


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        enum_check("kind", ApprovalKind, "ck_ar_kind"),
        enum_check("status", ApprovalStatus, "ck_ar_status"),
        Index("ix_ar_org_status", "org_id", "status"),
        # Only one open request per resource and kind.
        Index("uq_ar_open_resource", "resource_type", "resource_id", "kind", unique=True,
              postgresql_where=text("status = 'OPEN'")),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    contract_id: Mapped[uuid.UUID | None] = _fk("contracts", nullable=True)
    governance_decision_id: Mapped[uuid.UUID | None] = _fk("governance_decisions", nullable=True)
    required_role: Mapped[str] = mapped_column(String(40), nullable=False)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))
    # Fingerprint of what is being approved/cleared for payments: the payout account. Together with `amount`, an
    # approval only counts while the payment still has exactly this amount and payee (no silent change after approval).
    payee_ref: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ApprovalStatus.OPEN)
    requested_by: Mapped[uuid.UUID] = _fk("users")
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()

    decisions: Mapped[list["ApprovalDecision"]] = relationship(lazy="selectin", order_by="ApprovalDecision.created_at")
    governance_decision: Mapped[GovernanceDecision | None] = relationship(lazy="joined")


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (UniqueConstraint("approval_request_id", "decided_by", name="uq_one_decision_per_user"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    approval_request_id: Mapped[uuid.UUID] = _fk("approval_requests")
    decided_by: Mapped[uuid.UUID] = _fk("users")
    decided_role: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # APPROVE | REJECT | HOLD
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class AuditLog(Base):
    """Append-only, SHA-256 hash-chained. UPDATE/DELETE are blocked by a database trigger."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_org_created", "org_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    org_id: Mapped[uuid.UUID | None] = _fk("organizations", nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID | None] = _fk("users", nullable=True)
    actor_roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    actor_kind: Mapped[str | None] = mapped_column(String(16))
    resource_type: Mapped[str | None] = mapped_column(String(40))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    contract_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="SUCCESS")  # SUCCESS | DENIED | FAILURE
    previous_state: Mapped[str | None] = mapped_column(String(40))
    new_state: Mapped[str | None] = mapped_column(String(40))
    request_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditChainHead(Base):
    __tablename__ = "audit_chain_head"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    user_id: Mapped[uuid.UUID] = _fk("users")
    agent_identity_id: Mapped[uuid.UUID] = _fk("users")
    request_id: Mapped[str | None] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")  # RUNNING|COMPLETED|FAILED|REFUSED
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100))
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    limitations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    structured: Mapped[dict | None] = mapped_column(JSONB)
    governance_decision_id: Mapped[uuid.UUID | None] = _fk("governance_decisions", nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_col()

    tool_calls: Mapped[list["AgentToolCall"]] = relationship(lazy="selectin", order_by="AgentToolCall.created_at")


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    id: Mapped[uuid.UUID] = uuid_pk()
    agent_run_id: Mapped[uuid.UUID] = _fk("agent_runs")
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # OK | DENIED | ERROR | REJECTED
    result_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_col()


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_user_unread", "user_id", "read_at"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    user_id: Mapped[uuid.UUID] = _fk("users")
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    link: Mapped[str | None] = mapped_column(String(300))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID | None] = _fk("organizations", nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # POLICY_COMPARISON | CALIBRATION | OCR | ABLATION
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_config_hash: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    artifacts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()


class JobHeartbeat(Base):
    """Worker/scheduler liveness, shown on the admin health page."""

    __tablename__ = "job_heartbeats"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
