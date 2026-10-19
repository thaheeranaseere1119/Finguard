"""Status vocabularies. Stored as VARCHAR with CHECK constraints so values are enforced in PostgreSQL."""

from __future__ import annotations

from enum import StrEnum


class UserKind(StrEnum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"


class RoleName(StrEnum):
    CONTRACTOR = "CONTRACTOR"
    FINANCE_ANALYST = "FINANCE_ANALYST"
    FINANCE_APPROVER = "FINANCE_APPROVER"
    RISK_OFFICER = "RISK_OFFICER"
    FINANCE_MANAGER = "FINANCE_MANAGER"
    AUDITOR = "AUDITOR"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"
    AI_AGENT = "AI_AGENT"
    SCHEDULER = "SCHEDULER"


class ContractStatus(StrEnum):
    DRAFT = "DRAFT"
    UPLOADED = "UPLOADED"
    OCR_PROCESSING = "OCR_PROCESSING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    PENDING_VALIDATION = "PENDING_VALIDATION"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"
    TERMINATED = "TERMINATED"
    OCR_FAILED = "OCR_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class VersionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class OcrJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class FieldStatus(StrEnum):
    OK = "OK"
    MISSING = "MISSING"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"


class Frequency(StrEnum):
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUALLY = "ANNUALLY"
    CUSTOM = "CUSTOM"


class PaymentTiming(StrEnum):
    ADVANCE = "ADVANCE"
    ARREARS = "ARREARS"


class BusinessDayRule(StrEnum):
    NONE = "NONE"
    FOLLOWING = "FOLLOWING"
    PRECEDING = "PRECEDING"
    MODIFIED_FOLLOWING = "MODIFIED_FOLLOWING"


class ProrationRule(StrEnum):
    NONE = "NONE"
    DAILY = "DAILY"


class AmendmentStatus(StrEnum):
    DRAFT = "DRAFT"
    OCR_PROCESSING = "OCR_PROCESSING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class ScheduledPaymentStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    PROCESSING = "PROCESSING"          # claimed by a worker
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ESCALATED = "ESCALATED"
    HELD = "HELD"                      # needs verification / evidence (VERIFY) or manual hold
    APPROVED_FOR_EXECUTION = "APPROVED_FOR_EXECUTION"
    SUBMITTED = "SUBMITTED"            # sent to provider, outcome pending
    UNKNOWN = "UNKNOWN"                # provider outcome unknown; must reconcile before any retry
    PAID = "PAID"
    FAILED = "FAILED"                  # confirmed failure; a retry is safe
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"          # replaced by an amendment's regenerated schedule


SP_OPEN = {ScheduledPaymentStatus.SCHEDULED, ScheduledPaymentStatus.PROCESSING, ScheduledPaymentStatus.AWAITING_APPROVAL,
           ScheduledPaymentStatus.ESCALATED, ScheduledPaymentStatus.HELD, ScheduledPaymentStatus.APPROVED_FOR_EXECUTION,
           ScheduledPaymentStatus.FAILED, ScheduledPaymentStatus.BLOCKED}


class AttemptStatus(StrEnum):
    CREATED = "CREATED"        # persisted before the provider call
    PENDING = "PENDING"        # provider accepted, not final (queued/processing)
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"        # timeout / network error: outcome not known


ATTEMPT_ACTIVE = (AttemptStatus.CREATED, AttemptStatus.PENDING, AttemptStatus.PROCESSED, AttemptStatus.UNKNOWN)


class Decision(StrEnum):
    ACT = "ACT"
    APPROVE = "APPROVE"
    ESCALATE = "ESCALATE"
    VERIFY = "VERIFY"
    BLOCK = "BLOCK"


class ActionType(StrEnum):
    CONTRACT_APPROVAL = "CONTRACT_APPROVAL"
    AMENDMENT_APPROVAL = "AMENDMENT_APPROVAL"
    PAYMENT_EXECUTION = "PAYMENT_EXECUTION"
    AGENT_QUERY = "AGENT_QUERY"
    AGENT_TOOL = "AGENT_TOOL"


class ApprovalKind(StrEnum):
    CONTRACT_APPROVAL = "CONTRACT_APPROVAL"
    AMENDMENT_APPROVAL = "AMENDMENT_APPROVAL"
    PAYMENT_APPROVAL = "PAYMENT_APPROVAL"
    ESCALATION = "ESCALATION"
    VERIFICATION = "VERIFICATION"


class ApprovalStatus(StrEnum):
    OPEN = "OPEN"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class InvoiceStatus(StrEnum):
    SUBMITTED = "SUBMITTED"     # waiting for manual review (automatic validation switched off by policy)
    ACCEPTED = "ACCEPTED"       # passed validation (by rule) or accepted by finance
    FLAGGED = "FLAGGED"         # failed automatic validation → the claim is escalated to risk review
    REJECTED = "REJECTED"


def values(enum: type[StrEnum]) -> list[str]:
    return [e.value for e in enum]
