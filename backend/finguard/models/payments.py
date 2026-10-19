"""Payment schedules, scheduled occurrences, provider attempts and provider events."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finguard.core.db import Base
from finguard.models.common import Money, created_at_col, enum_check, uuid_pk
from finguard.models.enums import AttemptStatus, ScheduledPaymentStatus


def _fk(table: str, nullable: bool = False) -> Mapped:
    return mapped_column(PG_UUID(as_uuid=True), ForeignKey(f"{table}.id"), nullable=nullable)


class PaymentSchedule(Base):
    __tablename__ = "payment_schedules"
    id: Mapped[uuid.UUID] = uuid_pk()
    contract_id: Mapped[uuid.UUID] = _fk("contracts")
    contract_version_id: Mapped[uuid.UUID] = _fk("contract_versions")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")  # ACTIVE | SUPERSEDED | CLOSED
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    generator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID] = _fk("users")
    created_at: Mapped[datetime] = created_at_col()


class ScheduledPayment(Base):
    __tablename__ = "scheduled_payments"
    __table_args__ = (
        enum_check("status", ScheduledPaymentStatus, "ck_sp_status"),
        # A contract can have only one live occurrence per period.
        Index("uq_sp_contract_period_live", "contract_id", "period_start", unique=True,
              postgresql_where=text("status NOT IN ('SUPERSEDED','CANCELLED')")),
        Index("ix_sp_due", "status", "due_at"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    schedule_id: Mapped[uuid.UUID] = _fk("payment_schedules")
    contract_id: Mapped[uuid.UUID] = _fk("contracts")
    contract_version_id: Mapped[uuid.UUID] = _fk("contract_versions")
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    nominal_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_prorated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=ScheduledPaymentStatus.SCHEDULED)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    hold_reason: Mapped[str | None] = mapped_column(Text)
    last_decision_id: Mapped[uuid.UUID | None] = _fk("governance_decisions", nullable=True)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("approval_requests.id", use_alter=True, name="fk_sp_approval"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(100))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = created_at_col()

    attempts: Mapped[list["PaymentAttempt"]] = relationship(lazy="selectin", order_by="PaymentAttempt.attempt_no")


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        enum_check("status", AttemptStatus, "ck_attempt_status"),
        UniqueConstraint("scheduled_payment_id", "attempt_no", name="uq_attempt_no"),
        # At most ONE attempt per occurrence may be live or successful: blocks duplicate payouts at the DB level.
        Index("uq_attempt_one_active", "scheduled_payment_id", unique=True,
              postgresql_where=text("status IN ('CREATED','PENDING','PROCESSED','UNKNOWN')")),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    scheduled_payment_id: Mapped[uuid.UUID] = _fk("scheduled_payments")
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_payout_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fund_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AttemptStatus.CREATED)
    provider_status: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    initiated_by: Mapped[uuid.UUID] = _fk("users")
    governance_decision_id: Mapped[uuid.UUID] = _fk("governance_decisions")
    response_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = created_at_col()


class PaymentProviderEvent(Base):
    __tablename__ = "payment_provider_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_provider_event"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_payout_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payment_attempt_id: Mapped[uuid.UUID | None] = _fk("payment_attempts", nullable=True)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)  # APPLIED | DUPLICATE | REJECTED | IGNORED | MISMATCH
    detail: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = created_at_col()


class SandboxPayout(Base):
    """State store of the MOCK sandbox provider only (simulates the provider's own database). Never used for real money."""

    __tablename__ = "sandbox_payouts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    reference_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fund_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    scenario: Mapped[str] = mapped_column(String(40), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    final_status: Mapped[str | None] = mapped_column(String(16))  # for pending scenarios, where it will settle
    webhook_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = created_at_col()
