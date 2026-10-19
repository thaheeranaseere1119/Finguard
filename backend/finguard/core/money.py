"""Decimal money helpers. Floating point is never used for monetary values."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

CENT = Decimal("0.01")

# ISO-4217 minor units for the currencies the platform accepts.
CURRENCY_MINOR_UNITS = {"INR": 2, "USD": 2, "EUR": 2, "GBP": 2, "SGD": 2, "AED": 2, "JPY": 0}


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("float is not accepted for money; pass str or Decimal")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a decimal amount: {value!r}") from exc


def quantize_money(amount: Decimal, currency: str = "INR") -> Decimal:
    places = CURRENCY_MINOR_UNITS.get(currency, 2)
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def to_minor_units(amount: Decimal, currency: str) -> int:
    places = CURRENCY_MINOR_UNITS.get(currency, 2)
    q = quantize_money(amount, currency)
    return int(q * (10 ** places))


def from_minor_units(value: int, currency: str) -> Decimal:
    places = CURRENCY_MINOR_UNITS.get(currency, 2)
    return quantize_money(Decimal(value) / (10 ** places), currency)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return obj


def to_jsonable(obj: Any) -> Any:
    return _jsonable(obj)


def canonical_json(obj: Any) -> str:
    return json.dumps(_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
