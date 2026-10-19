"""RazorpayX adapter contract tests — the whole payment pipeline with PAYOUT_PROVIDER=razorpayx.

Razorpay's servers are replaced by an OFFLINE stand-in (httpx.MockTransport) that implements the documented Payouts API:
Basic auth with test keys, /v1/contacts, /v1/fund_accounts, /v1/payouts (mandatory X-Payout-Idempotency, amount in paise,
error envelope {"error": {"code", "description"}}), GET by id / by reference_id, and HMAC-SHA256-signed webhooks.
This proves FinGuard's side of the integration; it is NOT a call to Razorpay's real sandbox (that needs test keys).

Outcome is chosen by the last four digits of the bank account (same convention as the mock sandbox):
  0000 processing → processed by webhook · 1111 rejected (400) · 4444 created, then HTTP 503 (outcome unknown)
  8888 read timeout before creation · 9999 processed, then reversed by webhook
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from finguard.models import PaymentAttempt, ScheduledPayment
from finguard.services import payments
from tests.conftest import drain, onboard

KEY_ID, KEY_SECRET, ACCOUNT, WEBHOOK_SECRET = "rzp_test_FinGuardContract", "test_secret_contract", "2323230041626905", "whsec_contract"


class FakeRazorpayX:
    def __init__(self) -> None:
        self.contacts: dict[str, dict] = {}
        self.fund_accounts: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}
        self.idem: dict[str, tuple[str, str]] = {}      # idempotency key -> (body hash, payout id)
        self.requests: list[httpx.Request] = []

    @staticmethod
    def _err(status, desc, code="BAD_REQUEST_ERROR"):
        return httpx.Response(status, json={"error": {"code": code, "description": desc, "source": "business",
                                                      "step": "payout_initiation", "reason": "input_validation_failed"}})

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        auth = req.headers.get("authorization", "")
        if auth != "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode():
            return self._err(401, "The api key provided is invalid")
        path, body = req.url.path, json.loads(req.content or b"{}")
        if req.method == "POST" and path == "/v1/contacts":
            cid = f"cont_{uuid.uuid4().hex[:14]}"
            self.contacts[cid] = {"id": cid, "entity": "contact", "active": True, **body}
            return httpx.Response(200, json=self.contacts[cid])
        if req.method == "POST" and path == "/v1/fund_accounts":
            if body.get("contact_id") not in self.contacts:
                return self._err(400, "The id provided does not exist")
            ba = body["bank_account"]
            if len(ba["ifsc"]) != 11:
                return self._err(400, "The ifsc must be 11 characters.")
            fid = f"fa_{uuid.uuid4().hex[:14]}"
            self.fund_accounts[fid] = {"id": fid, "entity": "fund_account", "contact_id": body["contact_id"],
                                       "account_type": "bank_account", "active": True,
                                       "bank_account": {"ifsc": ba["ifsc"], "name": ba["name"], "account_number": ba["account_number"]}}
            return httpx.Response(200, json=self.fund_accounts[fid])
        if req.method == "POST" and path == "/v1/payouts":
            key = req.headers.get("x-payout-idempotency")
            if not key:
                return self._err(400, "X-Payout-Idempotency header is required")
            digest = hashlib.sha256(req.content).hexdigest()
            if key in self.idem:
                h, pid = self.idem[key]
                if h != digest:
                    return self._err(400, "Different request body sent for the same idempotency header")
                return httpx.Response(200, json=self.payouts[pid])      # replay → same payout, no second transfer
            if body.get("account_number") != ACCOUNT:
                return self._err(400, "Invalid account number")
            fa = self.fund_accounts.get(body.get("fund_account_id"))
            if not fa:
                return self._err(400, "The id provided does not exist")
            if not isinstance(body.get("amount"), int) or body["amount"] < 100 or body.get("currency") != "INR":
                return self._err(400, "The amount must be at least INR 1.00.")
            suffix = fa["bank_account"]["account_number"][-4:]
            if suffix == "8888":
                raise httpx.ReadTimeout("simulated timeout before the payout was created", request=req)
            if suffix == "1111":
                return self._err(400, "Payout failed: beneficiary bank rejected the transfer")
            pid = f"pout_{uuid.uuid4().hex[:14]}"
            self.payouts[pid] = {"id": pid, "entity": "payout", "fund_account_id": fa["id"], "amount": body["amount"],
                                 "currency": "INR", "fees": 590, "tax": 90, "status": "processing", "utr": None,
                                 "mode": body["mode"], "purpose": body["purpose"], "reference_id": body.get("reference_id"),
                                 "narration": body.get("narration"), "notes": body.get("notes", {}),
                                 "status_details": None, "failure_reason": None}
            self.idem[key] = (digest, pid)
            if suffix == "4444":
                return httpx.Response(503, text="Service Unavailable")   # created, but the response is lost
            return httpx.Response(200, json=self.payouts[pid])
        if req.method == "GET" and path.startswith("/v1/payouts/"):
            p = self.payouts.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=p) if p else self._err(400, "The id provided does not exist")
        if req.method == "GET" and path == "/v1/payouts":
            ref = req.url.params.get("reference_id")
            items = [p for p in self.payouts.values() if p["reference_id"] == ref]
            return httpx.Response(200, json={"entity": "collection", "count": len(items), "items": items})
        return self._err(404, "The requested URL was not found on the server.")

    # ---- provider-side state changes + signed webhooks
    def settle(self, pid: str, status: str) -> None:
        p = self.payouts[pid]
        p["status"] = status
        if status == "processed":
            p["utr"] = f"UTR{uuid.uuid4().hex[:12].upper()}"

    def webhook(self, pid: str, event: str) -> tuple[bytes, dict[str, str]]:
        body = json.dumps({"entity": "event", "account_id": "acc_FinGuardTest", "event": event, "contains": ["payout"],
                           "payload": {"payout": {"entity": self.payouts[pid]}}, "created_at": 1760000000}).encode()
        sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return body, {"x-razorpay-signature": sig, "x-razorpay-event-id": f"evt_{uuid.uuid4().hex[:14]}"}


@pytest.fixture
def rzp(monkeypatch):
    from finguard.core.config import get_settings
    from finguard.payments.providers import get_payout_provider
    from finguard.payments.providers.razorpayx import RazorpayXProvider

    s = get_settings()
    for k, v in (("payout_provider", "razorpayx"), ("payout_mode", "sandbox"), ("razorpayx_key_id", KEY_ID),
                 ("razorpayx_key_secret", KEY_SECRET), ("razorpayx_account_number", ACCOUNT),
                 ("razorpayx_webhook_secret", WEBHOOK_SECRET)):
        monkeypatch.setattr(s, k, v)
    fake = FakeRazorpayX()
    monkeypatch.setattr(RazorpayXProvider, "transport", httpx.MockTransport(fake.handle))
    get_payout_provider.cache_clear()
    assert get_payout_provider().name == "razorpayx" and get_payout_provider().mode == "sandbox"
    yield fake
    get_payout_provider.cache_clear()


def _due(db, c, now):
    db.expire_all()
    return db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id, ScheduledPayment.due_at <= now)
                      .order_by(ScheduledPayment.sequence_no)).scalars().all()


def _attempt(db, sp):
    return db.execute(select(PaymentAttempt).where(PaymentAttempt.scheduled_payment_id == sp.id)
                      .order_by(PaymentAttempt.attempt_no.desc())).scalars().first()


def test_success_via_webhook_idempotency_and_signature(db, make_org, now, rzp):
    o = make_org()
    c = onboard(db, o, account="50100000040000")
    # beneficiary registration created a RazorpayX contact + fund account; only the reference is stored
    assert len(rzp.contacts) == 1 and len(rzp.fund_accounts) == 1
    from finguard.models import Beneficiary

    ben = db.get(Beneficiary, c.beneficiary_id)
    assert ben.provider == "razorpayx" and ben.provider_fund_account_id in rzp.fund_accounts and ben.account_last4 == "0000"
    drain(db, now)
    sp = next(s for s in _due(db, c, now) if s.status in ("SUBMITTED", "AWAITING_APPROVAL"))
    if sp.status == "AWAITING_APPROVAL":
        from finguard.services import approvals

        approvals.decide(db, o.p(db, "approver"), sp.approval_request_id, "APPROVE", "Within limit and matches schedule")
        db.commit()
        db.expire_all()
        sp = db.get(ScheduledPayment, sp.id)
    at = _attempt(db, sp)
    assert sp.status == "SUBMITTED" and at.status == "PENDING" and at.provider == "razorpayx" and at.provider_mode == "sandbox"
    payout = rzp.payouts[at.provider_payout_id]
    assert payout["amount"] == int(sp.amount * 100) and payout["currency"] == "INR" and payout["mode"] == "IMPS"
    post = next(r for r in rzp.requests if r.url.path == "/v1/payouts")
    assert post.headers["x-payout-idempotency"] == at.idempotency_key
    # replaying the same request (e.g. a client retry) returns the SAME payout — no second transfer
    from finguard.payments.providers import get_payout_provider
    from finguard.payments.providers.base import PayoutRequest

    replay = get_payout_provider().create_payout(PayoutRequest(
        idempotency_key=at.idempotency_key, reference_id=payout["reference_id"], fund_account_id=payout["fund_account_id"],
        amount_minor=payout["amount"], currency="INR", narration=payout["narration"], notes=payout["notes"]))
    assert replay.provider_payout_id == at.provider_payout_id and len(rzp.payouts) == len(rzp.idem)
    # forged webhook rejected; genuine signed webhook marks it PAID; duplicate delivery ignored
    rzp.settle(at.provider_payout_id, "processed")
    body, headers = rzp.webhook(at.provider_payout_id, "payout.processed")
    assert payments.handle_webhook(db, "razorpayx", body, {**headers, "x-razorpay-signature": "0" * 64})["outcome"] == "REJECTED"
    assert payments.handle_webhook(db, "razorpayx", body, headers)["outcome"] == "APPLIED"
    assert payments.handle_webhook(db, "razorpayx", body, headers)["outcome"] == "DUPLICATE"
    db.expire_all()
    assert db.get(ScheduledPayment, sp.id).status == "PAID"


def test_provider_rejection_marks_failed(db, make_org, now, rzp):
    o = make_org()
    c = onboard(db, o, account="50100000041111")
    drain(db, now)
    failed = [s for s in _due(db, c, now) if s.status == "FAILED"]
    assert failed and "rejected" in failed[0].hold_reason.lower()
    assert _attempt(db, failed[0]).failure_code == "BAD_REQUEST_ERROR"


def test_lost_response_is_unknown_then_reconciled(db, make_org, now, rzp):
    """HTTP 503 after the payout was created: no blind retry; reconciliation finds it by reference_id."""
    o = make_org()
    c = onboard(db, o, account="50100000044444")
    drain(db, now)
    sp = next(s for s in _due(db, c, now) if s.status == "UNKNOWN")
    at = _attempt(db, sp)
    assert at.status == "UNKNOWN" and at.provider_payout_id is None
    n_payouts = len(rzp.payouts)
    drain(db, now)                                      # the scheduler must NOT create another payout
    assert len(rzp.payouts) == n_payouts
    pid = next(p["id"] for p in rzp.payouts.values() if p["reference_id"] == payments._reference(sp, at.attempt_no))
    rzp.settle(pid, "processed")
    payments.reconcile_attempt(db, o.p(db, "analyst"), at)
    db.expire_all()
    assert db.get(ScheduledPayment, sp.id).status == "PAID" and _attempt(db, sp).provider_payout_id == pid


def test_timeout_before_creation_reconciles_to_safe_retry(db, make_org, now, rzp):
    o = make_org()
    c = onboard(db, o, account="50100000048888")
    drain(db, now)
    sp = next(s for s in _due(db, c, now) if s.status == "UNKNOWN")
    payments.reconcile_attempt(db, o.p(db, "analyst"), _attempt(db, sp))
    db.expire_all()
    sp = db.get(ScheduledPayment, sp.id)
    assert sp.status == "FAILED" and "never created" in sp.hold_reason and not rzp.payouts


def test_reversal_webhook(db, make_org, now, rzp):
    o = make_org()
    c = onboard(db, o, account="50100000049999")
    drain(db, now)
    sp = next(s for s in _due(db, c, now) if s.status == "SUBMITTED")
    pid = _attempt(db, sp).provider_payout_id
    rzp.settle(pid, "processed")
    assert payments.handle_webhook(db, "razorpayx", *rzp.webhook(pid, "payout.processed"))["outcome"] == "APPLIED"
    rzp.settle(pid, "reversed")
    assert payments.handle_webhook(db, "razorpayx", *rzp.webhook(pid, "payout.reversed"))["outcome"] == "APPLIED"
    db.expire_all()
    assert _attempt(db, sp).status == "REVERSED"


def test_invoice_claim_auto_pays_through_razorpayx(db, make_org, now, rzp):
    """Verified contractor's matching invoice → auto-accepted → payout created at RazorpayX → webhook → PAID."""
    from finguard.services import approvals, invoicing

    o = make_org()
    c = onboard(db, o, amount=85000, invoice_required=True, account="50100000040000")
    drain(db, now)
    sp = next(s for s in _due(db, c, now) if s.status == "HELD")
    inv = invoicing.claim(db, o.p(db, "contractor"), sp.id)
    db.commit()
    assert inv.status == "ACCEPTED" and inv.release["decision"] in ("ACT", "APPROVE")
    db.expire_all()
    sp = db.get(ScheduledPayment, sp.id)
    if sp.status == "AWAITING_APPROVAL":
        approvals.decide(db, o.p(db, "approver"), sp.approval_request_id, "APPROVE", "Within limit and matches schedule")
        db.commit()
        db.expire_all()
    at = _attempt(db, sp)
    assert at.provider == "razorpayx" and rzp.payouts[at.provider_payout_id]["amount"] == 8500000
    rzp.settle(at.provider_payout_id, "processed")
    assert payments.handle_webhook(db, "razorpayx", *rzp.webhook(at.provider_payout_id, "payout.processed"))["outcome"] == "APPLIED"
    db.expire_all()
    assert db.get(ScheduledPayment, sp.id).status == "PAID"


def test_wrong_keys_are_rejected_without_storing_anything(db, make_org, rzp, monkeypatch):
    from finguard.core.config import get_settings
    from finguard.core.errors import ProviderError
    from finguard.payments.providers import get_payout_provider

    monkeypatch.setattr(get_settings(), "razorpayx_key_secret", "wrong")
    get_payout_provider.cache_clear()
    with pytest.raises(ProviderError, match="api key"):
        onboard(db, make_org(), account="50100000040000")
    db.rollback()
