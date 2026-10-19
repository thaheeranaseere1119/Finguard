"""LIVE Dwolla SANDBOX checks (real HTTPS calls to api-sandbox.dwolla.com — test money only).

Opt-in:  RUN_DWOLLA_SANDBOX=1 pytest tests/integration/test_dwolla_sandbox.py
Needs DWOLLA_KEY / DWOLLA_SECRET (sandbox application) in .env. Dwolla is USD-only, so these use USD contracts and
US bank details (routing 222222226 is Dwolla's sandbox test routing number). Pending transfers are settled with
Dwolla's /sandbox-simulations endpoint, then FinGuard reconciles them exactly as it would in production.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import uuid

import pytest
from sqlalchemy import select

from finguard.models import Beneficiary, PaymentAttempt, ScheduledPayment
from finguard.services import approvals, payments
from tests.conftest import drain, onboard

pytestmark = pytest.mark.skipif(os.environ.get("RUN_DWOLLA_SANDBOX") != "1", reason="live Dwolla sandbox test (opt-in)")
ROUTING = "222222226"


def _acct(suffix: str) -> str:
    return f"{random.randint(10000, 99999)}{suffix}"


@pytest.fixture
def dwolla(monkeypatch):
    from finguard.core.config import Settings, get_settings
    from finguard.payments.providers import get_payout_provider

    file_cfg = Settings()                              # reads DWOLLA_* from .env
    if not (file_cfg.dwolla_key and file_cfg.dwolla_secret):
        pytest.skip("DWOLLA_KEY / DWOLLA_SECRET not configured")
    s = get_settings()
    for k, v in (("payout_provider", "dwolla"), ("payout_mode", "sandbox"), ("dwolla_key", file_cfg.dwolla_key),
                 ("dwolla_secret", file_cfg.dwolla_secret), ("dwolla_webhook_secret", "local-test-webhook-secret"),
                 ("dwolla_base_url", "")):
        monkeypatch.setattr(s, k, v)
    get_payout_provider.cache_clear()
    p = get_payout_provider()
    assert p.name == "dwolla" and p.mode == "sandbox" and p.describe()["base_url"] == "https://api-sandbox.dwolla.com"
    yield p
    get_payout_provider.cache_clear()


def _usd(db, o, amount=1200, suffix="0000", **kw):
    return onboard(db, o, amount=amount, currency="USD", account=_acct(suffix), ifsc_override=ROUTING, **kw)


def _sps(db, c, now):
    db.expire_all()
    return db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id, ScheduledPayment.due_at <= now)
                      .order_by(ScheduledPayment.sequence_no)).scalars().all()


def _attempt(db, sp):
    return db.execute(select(PaymentAttempt).where(PaymentAttempt.scheduled_payment_id == sp.id)
                      .order_by(PaymentAttempt.attempt_no.desc())).scalars().first()


def _approve_if_needed(db, o, sp):
    if sp.status == "AWAITING_APPROVAL":
        approvals.decide(db, o.p(db, "approver"), sp.approval_request_id, "APPROVE", "Within limit and matches schedule")
        db.commit()
        db.expire_all()
    return db.get(ScheduledPayment, sp.id)


def _settle(db, o, provider, sp, want=("PROCESSED", "FAILED")):
    """Run Dwolla sandbox simulations until the transfer settles, reconciling after each round."""
    for _ in range(4):
        provider.simulate_processing()
        at = _attempt(db, sp)
        payments.reconcile_attempt(db, o.p(db, "analyst"), at)
        db.expire_all()
        if _attempt(db, sp).status in want:
            break
    return db.get(ScheduledPayment, sp.id), _attempt(db, sp)


def test_dwolla_end_to_end_payout(db, make_org, now, dwolla):
    o = make_org()
    c = _usd(db, o)
    ben = db.get(Beneficiary, c.beneficiary_id)
    assert ben.provider == "dwolla" and ben.ifsc == ROUTING and ben.provider_fund_account_id   # Dwolla funding source id
    drain(db, now)
    sp = next(s for s in _sps(db, c, now) if s.status in ("SUBMITTED", "AWAITING_APPROVAL"))
    sp = _approve_if_needed(db, o, sp)
    at = _attempt(db, sp)
    assert sp.status == "SUBMITTED" and at.status == "PENDING" and at.provider == "dwolla" and at.provider_payout_id
    t = dwolla.get_payout_status(at.provider_payout_id)
    assert t.amount_minor == 120000 and t.currency == "USD" and t.reference_id == payments._reference(sp, at.attempt_no)
    # idempotent replay: the same Idempotency-Key with the identical request returns the SAME transfer
    # (Dwolla creates a new transfer if the body differs, so FinGuard always rebuilds a byte-identical request)
    from finguard.payments.providers.base import PayoutRequest

    again = dwolla.create_payout(PayoutRequest(idempotency_key=at.idempotency_key, reference_id=t.reference_id,
                                               fund_account_id=at.fund_account_id, amount_minor=120000, currency="USD",
                                               narration=f"FinGuard {sp.sequence_no}",
                                               notes={"scheduled_payment_id": str(sp.id), "contract_id": str(sp.contract_id)}))
    assert again.provider_payout_id == at.provider_payout_id
    sp, at = _settle(db, o, dwolla, sp)
    assert at.status == "PROCESSED" and sp.status == "PAID", (at.status, at.provider_status)


def test_dwolla_ach_return_marks_failed(db, make_org, now, dwolla):
    o = make_org()
    c = _usd(db, o, suffix="1111")                    # sandbox funding source named "R01" → insufficient funds return
    drain(db, now)
    sp = _approve_if_needed(db, o, next(s for s in _sps(db, c, now) if s.status in ("SUBMITTED", "AWAITING_APPROVAL")))
    sp, at = _settle(db, o, dwolla, sp)
    assert at.status == "FAILED" and sp.status == "FAILED" and at.failure_code == "R01", (at.status, at.failure_code)


def test_dwolla_invoice_claim_auto_pays(db, make_org, now, dwolla):
    from finguard.services import invoicing

    o = make_org()
    c = _usd(db, o, amount=950, invoice_required=True)
    drain(db, now)
    sp = next(s for s in _sps(db, c, now) if s.status == "HELD")
    inv = invoicing.claim(db, o.p(db, "contractor"), sp.id)
    db.commit()
    assert inv.status == "ACCEPTED" and inv.release["decision"] in ("ACT", "APPROVE")
    sp = _approve_if_needed(db, o, db.get(ScheduledPayment, sp.id))
    assert _attempt(db, sp).provider == "dwolla"
    sp, at = _settle(db, o, dwolla, sp)
    assert sp.status == "PAID" and dwolla.get_payout_status(at.provider_payout_id).amount_minor == 95000
    # lookup by reference (the recovery path) must find the PAYOUT, not its bank-funding leg (same correlationId)
    import time

    from finguard.payments.providers.base import ProviderNotSent

    found = None
    for _ in range(15):                               # Dwolla's search index lags a few seconds behind
        try:
            found = dwolla.find_by_reference(payments._reference(sp, at.attempt_no))
            break
        except ProviderNotSent:
            time.sleep(2)
    assert found is not None and found.provider_payout_id == at.provider_payout_id
    with pytest.raises(ProviderNotSent):              # an unknown reference defers instead of claiming "never created"
        dwolla.find_by_reference(f"no-such-ref-{uuid.uuid4().hex[:8]}")


def test_dwolla_signed_webhook_reads_the_real_transfer(db, make_org, now, dwolla):
    o = make_org()
    c = _usd(db, o, amount=700)
    drain(db, now)
    sp = _approve_if_needed(db, o, next(s for s in _sps(db, c, now) if s.status in ("SUBMITTED", "AWAITING_APPROVAL")))
    tid = _attempt(db, sp).provider_payout_id
    for _ in range(4):
        dwolla.simulate_processing()
        if dwolla.get_payout_status(tid).status == "PROCESSED":
            break
    body = json.dumps({"id": str(uuid.uuid4()), "resourceId": tid, "topic": "transfer_completed",
                       "timestamp": "2026-10-18T00:00:00Z",
                       "_links": {"resource": {"href": f"https://api-sandbox.dwolla.com/transfers/{tid}"}}}).encode()
    good = {"x-request-signature-sha-256": hmac.new(b"local-test-webhook-secret", body, hashlib.sha256).hexdigest()}
    assert payments.handle_webhook(db, "dwolla", body, {"x-request-signature-sha-256": "0" * 64})["outcome"] == "REJECTED"
    assert payments.handle_webhook(db, "dwolla", body, good)["outcome"] == "APPLIED"
    assert payments.handle_webhook(db, "dwolla", body, good)["outcome"] == "DUPLICATE"
    db.expire_all()
    assert db.get(ScheduledPayment, sp.id).status == "PAID"


def test_dwolla_refuses_inr_and_bad_credentials(db, make_org, now, dwolla, monkeypatch):
    from finguard.core.config import get_settings
    from finguard.core.errors import ValidationFailed
    from finguard.payments.providers import get_payout_provider
    from finguard.payments.providers.base import PayoutRequest, ProviderError, ProviderRejected

    with pytest.raises(ProviderRejected, match="USD only"):
        dwolla.create_payout(PayoutRequest(idempotency_key=uuid.uuid4().hex, reference_id="x", fund_account_id="x",
                                           amount_minor=100, currency="INR"))
    with pytest.raises(ValidationFailed, match="routing"):          # an IFSC is not a US routing number
        onboard(db, make_org(), currency="USD", account=_acct("0000"))
    db.rollback()
    monkeypatch.setattr(get_settings(), "dwolla_secret", "wrong-secret")
    get_payout_provider.cache_clear()
    with pytest.raises(ProviderError, match="credentials"):
        get_payout_provider().register_beneficiary(name="A B", email="a@example.com", contractor_ref="x",
                                                   account_number="123456789", ifsc=ROUTING)
