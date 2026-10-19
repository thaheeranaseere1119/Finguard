"""Six-factor risk model — the single authoritative risk scorer for the platform.

    Risk = 3·S + 5·F + 3·I + 3·E + 3·T + 3·U        each factor ∈ [0, 5]  →  Risk ∈ [0, 100]

S data sensitivity · F financial impact · I irreversibility · E evidence risk · T tool risk · U model uncertainty.
Weights are prototype research parameters (financial impact weighted higher), not validated production values.
If a policy overrides the weights, the score is normalized so the maximum stays 100.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

FACTOR_KEYS = ("S", "F", "I", "E", "T", "U")
FACTOR_NAMES = {
    "S": "Data sensitivity", "F": "Financial impact", "I": "Irreversibility",
    "E": "Evidence risk", "T": "Tool risk", "U": "Model uncertainty",
}
DEFAULT_WEIGHTS: dict[str, Decimal] = {"S": Decimal(3), "F": Decimal(5), "I": Decimal(3), "E": Decimal(3),
                                       "T": Decimal(3), "U": Decimal(3)}
FACTOR_MAX = Decimal(5)
_Q = Decimal("0.01")


def clamp_factor(v: Decimal | int | str) -> Decimal:
    d = Decimal(str(v))
    return max(Decimal(0), min(FACTOR_MAX, d)).quantize(_Q, rounding=ROUND_HALF_UP)


@dataclass
class Factor:
    key: str
    value: Decimal
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": FACTOR_NAMES[self.key], "value": str(self.value), "rationale": self.rationale}


@dataclass
class RiskVector:
    S: Factor
    F: Factor
    I: Factor  # noqa: E741 - factor letter from the research model
    E: Factor
    T: Factor
    U: Factor
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)

    def factors(self) -> list[Factor]:
        return [self.S, self.F, self.I, self.E, self.T, self.U]

    def values(self) -> dict[str, Decimal]:
        return {f.key: f.value for f in self.factors()}


def risk_score(vector: RiskVector, weights: dict[str, Any] | None = None) -> Decimal:
    w = {k: Decimal(str(v)) for k, v in (weights or DEFAULT_WEIGHTS).items()}
    total_w = sum(w[k] for k in FACTOR_KEYS)
    if total_w <= 0:
        raise ValueError("risk weights must be positive")
    raw = sum(w[k] * vector.values()[k] for k in FACTOR_KEYS)
    score = raw * Decimal(100) / (FACTOR_MAX * total_w)
    return score.quantize(_Q, rounding=ROUND_HALF_UP)


def factor_contributions(vector: RiskVector, weights: dict[str, Any] | None = None) -> dict[str, str]:
    w = {k: Decimal(str(v)) for k, v in (weights or DEFAULT_WEIGHTS).items()}
    total_w = sum(w[k] for k in FACTOR_KEYS)
    return {k: str((w[k] * vector.values()[k] * Decimal(100) / (FACTOR_MAX * total_w)).quantize(_Q)) for k in FACTOR_KEYS}


def make_factor(key: str, value: Decimal | int | str, *rationale: str) -> Factor:
    return Factor(key, clamp_factor(value), [r for r in rationale if r])


def band(amount: Decimal, bands: list[list[str]]) -> tuple[int, str]:
    """Map an amount to a 0–5 level using ascending [upper_bound, level] bands; above the last bound → 5."""
    for upper, level in bands:
        if amount <= Decimal(str(upper)):
            return int(level), f"≤ {upper}"
    return 5, f"> {bands[-1][0]}"
