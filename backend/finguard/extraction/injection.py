"""Deterministic prompt-injection detector (ported from Finguard-v3, adapted to contract documents).

Uploaded documents are UNTRUSTED DATA. This scores text for instruction-like content addressed to an AI system:

    score = 1 − Π (1 − w_family)   over pattern families with at least one match

Detection never causes an embedded instruction to be followed; it raises risk and forces compliance review.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from decimal import Decimal

_FLAGS = re.IGNORECASE | re.DOTALL


def _p(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, _FLAGS) for p in patterns)


FAMILIES: tuple[tuple[str, Decimal, tuple[re.Pattern[str], ...]], ...] = (
    ("instruction_override", Decimal("0.60"), _p(
        r"\b(ignore|disregard|forget|override)\b.{0,30}\b(all|any|previous|prior|above|earlier|your|the)\b.{0,20}\b(instructions?|prompts?|rules|directions|guidelines|policies)",
        r"\bnew (system )?instructions?\s*[:\-]", r"\b(system|developer) prompt\b", r"\byou are now\b",
        r"\bdeveloper mode\b", r"\bjailbreak\b", r"\bact as (an? )?(admin|administrator|system|root)\b")),
    ("role_marker", Decimal("0.50"), _p(
        r"<\|?im_start\|?>", r"\[/?INST\]", r"^\s*(system|assistant)\s*:", r"<\s*/?\s*(system|assistant|tool_call)\s*>")),
    ("approval_bypass", Decimal("0.55"), _p(
        r"\b(do not|don't|never|without)\b.{0,20}\b(ask|seek|request|require|wait for|need)\w*\b.{0,20}\b(approval|confirmation|review|human|permission)",
        r"\b(pre-?approved|approval (is )?not (needed|required))\b",
        r"\bskip\b.{0,20}\b(approval|review|verification|checks?|controls?)\b",
        r"\bbypass\b.{0,20}\b(approval|review|verification|checks?|controls?|policy|policies)\b",
        r"\bauthori[sz]ed by (the )?(ceo|cfo|board|admin|administrator)\b")),
    ("agent_directed_action", Decimal("0.45"), _p(
        r"\b(ai|assistant|agent|model|llm|bot|system)\b.{0,40}\b(must|should|shall|will|needs? to)\b.{0,40}\b(transfer|wire|pay|release|send|delete|approve|execute|increase|change)",
        r"\b(call|invoke|execute|run)\s+(the\s+)?[a-z]+_[a-z_]+\b", r"\bfunction_call\b|\btool_calls?\b")),
    ("exfiltration", Decimal("0.50"), _p(
        r"\b(send|email|upload|post|forward|reveal|print|exfiltrate|leak)\b.{0,40}\b(passwords?|credentials?|api keys?|tokens?|secrets?|system prompt|environment variables)\b")),
    ("policy_tamper", Decimal("0.45"), _p(
        r"\b(set|change|raise|update)\b.{0,30}\b(risk score|threshold|approval limit|policy)\b.{0,30}\b(to|=)\b",
        r"\brisk\s*(score)?\s*(=|is|to)\s*0\b")),
)

_ZERO_WIDTH = {"​", "‌", "‍", "⁠", "﻿"}
_B64 = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


@dataclass(frozen=True)
class InjectionReport:
    score: Decimal
    families: tuple[str, ...]
    snippets: tuple[dict, ...] = field(default_factory=tuple)
    obfuscation: tuple[str, ...] = ()

    @property
    def detected(self) -> bool:
        return self.score >= Decimal("0.40")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["score"] = str(self.score)
        d["detected"] = self.detected
        return d


def scan_text(text: str) -> InjectionReport:
    obf: list[str] = []
    if any(ch in _ZERO_WIDTH for ch in text):
        obf.append("zero_width_characters")
    cleaned = unicodedata.normalize("NFKC", "".join(ch for ch in text if ch not in _ZERO_WIDTH))
    corpora = [cleaned]
    for m in _B64.finditer(cleaned):
        try:
            decoded = base64.b64decode(m.group(0), validate=True).decode("utf-8")
            if decoded.isprintable():
                corpora.append(decoded)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
    families, snippets = [], []
    for name, _w, pats in FAMILIES:
        for corpus in corpora:
            hit = next((m for p in pats for m in [p.search(corpus)] if m), None)
            if hit:
                families.append(name)
                s = max(0, hit.start() - 30)
                snippets.append({"family": name, "snippet": corpus[s:hit.end() + 30].replace("\n", " ")[:200]})
                break
    if len(corpora) > 1 and families:
        obf.append("base64_encoded_instructions")
    survival = Decimal(1)
    weights = {n: w for n, w, _ in FAMILIES}
    for f in families:
        survival *= 1 - weights[f]
    for _ in obf:
        survival *= Decimal("0.9")
    score = (1 - survival).quantize(Decimal("0.0001")) if (families or obf) else Decimal(0)
    return InjectionReport(score, tuple(families), tuple(snippets), tuple(obf))
