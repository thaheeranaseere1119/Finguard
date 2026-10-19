"""Redaction of secrets and banking identifiers before anything is logged or written to the audit trail."""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*"), r"\1 [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(rzp_(?:test|live)_[A-Za-z0-9]+)"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)(password|secret|api[_-]?key|token|authorization)(\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1\2[REDACTED]"),
    # Long digit runs: bank account / card numbers. Keep the last 4 digits for traceability.
    (re.compile(r"\b(\d{5,14})(\d{4})\b"), r"XXXX\2"),
)

_SENSITIVE_KEYS = {"password", "secret", "token", "access_token", "refresh_token", "api_key", "authorization",
                   "account_number", "card_number", "cvv", "key_secret", "webhook_secret", "password_hash"}


def redact_text(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact(obj: Any, _depth: int = 0) -> Any:
    if _depth > 8:
        return "[TRUNCATED]"
    if isinstance(obj, str):
        return redact_text(obj)[:4000]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in list(obj)[:200]]
    return obj
