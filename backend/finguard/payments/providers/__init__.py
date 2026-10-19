from __future__ import annotations

from functools import lru_cache

from finguard.core.config import get_settings
from finguard.payments.providers.base import PayoutProvider


@lru_cache
def get_payout_provider() -> PayoutProvider:
    s = get_settings()
    if s.payout_provider == "dwolla":
        from finguard.payments.providers.dwolla import DwollaProvider

        return DwollaProvider()
    if s.payout_provider == "razorpayx":
        from finguard.payments.providers.razorpayx import RazorpayXProvider

        return RazorpayXProvider()
    from finguard.payments.providers.mock import MockSandboxPaymentProvider

    return MockSandboxPaymentProvider()


def get_provider_by_name(name: str) -> PayoutProvider:
    p = get_payout_provider()
    if p.name != name:
        raise LookupError(f"provider {name} is not the configured provider")
    return p
