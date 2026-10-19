"""Contracts, immutable versions, documents, OCR jobs, extraction provenance, validation, approvals, amendments."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from finguard.core.db import Base
from finguard.models.common import Money, created_at_col, enum_check, uuid_pk
from finguard.models.enums import (AmendmentStatus, BusinessDayRule, ContractStatus, FieldStatus, Frequency, OcrJobStatus,
                                   PaymentTiming, ProrationRule, VersionStatus)


def _fk(table: str, nullable: bool = False, **kw) -> Mapped:
    return mapped_column(PG_UUID(as_uuid=True), ForeignKey(f"{table}.id", **kw), nullable=nullable)


class Beneficiary(Base):
    """Payout destination. Only the provider's fund-account reference and the last 4 digits are stored."""

    __tablename__ = "beneficiaries"
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    contractor_id: Mapped[uuid.UUID] = _fk("users")
    account_holder_name: Mapped[str] = mapped_column(String(120), nullable=False)
    ifsc: Mapped[str | None] = mapped_column(String(11))
    account_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_contact_id: Mapped[str | None] = mapped_column(String(64))
    provider_fund_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_by: Mapped[uuid.UUID | None] = _fk("users", nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (
        enum_check("status", ContractStatus, "ck_contracts_status"),
        Index("ix_contracts_org_status", "org_id", "status"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    contractor_id: Mapped[uuid.UUID] = _fk("users")
    beneficiary_id: Mapped[uuid.UUID | None] = _fk("beneficiaries", nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ContractStatus.DRAFT)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_versions.id", use_alter=True, name="fk_contracts_current_version"))
    primary_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_documents.id", use_alter=True, name="fk_contracts_primary_document"))
    latest_extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("extraction_results.id", use_alter=True, name="fk_contracts_latest_extraction"))
    status_reason: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = _fk("users")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = created_at_col()

    contractor = relationship("User", foreign_keys=[contractor_id], lazy="joined")
    current_version = relationship("ContractVersion", foreign_keys=[current_version_id], lazy="joined", post_update=True)
    beneficiary = relationship("Beneficiary", foreign_keys=[beneficiary_id], lazy="joined")


class ContractVersion(Base):
    """Contract terms. Once APPROVED, the row is immutable (DB trigger); changes require an amendment = new version."""

    __tablename__ = "contract_versions"
    __table_args__ = (
        enum_check("status", VersionStatus, "ck_cv_status"),
        enum_check("frequency", Frequency, "ck_cv_frequency"),
        enum_check("payment_timing", PaymentTiming, "ck_cv_timing"),
        enum_check("business_day_rule", BusinessDayRule, "ck_cv_bdr"),
        enum_check("proration_rule", ProrationRule, "ck_cv_proration"),
        CheckConstraint("payment_amount > 0", name="ck_cv_amount_positive"),
        CheckConstraint("total_value IS NULL OR total_value > 0", name="ck_cv_total_positive"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="ck_cv_dates"),
        CheckConstraint("interval_count >= 1 AND interval_count <= 52", name="ck_cv_interval"),
        Index("uq_cv_contract_version_no", "contract_id", "version_no", unique=True,
              postgresql_where=text("version_no IS NOT NULL")),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    contract_id: Mapped[uuid.UUID] = _fk("contracts")
    version_no: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=VersionStatus.PROPOSED)
    contractor_legal_name: Mapped[str | None] = mapped_column(String(200))
    counterparty_name: Mapped[str | None] = mapped_column(String(200))
    total_value: Mapped[Decimal | None] = mapped_column(Money)
    payment_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False)
    interval_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    custom_unit: Mapped[str | None] = mapped_column(String(8))  # DAYS | WEEKS | MONTHS for CUSTOM
    due_day_of_month: Mapped[int | None] = mapped_column(Integer)  # 1..31; 31 means "last day" if shorter month
    due_weekday: Mapped[int | None] = mapped_column(Integer)  # 0=Mon..6=Sun
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    first_payment_date: Mapped[date | None] = mapped_column(Date)
    payment_timing: Mapped[str] = mapped_column(String(8), nullable=False, default=PaymentTiming.ARREARS)
    grace_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proration_rule: Mapped[str] = mapped_column(String(8), nullable=False, default=ProrationRule.NONE)
    business_day_rule: Mapped[str] = mapped_column(String(24), nullable=False, default=BusinessDayRule.FOLLOWING)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    invoice_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_execute_permitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tax_withholding_note: Mapped[str | None] = mapped_column(Text)
    termination_terms: Mapped[str | None] = mapped_column(Text)
    notice_period_days: Mapped[int | None] = mapped_column(Integer)
    effective_from: Mapped[date | None] = mapped_column(Date)
    source_extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("extraction_results.id", use_alter=True, name="fk_cv_extraction"))
    amendment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_amendments.id", use_alter=True, name="fk_cv_amendment"))
    terms_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID] = _fk("users")
    approved_by: Mapped[uuid.UUID | None] = _fk("users", nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class ContractDocument(Base):
    __tablename__ = "contract_documents"
    __table_args__ = (Index("ix_docs_org_sha", "org_id", "sha256"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    contract_id: Mapped[uuid.UUID] = _fk("contracts")
    amendment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_amendments.id", use_alter=True, name="fk_docs_amendment"))
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="CONTRACT")  # CONTRACT|AMENDMENT|INVOICE|SUPPORTING
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    uploaded_by: Mapped[uuid.UUID] = _fk("users")
    created_at: Mapped[datetime] = created_at_col()


class OcrJob(Base):
    __tablename__ = "ocr_jobs"
    __table_args__ = (enum_check("status", OcrJobStatus, "ck_ocr_status"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = _fk("contract_documents")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OcrJobStatus.QUEUED)
    method: Mapped[str | None] = mapped_column(String(16))  # NATIVE | OCR | MIXED | DOCX
    pages_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_ocr: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mean_ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class ExtractionResult(Base):
    """Immutable record of one extraction run (text layer + schema-constrained fields + provenance)."""

    __tablename__ = "extraction_results"
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    document_id: Mapped[uuid.UUID] = _fk("contract_documents")
    ocr_job_id: Mapped[uuid.UUID] = _fk("ocr_jobs")
    pages: Mapped[list] = mapped_column(JSONB, nullable=False)  # [{page, method, text, ocr_confidence, lines:[{text,bbox,conf}]}]
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    extractor: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "rules+mock" / "rules+gemini"
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    injection_report: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    quality_flags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = created_at_col()

    fields: Mapped[list["ExtractedField"]] = relationship(lazy="selectin", order_by="ExtractedField.field_name")


class ExtractedField(Base):
    """Immutable extracted value with provenance. Corrections are separate rows in field_corrections."""

    __tablename__ = "extracted_fields"
    __table_args__ = (
        UniqueConstraint("extraction_id", "field_name", name="uq_field_per_extraction"),
        enum_check("status", FieldStatus, "ck_field_status"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = _fk("extraction_results")
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str | None] = mapped_column(Text)              # normalized machine value as string (e.g. "20000.00")
    display_value: Mapped[str | None] = mapped_column(Text)
    null_reason: Mapped[str | None] = mapped_column(Text)
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_text: Mapped[str | None] = mapped_column(Text)
    source_bbox: Mapped[list | None] = mapped_column(JSONB)
    extraction_confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)  # computed, field-level
    model_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))               # LLM self-report (not trusted)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    is_critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issues: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class FieldCorrection(Base):
    __tablename__ = "field_corrections"
    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = _fk("extraction_results")
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_by: Mapped[uuid.UUID] = _fk("users")
    created_at: Mapped[datetime] = created_at_col()


class ContractValidation(Base):
    __tablename__ = "contract_validations"
    id: Mapped[uuid.UUID] = uuid_pk()
    contract_id: Mapped[uuid.UUID] = _fk("contracts")
    contract_version_id: Mapped[uuid.UUID] = _fk("contract_versions")
    validator_id: Mapped[uuid.UUID] = _fk("users")
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # PASSED | FAILED | CHANGES_REQUESTED
    checks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class ContractApproval(Base):
    __tablename__ = "contract_approvals"
    id: Mapped[uuid.UUID] = uuid_pk()
    contract_id: Mapped[uuid.UUID] = _fk("contracts")
    contract_version_id: Mapped[uuid.UUID] = _fk("contract_versions")
    amendment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_amendments.id", use_alter=True, name="fk_ca_amendment"))
    approver_id: Mapped[uuid.UUID] = _fk("users")
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # APPROVED | REJECTED
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    risk_assessment_id: Mapped[uuid.UUID | None] = _fk("risk_assessments", nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class ContractAmendment(Base):
    __tablename__ = "contract_amendments"
    __table_args__ = (enum_check("status", AmendmentStatus, "ck_amend_status"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    contract_id: Mapped[uuid.UUID] = _fk("contracts")
    base_version_id: Mapped[uuid.UUID] = _fk("contract_versions")
    proposed_version_id: Mapped[uuid.UUID | None] = _fk("contract_versions", nullable=True)
    requested_by: Mapped[uuid.UUID] = _fk("users")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    proposed_changes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    diff: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=AmendmentStatus.DRAFT)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contract_documents.id", use_alter=True, name="fk_amend_doc"))
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("extraction_results.id", use_alter=True, name="fk_amend_extraction"))
    risk_assessment_id: Mapped[uuid.UUID | None] = _fk("risk_assessments", nullable=True)
    decided_by: Mapped[uuid.UUID | None] = _fk("users", nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("contract_id", "invoice_number", name="uq_invoice_number"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = _fk("organizations")
    contract_id: Mapped[uuid.UUID] = _fk("contracts")
    scheduled_payment_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("scheduled_payments.id", use_alter=True, name="fk_invoice_sp"), nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    document_id: Mapped[uuid.UUID | None] = _fk("contract_documents", nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="SUBMITTED")
    reviewed_by: Mapped[uuid.UUID | None] = _fk("users", nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    submitted_by: Mapped[uuid.UUID] = _fk("users")
    created_at: Mapped[datetime] = created_at_col()
