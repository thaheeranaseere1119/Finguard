"""RazorpayX Payouts adapter (outbound payouts). UNTESTED against the live sandbox in this repository — it needs a
RazorpayX account with Payouts enabled, test-mode API keys, the test-mode business account number, IP allowlisting
and a webhook secret. See docs/payment-sandbox.md.

Endpoints and behaviour follow the official API reference (checked 2026-10-17):
  POST /v1/contacts · POST /v1/fund_accounts · POST /v1/payouts (header X-Payout-Idempotency, amount in paise)
  GET  /v1/payouts/{id} · GET /v1/payouts?account_number=…&reference_id=… · POST /v1/payouts/{id}/cancel (queued only)
  Webhooks: HMAC-SHA256 hex of the RAW body with the webhook secret in X-Razorpay-Signature; dedupe by
  X-Razorpay-Event-Id.
Ordinary Razorpay Payment Gateway test keys do NOT enable payouts.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from finguard.core.config import get_settings
from finguard.payments.providers.base import (PayoutProvider, PayoutRequest, PayoutResult, ProviderNotSent,
                                              ProviderOutcomeUnknown, ProviderRejected, WebhookEvent)

_STATUS = {"queued": "PENDING", "pending": "PENDING", "processing": "PENDING", "scheduled": "PENDING",
           "processed": "PROCESSED", "failed": "FAILED", "rejected": "FAILED", "cancelled": "CANCELLED",
           "reversed": "REVERSED"}


def normalize_status(raw: str | None) -> str:
    return _STATUS.get((raw or "").lower(), "PENDING")


def parse_razorpay_payout_event(raw_body: bytes, headers: dict[str, str]) -> WebhookEvent:
    data = json.loads(raw_body)
    entity = (((data.get("payload") or {}).get("payout") or {}).get("entity")) or {}
    event_id = headers.get("x-razorpay-event-id") or ""
    if not event_id:
        raise ValueError("missing event id header")
    return WebhookEvent(
        event_id=event_id, event_type=str(data.get("event", "")), provider_payout_id=entity.get("id"),
        status=normalize_status(entity.get("status")) if entity.get("status") else None,
        amount_minor=entity.get("amount"), currency=entity.get("currency"), reference_id=entity.get("reference_id"),
        failure_reason=entity.get("failure_reason") or ((entity.get("status_details") or {}).get("description")),
        raw=data,
    )


class RazorpayXProvider(PayoutProvider):
    name = "razorpayx"
    transport: httpx.BaseTransport | None = None   # test seam: an offline stand-in for the RazorpayX HTTP API

    def __init__(self) -> None:
        s = get_settings()
        missing = [n for n, v in (("RAZORPAYX_KEY_ID", s.razorpayx_key_id), ("RAZORPAYX_KEY_SECRET", s.razorpayx_key_secret),
                                  ("RAZORPAYX_ACCOUNT_NUMBER", s.razorpayx_account_number),
                                  ("RAZORPAYX_WEBHOOK_SECRET", s.razorpayx_webhook_secret)) if not v]
        if missing:
            raise RuntimeError(f"RazorpayX provider selected but not configured: {', '.join(missing)}")
        self.mode = s.payout_mode
        if self.mode == "sandbox" and s.razorpayx_key_id.startswith("rzp_live_"):
            raise RuntimeError("Refusing to use a LIVE RazorpayX key while PAYOUT_MODE=sandbox")
        if self.mode == "live" and not s.razorpayx_key_id.startswith("rzp_live_"):
            raise RuntimeError("PAYOUT_MODE=live requires a live key")
        self._auth = (s.razorpayx_key_id, s.razorpayx_key_secret)
        self._account = s.razorpayx_account_number
        self._webhook_secret = s.razorpayx_webhook_secret
        self._base = s.razorpayx_base_url.rstrip("/")
        self._timeout = httpx.Timeout(s.payout_timeout_seconds, connect=5.0)

    def _call(self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None,
              headers: dict | None = None) -> dict:
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False, transport=self.transport) as c:
                r = c.request(method, f"{self._base}{path}", auth=self._auth, json=json_body, params=params,
                              headers=headers)
        except httpx.ConnectError as exc:
            raise ProviderNotSent("could not connect to RazorpayX") from exc
        except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise ProviderOutcomeUnknown(f"RazorpayX call interrupted: {type(exc).__name__}") from exc
        if r.status_code >= 500:
            raise ProviderOutcomeUnknown(f"RazorpayX HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as exc:
            raise ProviderOutcomeUnknown("RazorpayX returned a non-JSON response") from exc
        if r.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise ProviderRejected(str(err.get("code", r.status_code)), str(err.get("description", "request rejected")))
        return data

    def register_beneficiary(self, *, name, email, contractor_ref, account_number, ifsc):
        contact = self._call("POST", "/v1/contacts", json_body={"name": name[:50], "email": email, "type": "vendor",
                                                                "reference_id": contractor_ref[:40]})
        fa = self._call("POST", "/v1/fund_accounts", json_body={
            "contact_id": contact["id"], "account_type": "bank_account",
            "bank_account": {"name": name[:120], "ifsc": ifsc, "account_number": account_number}})
        return contact["id"], fa["id"]

    def _result(self, d: dict) -> PayoutResult:
        return PayoutResult(d.get("id"), str(d.get("status")), normalize_status(d.get("status")), d.get("amount"),
                            d.get("currency"), d.get("reference_id"),
                            (d.get("status_details") or {}).get("reason") or d.get("failure_reason"),
                            (d.get("status_details") or {}).get("description") or d.get("failure_reason"),
                            {k: d.get(k) for k in ("id", "status", "amount", "currency", "reference_id", "mode", "utr")})

    def create_payout(self, req: PayoutRequest) -> PayoutResult:
        if req.currency != "INR":
            raise ProviderRejected("unsupported_currency", "RazorpayX payouts support INR only")
        d = self._call("POST", "/v1/payouts", headers={"X-Payout-Idempotency": req.idempotency_key}, json_body={
            "account_number": self._account, "fund_account_id": req.fund_account_id, "amount": req.amount_minor,
            "currency": "INR", "mode": "IMPS", "purpose": "vendor bill", "queue_if_low_balance": False,
            "reference_id": req.reference_id[:40], "narration": "".join(ch for ch in req.narration if ch.isalnum() or ch == " ")[:30],
            "notes": {k: str(v)[:256] for k, v in list(req.notes.items())[:15]},
        })
        return self._result(d)

    def get_payout_status(self, provider_payout_id: str) -> PayoutResult:
        return self._result(self._call("GET", f"/v1/payouts/{provider_payout_id}"))

    def find_by_reference(self, reference_id: str) -> PayoutResult | None:
        d = self._call("GET", "/v1/payouts", params={"account_number": self._account, "reference_id": reference_id[:40]})
        items = d.get("items") or []
        return self._result(items[0]) if items else None

    def cancel_payout(self, provider_payout_id: str) -> PayoutResult:
        return self._result(self._call("POST", f"/v1/payouts/{provider_payout_id}/cancel"))

    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        sig = headers.get("x-razorpay-signature", "")
        expected = hmac.new(self._webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return bool(sig) and hmac.compare_digest(expected, sig)

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> WebhookEvent:
        return parse_razorpay_payout_event(raw_body, headers)
