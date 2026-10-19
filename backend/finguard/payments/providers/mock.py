"""Deterministic MOCK sandbox payout provider — no money moves, ever.

It simulates a provider's own database (table sandbox_payouts, written in an independent transaction like an
external system) and signs webhooks with MOCK_WEBHOOK_SECRET using the same HMAC-SHA256 scheme as RazorpayX, so the
real webhook verification code path is exercised.

Scenario is selected by the LAST FOUR DIGITS of the beneficiary account number at registration:
  0000 success              1111 rejected by provider      2222 insufficient funds
  3333 invalid beneficiary  4444 timeout after creation    5555 pending → processed (webhook)
  6666 pending → failed     7777 network error (not sent)  8888 timeout, payout never created
  9999 processed then reversed
Any other suffix behaves like 0000.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from finguard.core.config import get_settings
from finguard.core.db import new_session
from finguard.models import SandboxPayout
from finguard.payments.providers.base import (PayoutProvider, PayoutRequest, PayoutResult, ProviderNotSent,
                                              ProviderOutcomeUnknown, ProviderRejected, WebhookEvent)

SCENARIOS = {"0000": "success", "1111": "rejected", "2222": "insufficient_funds", "3333": "invalid_beneficiary",
             "4444": "timeout_after_create", "5555": "pending_then_success", "6666": "pending_then_failure",
             "7777": "network_error", "8888": "timeout_not_created", "9999": "processed_then_reversed"}


def _scenario_from_fund_account(fa: str) -> str:
    # Format: fa_mock_<8 hex>_<scenario>; scenario names themselves contain underscores.
    parts = fa.split("_", 3)
    return parts[3] if fa.startswith("fa_mock_") and len(parts) == 4 and parts[3] in SCENARIOS.values() else "success"


class MockSandboxPaymentProvider(PayoutProvider):
    name = "mock"
    mode = "sandbox"

    def __init__(self) -> None:
        self._secret = get_settings().mock_webhook_secret

    # --- beneficiaries
    def register_beneficiary(self, *, name, email, contractor_ref, account_number, ifsc):
        digits = "".join(ch for ch in account_number if ch.isdigit())
        if len(digits) < 5:
            raise ProviderRejected("invalid_account", "account number must have at least 5 digits")
        scenario = SCENARIOS.get(digits[-4:], "success")
        return f"cont_mock_{uuid.uuid4().hex[:10]}", f"fa_mock_{uuid.uuid4().hex[:8]}_{scenario}"

    # --- payouts
    def create_payout(self, req: PayoutRequest) -> PayoutResult:
        scenario = _scenario_from_fund_account(req.fund_account_id)
        if scenario == "network_error":
            raise ProviderNotSent("simulated connection refused before the request was sent")
        if scenario == "timeout_not_created":
            raise ProviderOutcomeUnknown("simulated timeout; request was lost before the provider stored it")
        if scenario == "rejected":
            raise ProviderRejected("BAD_REQUEST_ERROR", "simulated provider validation rejection")
        with new_session() as s:
            existing = s.execute(select(SandboxPayout).where(SandboxPayout.idempotency_key == req.idempotency_key)).scalar_one_or_none()
            if existing:  # idempotent replay: same key returns the same payout, never a second one
                return self._result(existing)
            status, final, reason = {
                "success": ("processed", None, None),
                "insufficient_funds": ("failed", None, "insufficient_funds"),
                "invalid_beneficiary": ("failed", None, "invalid_beneficiary_account"),
                "timeout_after_create": ("processed", None, None),
                "pending_then_success": ("processing", "processed", None),
                "pending_then_failure": ("processing", "failed", "beneficiary_bank_rejected"),
                "processed_then_reversed": ("processed", "reversed", "reversed_by_bank"),
            }.get(scenario, ("processed", None, None))
            p = SandboxPayout(id=f"pout_mock_{uuid.uuid4().hex[:14]}", idempotency_key=req.idempotency_key,
                              reference_id=req.reference_id, fund_account_id=req.fund_account_id,
                              amount_minor=req.amount_minor, currency=req.currency, status=status, scenario=scenario,
                              failure_reason=reason if status == "failed" else None, final_status=final)
            s.add(p)
            s.commit()
            result = self._result(p)
        if scenario == "timeout_after_create":
            raise ProviderOutcomeUnknown("simulated timeout after the provider created the payout")
        return result

    def get_payout_status(self, provider_payout_id: str) -> PayoutResult:
        with new_session() as s:
            p = s.get(SandboxPayout, provider_payout_id)
            if p is None:
                raise ProviderRejected("NOT_FOUND", "payout not found")
            return self._result(p)

    def find_by_reference(self, reference_id: str) -> PayoutResult | None:
        with new_session() as s:
            p = s.execute(select(SandboxPayout).where(SandboxPayout.reference_id == reference_id)
                          .order_by(SandboxPayout.created_at.desc())).scalars().first()
            return self._result(p) if p else None

    def cancel_payout(self, provider_payout_id: str) -> PayoutResult:
        with new_session() as s:
            p = s.get(SandboxPayout, provider_payout_id)
            if p is None or p.status not in ("queued", "processing"):
                raise ProviderRejected("NOT_CANCELLABLE", "only queued/processing sandbox payouts can be cancelled")
            p.status, p.final_status = "cancelled", None
            s.commit()
            return self._result(p)

    # --- webhooks
    def sign(self, body: bytes) -> str:
        return hmac.new(self._secret.encode(), body, hashlib.sha256).hexdigest()

    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        sig = headers.get("x-razorpay-signature") or headers.get("x-mock-signature") or ""
        return bool(sig) and hmac.compare_digest(self.sign(raw_body), sig)

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> WebhookEvent:
        from finguard.payments.providers.razorpayx import parse_razorpay_payout_event

        return parse_razorpay_payout_event(raw_body, headers)

    def settle_pending(self) -> list[tuple[bytes, dict[str, str]]]:
        """Advance pending sandbox payouts to their final status; return signed webhook deliveries to post."""
        deliveries = []
        with new_session() as s:
            rows = s.execute(select(SandboxPayout).where(
                (SandboxPayout.final_status.is_not(None)) | (SandboxPayout.webhook_sent.is_(False)))).scalars().all()
            for p in rows:
                if p.final_status:
                    p.status, p.failure_reason = p.final_status, (p.failure_reason or None)
                    if p.status == "failed" and not p.failure_reason:
                        p.failure_reason = "beneficiary_bank_rejected"
                    p.final_status = None
                p.webhook_sent = True
                p.updated_at = datetime.now(timezone.utc)
                deliveries.append(self.webhook_for(p))
            s.commit()
        return deliveries

    def webhook_for(self, p: SandboxPayout, event_id: str | None = None) -> tuple[bytes, dict[str, str]]:
        body = json.dumps({
            "entity": "event", "event": f"payout.{p.status}", "contains": ["payout"],
            "payload": {"payout": {"entity": {"id": p.id, "entity": "payout", "fund_account_id": p.fund_account_id,
                                              "amount": p.amount_minor, "currency": p.currency, "status": p.status,
                                              "reference_id": p.reference_id, "failure_reason": p.failure_reason,
                                              "mode": "IMPS"}}},
            "created_at": int(datetime.now(timezone.utc).timestamp()),
        }, separators=(",", ":")).encode()
        eid = event_id or f"evt_mock_{hashlib.sha256(body).hexdigest()[:16]}"
        return body, {"x-razorpay-signature": self.sign(body), "x-razorpay-event-id": eid}

    @staticmethod
    def _result(p: SandboxPayout) -> PayoutResult:
        from finguard.payments.providers.razorpayx import normalize_status

        return PayoutResult(p.id, p.status, normalize_status(p.status), p.amount_minor, p.currency, p.reference_id,
                            p.failure_reason if p.status == "failed" else None, p.failure_reason,
                            {"id": p.id, "status": p.status, "scenario": p.scenario})
