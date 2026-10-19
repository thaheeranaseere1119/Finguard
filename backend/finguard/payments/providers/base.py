"""Outbound payout provider interface (contractor payouts — not inbound checkout/collections)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PayoutRequest:
    idempotency_key: str
    reference_id: str           # our scheduled-payment reference (≤ 40 chars)
    fund_account_id: str
    amount_minor: int           # derived server-side from the approved contract version, never from a client
    currency: str
    narration: str = "Contract payout"
    notes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PayoutResult:
    provider_payout_id: str | None
    provider_status: str        # provider's raw status
    status: str                 # normalized AttemptStatus: PENDING | PROCESSED | FAILED | REVERSED | CANCELLED
    amount_minor: int | None = None
    currency: str | None = None
    reference_id: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    event_type: str
    provider_payout_id: str | None
    status: str | None          # normalized
    amount_minor: int | None
    currency: str | None
    reference_id: str | None
    failure_reason: str | None
    raw: dict[str, Any]


class ProviderError(Exception):
    """Base. Subclasses tell the caller whether the payout outcome is known."""


class ProviderRejected(ProviderError):
    """Provider definitively refused the request (validation/auth error). Nothing was created. Safe to retry after fixing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderNotSent(ProviderError):
    """The request never reached the provider (connection refused/DNS). Nothing was created."""


class ProviderOutcomeUnknown(ProviderError):
    """Timeout or interrupted connection after sending: the payout may or may not exist. Reconcile before retrying."""


class PayoutProvider(ABC):
    name: str = "abstract"
    mode: str = "sandbox"

    @abstractmethod
    def register_beneficiary(self, *, name: str, email: str | None, contractor_ref: str, account_number: str,
                             ifsc: str) -> tuple[str | None, str]:
        """Create contact + fund account; return (contact_id, fund_account_id). Account number is not retained."""

    @abstractmethod
    def create_payout(self, req: PayoutRequest) -> PayoutResult: ...

    @abstractmethod
    def get_payout_status(self, provider_payout_id: str) -> PayoutResult: ...

    @abstractmethod
    def find_by_reference(self, reference_id: str) -> PayoutResult | None:
        """Look up a payout by our reference (used to reconcile when no payout id was received)."""

    def cancel_payout(self, provider_payout_id: str) -> PayoutResult:
        raise ProviderRejected("not_supported", f"{self.name} does not support cancellation")

    @abstractmethod
    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool: ...

    @abstractmethod
    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> WebhookEvent: ...

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "mode": self.mode}
