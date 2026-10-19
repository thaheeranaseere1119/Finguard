"""Dwolla payouts adapter (US ACH, USD only) — runs against the Dwolla SANDBOX by default.

API (https://developers.dwolla.com, HAL+JSON, Accept: application/vnd.dwolla.v1.hal+json):
  POST /token (client credentials, Basic key:secret)        → bearer token, cached until shortly before expiry
  POST /customers  {type: receive-only}                       → payee (the contractor)
  POST /customers/{id}/funding-sources {routingNumber, …}     → payee bank account  (= our fund_account_id)
  POST /transfers  header Idempotency-Key, correlationId      → 201 + Location (transfer id)
      (verified in the sandbox: same key + identical body → same transfer; same key + DIFFERENT body → a NEW transfer,
       so a request must be rebuilt byte-identically from the persisted attempt — which execute_payout does)
  GET  /transfers/{id} · GET /transfers/{id}/failure · GET /accounts/{id}/transfers?correlationId=…
  POST /transfers/{id} {status: cancelled}
  Webhooks: X-Request-Signature-SHA-256 = hex HMAC-SHA256(raw body, webhook secret); the event carries only a
  resource link, so the transfer is re-read from Dwolla (authoritative) before anything is applied.
Sandbox: transfers stay "pending" until POST /sandbox-simulations; a payee funding source NAMED after an ACH return
code (e.g. "R01") makes its transfers fail. For demos, bank accounts ending 1111/3333 get the names R01/R03.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from decimal import Decimal

import httpx

from finguard.core.config import get_settings
from finguard.payments.providers.base import (PayoutProvider, PayoutRequest, PayoutResult, ProviderNotSent,
                                              ProviderOutcomeUnknown, ProviderRejected, WebhookEvent)

SANDBOX_URL, LIVE_URL = "https://api-sandbox.dwolla.com", "https://api.dwolla.com"
_STATUS = {"pending": "PENDING", "processed": "PROCESSED", "failed": "FAILED", "cancelled": "CANCELLED"}
SANDBOX_FAILURE_NAMES = {"1111": "R01", "3333": "R03"}      # insufficient funds · no account / unable to locate
ABA_RE = re.compile(r"^\d{9}$")


def aba_valid(routing: str) -> bool:
    """US ABA routing number checksum (3-7-1 weights)."""
    if not ABA_RE.match(routing or ""):
        return False
    d = [int(x) for x in routing]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def normalize_status(raw: str | None) -> str:
    return _STATUS.get((raw or "").lower(), "PENDING")


def _id(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


class DwollaProvider(PayoutProvider):
    name = "dwolla"
    bank_code_kind = "ABA"              # beneficiaries need a US routing number instead of an IFSC
    currencies = ("USD",)
    transport: httpx.BaseTransport | None = None   # test seam (offline contract tests)

    def __init__(self) -> None:
        s = get_settings()
        missing = [n for n, v in (("DWOLLA_KEY", s.dwolla_key), ("DWOLLA_SECRET", s.dwolla_secret)) if not v]
        if missing:
            raise RuntimeError(f"Dwolla provider selected but not configured: {', '.join(missing)}")
        self.mode = s.payout_mode
        self._base = (s.dwolla_base_url or (LIVE_URL if self.mode == "live" else SANDBOX_URL)).rstrip("/")
        if self.mode == "sandbox" and self._base != SANDBOX_URL:
            raise RuntimeError("Refusing a non-sandbox Dwolla URL while PAYOUT_MODE=sandbox")
        if self.mode == "live" and self._base != LIVE_URL:
            raise RuntimeError("PAYOUT_MODE=live requires the production Dwolla URL")
        self._key, self._secret = s.dwolla_key, s.dwolla_secret
        self._webhook_secret = s.dwolla_webhook_secret
        self._source_cfg = s.dwolla_funding_source
        self._timeout = httpx.Timeout(s.payout_timeout_seconds, connect=5.0)
        self._token: tuple[str, float] | None = None
        self._account: str | None = None
        self._source: str | None = None

    # ------------------------------------------------------------------ HTTP
    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self._timeout, follow_redirects=False, transport=self.transport)

    def _bearer(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        try:
            with self._client() as c:
                r = c.post(f"{self._base}/token", auth=(self._key, self._secret), data={"grant_type": "client_credentials"})
        except httpx.HTTPError as exc:
            raise ProviderNotSent(f"could not obtain a Dwolla token: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise ProviderRejected("auth_failed", f"Dwolla rejected the API credentials (HTTP {r.status_code})")
        d = r.json()
        self._token = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
        return self._token[0]

    def _call(self, method: str, path_or_url: str, *, json_body: dict | None = None, params: dict | None = None,
              headers: dict | None = None, may_create: bool = False) -> httpx.Response:
        url = path_or_url if path_or_url.startswith("http") else f"{self._base}{path_or_url}"
        if not url.startswith(self._base):
            raise ProviderRejected("bad_url", "refusing to call a URL outside the configured Dwolla environment")
        h = {"Accept": "application/vnd.dwolla.v1.hal+json", "Authorization": f"Bearer {self._bearer()}", **(headers or {})}
        if json_body is not None:
            h["Content-Type"] = "application/vnd.dwolla.v1.hal+json"
        try:
            with self._client() as c:
                r = c.request(method, url, content=json.dumps(json_body) if json_body is not None else None,
                              params=params, headers=h)
        except httpx.ConnectError as exc:
            raise ProviderNotSent("could not connect to Dwolla") from exc
        except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            if may_create:
                raise ProviderOutcomeUnknown(f"Dwolla call interrupted: {type(exc).__name__}") from exc
            raise ProviderNotSent(f"Dwolla call interrupted: {type(exc).__name__}") from exc
        if r.status_code == 401:                      # token expired early → one refresh
            self._token = None
            return self._call(method, path_or_url, json_body=json_body, params=params, headers=headers, may_create=may_create)
        if r.status_code >= 500:
            if may_create:
                raise ProviderOutcomeUnknown(f"Dwolla HTTP {r.status_code}")
            raise ProviderNotSent(f"Dwolla HTTP {r.status_code}")
        return r

    @staticmethod
    def _error(r: httpx.Response) -> ProviderRejected:
        try:
            d = r.json()
        except ValueError:
            return ProviderRejected(str(r.status_code), "Dwolla rejected the request")
        errs = ((d.get("_embedded") or {}).get("errors")) or []
        detail = "; ".join(f"{e.get('path', '')} {e.get('message', '')}".strip() for e in errs) or d.get("message", "")
        return ProviderRejected(str(d.get("code", r.status_code)), detail or "Dwolla rejected the request")

    @staticmethod
    def _duplicate_link(r: httpx.Response) -> str | None:
        try:
            d = r.json()
        except ValueError:
            return None
        for e in ((d.get("_embedded") or {}).get("errors") or []) + [d]:
            href = ((e.get("_links") or {}).get("about") or {}).get("href")
            if href and str(e.get("code", "")).lower() in ("duplicate", "duplicateresource"):
                return href
        return None

    # ------------------------------------------------------------------ account & source of funds
    def account_href(self) -> str:
        if not self._account:
            r = self._call("GET", "/")
            self._account = r.json()["_links"]["account"]["href"]
        return self._account

    def source_href(self) -> str:
        """Funding source payouts are sent FROM: DWOLLA_FUNDING_SOURCE, else the account's verified bank, else Balance."""
        if self._source:
            return self._source
        if self._source_cfg:
            self._source = self._source_cfg if self._source_cfg.startswith("http") else f"{self._base}/funding-sources/{self._source_cfg}"
            return self._source
        r = self._call("GET", f"{self.account_href()}/funding-sources", params={"removed": "false"})
        fss = (r.json().get("_embedded") or {}).get("funding-sources") or []
        pick = next((f for f in fss if f["type"] == "bank" and f["status"] == "verified"), None) or \
            next((f for f in fss if f["type"] == "balance"), None)
        if not pick:
            raise ProviderRejected("no_funding_source", "the Dwolla account has no verified funding source to pay from")
        self._source = pick["_links"]["self"]["href"]
        return self._source

    # ------------------------------------------------------------------ PayoutProvider
    def register_beneficiary(self, *, name, email, contractor_ref, account_number, ifsc):
        routing = ifsc
        if not aba_valid(routing):
            raise ProviderRejected("invalid_routing_number", "a valid 9-digit US ABA routing number is required")
        first, _, last = name.strip().partition(" ")
        r = self._call("POST", "/customers", json_body={"firstName": first[:50], "lastName": (last or first)[:50],
                                                        "email": email, "type": "receive-only",
                                                        "correlationId": contractor_ref[:255]})
        if r.status_code == 201:
            customer = r.headers["location"]
        elif (dup := self._duplicate_link(r)) is not None:
            customer = dup                            # same e-mail already registered as a Dwolla customer
        else:
            raise self._error(r)
        fs_name = (SANDBOX_FAILURE_NAMES.get(account_number[-4:]) if self.mode == "sandbox" else None) or f"{name[:40]} checking"
        r = self._call("POST", f"{customer}/funding-sources", json_body={
            "routingNumber": routing, "accountNumber": account_number, "bankAccountType": "checking", "name": fs_name})
        if r.status_code == 201:
            fs = r.headers["location"]
        elif (dup := self._duplicate_link(r)) is not None:
            fs = dup
        else:
            raise self._error(r)
        return _id(customer), _id(fs)

    def _result(self, d: dict, reference: str | None = None) -> PayoutResult:
        amount = d.get("amount") or {}
        cents = int((Decimal(str(amount["value"])) * 100).to_integral_value()) if amount.get("value") is not None else None
        status = normalize_status(d.get("status"))
        code = reason = None
        if status == "FAILED" and (d.get("_links") or {}).get("failure"):
            fr = self._call("GET", d["_links"]["failure"]["href"])
            if fr.status_code == 200:
                code, reason = fr.json().get("code"), fr.json().get("description")
        return PayoutResult(d.get("id"), str(d.get("status")), status, cents, amount.get("currency"),
                            d.get("correlationId") or reference, code, reason,
                            {k: d.get(k) for k in ("id", "status", "created", "correlationId", "individualAchId")}
                            | {"amount": amount})

    def create_payout(self, req: PayoutRequest) -> PayoutResult:
        if req.currency not in self.currencies:
            raise ProviderRejected("unsupported_currency", f"Dwolla pays out in USD only (payment is {req.currency})")
        body = {"_links": {"source": {"href": self.source_href()},
                           "destination": {"href": f"{self._base}/funding-sources/{req.fund_account_id}"}},
                "amount": {"currency": "USD", "value": f"{Decimal(req.amount_minor) / 100:.2f}"},
                "correlationId": req.reference_id[:255],
                "metadata": {k[:40]: str(v)[:255] for k, v in list(req.notes.items())[:10]}}
        r = self._call("POST", "/transfers", json_body=body, headers={"Idempotency-Key": req.idempotency_key[:255]},
                       may_create=True)
        if r.status_code != 201:
            raise self._error(r)
        return self.get_payout_status(_id(r.headers["location"]))

    def get_payout_status(self, provider_payout_id: str) -> PayoutResult:
        r = self._call("GET", f"/transfers/{provider_payout_id}")
        if r.status_code != 200:
            raise ProviderNotSent(f"could not read Dwolla transfer {provider_payout_id} (HTTP {r.status_code})")
        return self._result(r.json())

    def find_by_reference(self, reference_id: str) -> PayoutResult | None:
        r = self._call("GET", f"{self.account_href()}/transfers", params={"correlationId": reference_id[:255]})
        if r.status_code != 200:
            raise ProviderNotSent(f"Dwolla transfer search failed (HTTP {r.status_code})")
        items = (r.json().get("_embedded") or {}).get("transfers") or []
        own = self.account_href().rstrip("/").rsplit("/", 1)[-1]
        # A bank-funded payout is two transfers sharing the correlationId: the funding leg (our bank → our Dwolla
        # account, carries a "funded-transfer" link) and the payout itself (→ the payee). Only the payout counts.
        mine = [t for t in items if t.get("correlationId") == reference_id[:255]]
        payouts = [t for t in mine if "funded-transfer" not in (t.get("_links") or {})
                   and _id(((t.get("_links") or {}).get("destination") or {}).get("href", "")) != own]
        if payouts:
            return self._result(payouts[0], reference_id)
        # Dwolla's search can lag a few seconds behind; if only the funding leg is visible, follow it to its payout
        # (treating the payout as "never created" here could otherwise allow a duplicate retry).
        for leg in mine:
            href = ((leg.get("_links") or {}).get("funded-transfer") or {}).get("href")
            if href:
                return self.get_payout_status(_id(href))
        # Verified in the sandbox: a just-created transfer can be missing from search for several seconds, so
        # "not found" is NOT proof that nothing was created. Defer (payment stays UNKNOWN, no retry) instead of None.
        raise ProviderNotSent("Dwolla search shows no transfer for this reference yet; reconciliation will retry")

    def cancel_payout(self, provider_payout_id: str) -> PayoutResult:
        r = self._call("POST", f"/transfers/{provider_payout_id}", json_body={"status": "cancelled"})
        if r.status_code != 200:
            raise self._error(r)
        return self._result(r.json())

    def verify_webhook(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        sig = headers.get("x-request-signature-sha-256", "")
        if not (self._webhook_secret and sig):
            return False
        expected = hmac.new(self._webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> WebhookEvent:
        data = json.loads(raw_body)
        topic, event_id = str(data.get("topic", "")), str(data.get("id", ""))
        if not event_id:
            raise ValueError("missing event id")
        transfer_id = None
        if "transfer" in topic:
            transfer_id = data.get("resourceId") or _id(((data.get("_links") or {}).get("resource") or {}).get("href", ""))
        if not transfer_id:
            return WebhookEvent(event_id, topic, None, None, None, None, None, None, data)
        t = self.get_payout_status(transfer_id)       # never trust the event body for status/amount
        return WebhookEvent(event_id, topic, transfer_id, t.status, t.amount_minor, t.currency, t.reference_id,
                            t.failure_reason, data)

    # ------------------------------------------------------------------ sandbox helpers
    def simulate_processing(self) -> dict:
        """SANDBOX ONLY: ask Dwolla to process pending bank transfers (what the ACH network does in real life)."""
        if self.mode != "sandbox":
            raise RuntimeError("sandbox simulations are only available in sandbox mode")
        r = self._call("POST", "/sandbox-simulations", json_body={})
        return r.json() if r.content else {}

    def describe(self) -> dict:
        return {"name": self.name, "mode": self.mode, "base_url": self._base, "currencies": list(self.currencies)}
