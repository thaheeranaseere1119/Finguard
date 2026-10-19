"""Deterministic parsers for contract values. Used both by the rule extractor and to validate LLM output and
human corrections, so every stored value has passed the same checks."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
                 "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
                 "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
                 "twentieth": 20, "twenty-first": 21, "twenty-second": 22, "twenty-third": 23, "twenty-fourth": 24,
                 "twenty-fifth": 25, "twenty-sixth": 26, "twenty-seventh": 27, "twenty-eighth": 28}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "eighteen": 18, "twenty": 20, "twenty-four": 24,
                "thirty": 30, "thirty-six": 36, "forty-five": 45, "sixty": 60, "ninety": 90}

CURRENCY_SYMBOLS = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupees": "INR", "$": "USD", "usd": "USD",
                    "us$": "USD", "dollars": "USD", "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
                    "£": "GBP", "gbp": "GBP", "pounds": "GBP", "sgd": "SGD", "aed": "AED", "jpy": "JPY", "¥": "JPY"}

_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"  # grouped form first, then plain digits
AMOUNT_RE = re.compile(
    rf"(?P<pre>₹|(?<![A-Za-z])(?:rs\.?|inr|us\$|usd|eur|gbp|sgd|aed|jpy)|\$|€|£|¥)\s?(?P<num1>{_NUM})(?:\s?(?P<scale1>lakhs?|lacs?|crores?|k)\b)?"
    rf"|(?P<num2>{_NUM})\s?(?P<scale2>lakhs?|lacs?|crores?)?\s?(?P<post>inr|rupees|usd|dollars|eur|euros?|gbp|pounds|sgd|aed)\b",
    re.IGNORECASE)


class ParseError(ValueError):
    pass


def parse_amount_text(raw: str) -> Decimal:
    cleaned = raw.replace(",", "").replace(" ", "")
    # Common OCR confusions inside digit groups: O→0, l/I→1, S→5 (only when surrounded by digits).
    cleaned = re.sub(r"(?<=\d)[Oo](?=\d|$)", "0", cleaned)
    cleaned = re.sub(r"(?<=\d)[lI](?=\d|$)", "1", cleaned)
    try:
        d = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ParseError(f"not an amount: {raw!r}") from exc
    if d <= 0 or d > Decimal("1e13"):
        raise ParseError(f"amount out of range: {raw!r}")
    return d.quantize(Decimal("0.01"))


def _scale(word: str | None) -> Decimal:
    if not word:
        return Decimal(1)
    w = word.lower()
    if w.startswith(("lakh", "lac")):
        return Decimal(100000)
    if w.startswith("crore"):
        return Decimal(10000000)
    if w == "k":
        return Decimal(1000)
    return Decimal(1)


def find_amounts(text: str) -> list[tuple[int, int, Decimal, str]]:
    """Return (start, end, amount, currency) for every currency-qualified amount."""
    out = []
    for m in AMOUNT_RE.finditer(text):
        sym = (m.group("pre") or m.group("post") or "").lower()
        cur = CURRENCY_SYMBOLS.get(sym)
        num = m.group("num1") or m.group("num2")
        if not cur or not num:
            continue
        try:
            amt = parse_amount_text(num) * _scale(m.group("scale1") or m.group("scale2"))
        except ParseError:
            continue
        out.append((m.start(), m.end(), amt.quantize(Decimal("0.01")), cur))
    return out


def normalize_currency(value: str) -> str:
    v = value.strip().lower()
    if v.upper() in {"INR", "USD", "EUR", "GBP", "SGD", "AED", "JPY"}:
        return v.upper()
    if v in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[v]
    raise ParseError(f"unknown currency {value!r}")


# OCR tolerance: a stray punctuation mark after the day ("31: March 2027") is accepted.
_D_MONTH_Y = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[.:,'’]?\s+(?:day\s+of\s+)?([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MONTH_D_Y = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")


def find_dates(text: str, day_first: bool = True) -> list[tuple[int, int, date, bool]]:
    """Return (start, end, date, ambiguous). Numeric dd/mm vs mm/dd is resolved by policy (day_first) and flagged."""
    found: list[tuple[int, int, date, bool]] = []
    for m in _ISO.finditer(text):
        try:
            found.append((m.start(), m.end(), date(int(m[1]), int(m[2]), int(m[3])), False))
        except ValueError:
            pass
    for m in _D_MONTH_Y.finditer(text):
        mon = MONTHS.get(m[2].lower())
        if mon:
            try:
                found.append((m.start(), m.end(), date(int(m[3]), mon, int(m[1])), False))
            except ValueError:
                pass
    for m in _MONTH_D_Y.finditer(text):
        mon = MONTHS.get(m[1].lower())
        if mon:
            try:
                found.append((m.start(), m.end(), date(int(m[3]), mon, int(m[2])), False))
            except ValueError:
                pass
    for m in _NUMERIC.finditer(text):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        ambiguous = a <= 12 and b <= 12 and a != b
        d, mo = (a, b) if day_first else (b, a)
        if mo > 12:
            d, mo = mo, d
            ambiguous = False
        try:
            found.append((m.start(), m.end(), date(y, mo, d), ambiguous))
        except ValueError:
            pass
    found.sort(key=lambda t: t[0])
    # Drop overlaps (keep first/longest).
    dedup: list[tuple[int, int, date, bool]] = []
    for f in found:
        if dedup and f[0] < dedup[-1][1]:
            continue
        dedup.append(f)
    return dedup


def parse_date(value: str, day_first: bool = True) -> date:
    hits = find_dates(value, day_first)
    if not hits:
        raise ParseError(f"not a date: {value!r}")
    return hits[0][2]


FREQ_PATTERNS: list[tuple[re.Pattern[str], tuple[str, int, str | None]]] = [
    (re.compile(r"\b(fortnightly|bi-?weekly|every\s+(?:two|2)\s+weeks)\b", re.I), ("CUSTOM", 2, "WEEKS")),
    (re.compile(r"\b(weekly|per\s+week|every\s+week|each\s+week|every\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.I), ("WEEKLY", 1, None)),
    (re.compile(r"\b(quarterly|per\s+quarter|every\s+quarter|each\s+quarter|every\s+(?:three|3)\s+months)\b", re.I), ("QUARTERLY", 1, None)),
    (re.compile(r"\b(half[- ]yearly|semi[- ]annual(?:ly)?|every\s+(?:six|6)\s+months)\b", re.I), ("CUSTOM", 6, "MONTHS")),
    (re.compile(r"\b(annually|per\s+annum|yearly|per\s+year|every\s+year|each\s+year)\b", re.I), ("ANNUALLY", 1, None)),
    (re.compile(r"\b(monthly|per\s+month|every\s+month|each\s+(?:calendar\s+)?month|a\s+month|/\s?month|p\.?m\.?)\b|of\s+(?:each|every)\s+(?:calendar\s+)?month", re.I), ("MONTHLY", 1, None)),
]


def detect_frequency(text: str) -> tuple[str, int, str | None] | None:
    for pat, val in FREQ_PATTERNS:
        if pat.search(text):
            return val
    return None


def normalize_frequency(value: str) -> str:
    v = value.strip().upper()
    if v in {"WEEKLY", "MONTHLY", "QUARTERLY", "ANNUALLY", "CUSTOM"}:
        return v
    det = detect_frequency(value)
    if det:
        return det[0]
    raise ParseError(f"unknown frequency {value!r}")


_DUE_DOM = re.compile(
    r"(?:on|by|before)\s+(?:or\s+before\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)?\s*(?:day\s+)?(?:of\s+)?(?:each|every|the|every\s+calendar|each\s+calendar)?\s*(?:calendar\s+)?(?:month)?"
    r"|(\d{1,2})(?:st|nd|rd|th)\s+(?:day\s+)?of\s+(?:each|every|the)\s+(?:calendar\s+)?month"
    r"|the\s+(" + "|".join(ORDINAL_WORDS) + r")\s+(?:\(\d+(?:st|nd|rd|th)?\)\s+)?day\s+of\s+(?:each|every|the)\s+(?:calendar\s+)?month",
    re.I)
_DUE_LAST = re.compile(r"\blast\s+(?:business\s+|working\s+)?day\s+of\s+(?:each|every|the)\s+(?:calendar\s+)?month\b", re.I)
_DUE_WEEKDAY = re.compile(r"\b(?:every|each|on)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.I)


def detect_due_rule(text: str) -> tuple[str, int, int, int] | None:
    """Return ("DOM"|"WEEKDAY", value, start, end)."""
    if m := _DUE_LAST.search(text):
        return ("DOM", 31, m.start(), m.end())
    if m := _DUE_DOM.search(text):
        raw = m.group(1) or m.group(2)
        val = int(raw) if raw else ORDINAL_WORDS[m.group(3).lower()]
        if 1 <= val <= 31:
            return ("DOM", val, m.start(), m.end())
    if m := _DUE_WEEKDAY.search(text):
        return ("WEEKDAY", WEEKDAYS[m.group(1).lower()], m.start(), m.end())
    return None


_DURATION = re.compile(r"(?:period|term)\s+of\s+(\d+|" + "|".join(NUMBER_WORDS) + r")\s*(?:\(\d+\)\s*)?(months?|years?)", re.I)


def detect_duration(text: str) -> tuple[relativedelta, int, int] | None:
    if m := _DURATION.search(text):
        raw = m.group(1).lower()
        n = int(raw) if raw.isdigit() else NUMBER_WORDS[raw]
        delta = relativedelta(months=n) if m.group(2).lower().startswith("month") else relativedelta(years=n)
        return delta, m.start(), m.end()
    return None


def parse_int(value: str, lo: int, hi: int) -> int:
    try:
        v = int(str(value).strip())
    except ValueError as exc:
        raise ParseError(f"not an integer: {value!r}") from exc
    if not lo <= v <= hi:
        raise ParseError(f"value {v} outside {lo}..{hi}")
    return v


def parse_bool(value: str) -> bool:
    v = str(value).strip().lower()
    if v in {"true", "yes", "1", "y"}:
        return True
    if v in {"false", "no", "0", "n"}:
        return False
    raise ParseError(f"not a boolean: {value!r}")
