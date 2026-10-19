"""Contract extraction schema: field names, kinds, criticality and validation (deterministic)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from finguard.extraction import parsers as P


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str          # amount | currency | date | frequency | int | bool | text | enum
    critical: bool = False
    choices: tuple[str, ...] = ()


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("contract_id", "Contract ID", "text"),
    FieldSpec("contract_title", "Contract title", "text"),
    FieldSpec("contractor_legal_name", "Contractor legal name", "text", critical=True),
    FieldSpec("counterparty_name", "Company / client", "text"),
    FieldSpec("parties", "Parties", "text"),
    FieldSpec("start_date", "Effective / start date", "date", critical=True),
    FieldSpec("end_date", "End / expiry date", "date", critical=True),
    FieldSpec("total_value", "Total contract value", "amount"),
    FieldSpec("payment_amount", "Payment amount per period", "amount", critical=True),
    FieldSpec("currency", "Currency", "currency", critical=True),
    FieldSpec("frequency", "Payment frequency", "frequency", critical=True),
    FieldSpec("interval_count", "Frequency interval", "int"),
    FieldSpec("custom_unit", "Custom frequency unit", "enum", choices=("DAYS", "WEEKS", "MONTHS")),
    FieldSpec("due_day_of_month", "Due day of month", "int"),
    FieldSpec("due_weekday", "Due weekday (0=Mon)", "int"),
    FieldSpec("first_payment_date", "First payment date", "date"),
    FieldSpec("payment_timing", "Advance / arrears", "enum", choices=("ADVANCE", "ARREARS")),
    FieldSpec("grace_period_days", "Grace period (days)", "int"),
    FieldSpec("proration_rule", "Proration", "enum", choices=("NONE", "DAILY")),
    FieldSpec("tax_withholding", "Tax / withholding / deductions", "text"),
    FieldSpec("invoice_required", "Invoice required", "bool"),
    FieldSpec("deliverables", "Deliverables / acceptance", "text"),
    FieldSpec("renewal_terms", "Renewal terms", "text"),
    FieldSpec("termination_terms", "Termination terms", "text"),
    FieldSpec("notice_period_days", "Notice period (days)", "int"),
    FieldSpec("bank_details_present", "Bank/payment instructions present", "bool"),
    FieldSpec("signatories", "Signatories", "text"),
    FieldSpec("signature_present", "Signature detected", "bool"),
    FieldSpec("amendment_clause", "Amendment clause", "text"),
    FieldSpec("auto_execute_permitted", "Automatic recurring payment permitted", "bool"),
)
FIELD_MAP = {f.name: f for f in FIELDS}
CRITICAL = [f.name for f in FIELDS if f.critical]


def normalize_value(name: str, raw: Any) -> str | None:
    """Validate and normalize a value for a field. Raises ParseError if invalid. None stays None."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    spec = FIELD_MAP[name]
    s = str(raw).strip()
    if spec.kind == "amount":
        return str(P.parse_amount_text(s))
    if spec.kind == "currency":
        return P.normalize_currency(s)
    if spec.kind == "date":
        if isinstance(raw, date):
            return raw.isoformat()
        return P.parse_date(s).isoformat()
    if spec.kind == "frequency":
        return P.normalize_frequency(s)
    if spec.kind == "int":
        bounds = {"due_day_of_month": (1, 31), "due_weekday": (0, 6), "interval_count": (1, 52),
                  "grace_period_days": (0, 365), "notice_period_days": (0, 730)}.get(name, (0, 100000))
        return str(P.parse_int(s, *bounds))
    if spec.kind == "bool":
        return "true" if P.parse_bool(s) else "false"
    if spec.kind == "enum":
        v = s.upper()
        if v not in spec.choices:
            raise P.ParseError(f"{name} must be one of {', '.join(spec.choices)}")
        return v
    return s[:2000]
