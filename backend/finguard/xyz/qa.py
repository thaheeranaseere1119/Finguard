"""XYZ Ltd financial question answering: question → plan → exact computation → evidence-backed answer.

Understanding:  a trained classifier (TF-IDF + logistic regression, see training.py) predicts the metric and the
                operation; deterministic parsers extract periods, filters (segment/region/customer), grouping and top-k.
                A lexicon of metric synonyms is used when the classifier is unsure.
Computation:    always deterministic over the ledger (metrics.py) — numbers never come from a model.
Scope:          XYZ Ltd only. Questions about other companies, or periods outside FY2017–FY2026, are refused explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

from finguard.xyz.coa import (COMPANY, CUSTOMERS, DATA_END, FIRST_FY, LAST_FY, REGIONS, SEGMENT_ALIASES, SEGMENTS, VENDORS,
                              fy_of, quarter_bounds)
from finguard.xyz.metrics import GROUPABLE, METRICS, Calc, Filters, Metric
from finguard.xyz.periods import (RANGE_HINT, Period, coverage_note, expand_fy_range, find_periods, fy_period,
                                  in_range)

OPS = ("SERIES", "VALUE", "CHANGE", "PCT_CHANGE", "AVERAGE", "MONTHLY_AVERAGE", "SUM", "TOP", "EXTREME_PERIOD", "SHARE", "CAGR",
       "DRIVERS")

# Metric synonyms (lexicon). Longest match wins. Used for corpus generation and as a fallback for the classifier.
LEXICON: dict[str, list[str]] = {
    "revenue": ["revenue from operations", "total revenue", "revenue", "net sales", "total sales", "sales", "turnover",
                "top line", "income from operations", "topline"],
    "product_revenue": ["product revenue", "product sales", "revenue from products", "licence and hardware revenue"],
    "service_revenue": ["service revenue", "services revenue", "revenue from services", "subscription revenue"],
    "other_income": ["other income", "interest income", "non-operating income", "income on deposits"],
    "cogs": ["cost of sales", "cost of goods sold", "cogs", "cost of revenue", "direct costs", "cost of products and services"],
    "gross_profit": ["gross profit", "gross earnings"],
    "gross_margin": ["gross margin", "gross profit margin", "gross margin percentage"],
    "employee_expense": ["employee benefit expense", "employee expenses", "salaries", "salary expense", "payroll",
                         "staff costs", "wages", "personnel expenses", "salaries and wages"],
    "rent": ["rent", "rent expense", "lease rental", "office rent"],
    "utilities": ["utilities", "electricity", "power and fuel", "utility expenses"],
    "marketing": ["marketing", "advertising", "marketing expense", "advertising and promotion", "marketing spend",
                  "sales and marketing expense"],
    "travel": ["travel", "travel expense", "travelling and conveyance", "business travel"],
    "it_expense": ["it expense", "software subscriptions", "it and software", "technology costs", "software expense"],
    "professional_fees": ["professional fees", "legal and professional", "consulting fees", "audit fees", "legal fees"],
    "depreciation": ["depreciation", "depreciation and amortisation", "depreciation expense", "d&a"],
    "repairs": ["repairs", "repairs and maintenance", "maintenance expense"],
    "interest_expense": ["interest expense", "finance costs", "finance cost", "interest paid", "borrowing costs"],
    "tax_expense": ["income tax expense", "tax expense", "income tax", "tax provision", "provision for tax", "taxes"],
    "opex": ["operating expenses", "total operating expenses", "opex", "total expenses", "overheads", "operating costs"],
    "ebitda": ["ebitda", "earnings before interest, tax, depreciation and amortisation", "operating ebitda"],
    "ebitda_margin": ["ebitda margin", "ebitda margin percentage"],
    "operating_income": ["operating income", "operating profit", "ebit", "earnings before interest and tax", "profit from operations"],
    "operating_margin": ["operating margin", "ebit margin", "operating profit margin"],
    "pbt": ["profit before tax", "pbt", "pre-tax profit", "earnings before tax", "income before taxes"],
    "net_income": ["net income", "net profit", "profit after tax", "pat", "net earnings", "bottom line", "profit for the year",
                   "net profit after tax", "profit", "profits", "earnings"],
    "net_margin": ["net margin", "net profit margin", "pat margin", "net income margin", "profit margin"],
    "effective_tax_rate": ["effective tax rate", "etr", "effective rate of tax"],
    "interest_coverage": ["interest coverage", "interest coverage ratio", "times interest earned"],
    "capex": ["capital expenditure", "capex", "capital spending", "purchase of property, plant and equipment",
              "investment in fixed assets", "pp&e purchases"],
    "capex_pct_revenue": ["capex as a % of revenue", "capex as a percentage of revenue", "capital intensity",
                          "capex to revenue", "capex as % of revenue"],
    "cfo": ["cash from operations", "operating cash flow", "cash flow from operating activities", "net cash from operating activities",
            "cash generated from operations", "cfo"],
    "cfi": ["cash flow from investing activities", "investing cash flow", "net cash used in investing activities"],
    "cff": ["cash flow from financing activities", "financing cash flow", "net cash from financing activities"],
    "fcf": ["free cash flow", "fcf"],
    "operating_cash_flow_ratio": ["operating cash flow ratio"],
    "dividends": ["dividends", "dividend paid", "dividends paid", "dividend payout"],
    "cash": ["cash balance", "cash and bank", "cash and cash equivalents", "cash position", "cash", "bank balance"],
    "receivables": ["trade receivables", "accounts receivable", "receivables", "net ar", "debtors", "ar balance"],
    "inventory": ["inventory", "inventories", "stock", "closing stock"],
    "payables": ["trade payables", "accounts payable", "payables", "creditors", "ap balance"],
    "borrowings": ["borrowings", "debt", "total debt", "loans", "term loans", "outstanding debt"],
    "ppe_net": ["net pp&e", "net property, plant and equipment", "net fixed assets", "fixed assets"],
    "total_assets": ["total assets", "assets"],
    "total_liabilities": ["total liabilities", "liabilities"],
    "equity": ["shareholders' equity", "total equity", "equity", "net worth", "book value"],
    "working_capital": ["working capital", "net working capital"],
    "current_ratio": ["current ratio", "liquidity ratio"],
    "debt_to_equity": ["debt to equity", "debt-to-equity", "debt equity ratio", "d/e ratio", "leverage ratio"],
    "roe": ["return on equity", "roe"],
    "roa": ["return on assets", "roa"],
    "dso": ["days sales outstanding", "dso", "receivable days", "collection period", "debtor days"],
    "dpo": ["days payable outstanding", "dpo", "payable days", "creditor days"],
    "inventory_turnover": ["inventory turnover", "inventory turnover ratio", "stock turnover"],
    "invoice_count": ["number of invoices", "number of sales invoices", "invoices issued", "count of invoices",
                      "how many invoices", "number of sales"],
    "transaction_count": ["number of transactions", "how many transactions", "transaction count", "number of journal entries"],
    "average_invoice": ["average invoice value", "average invoice size", "average deal size", "average ticket size"],
}

EXTERNAL_COMPANIES = ("3m", "activision blizzard", "adobe", "aes", "amazon", "amcor", "amd", "american express", "american water works",
                      "apple", "best buy", "block", "boeing", "coca-cola", "corning", "costco", "cvs", "foot locker", "general mills",
                      "johnson & johnson", "jpmorgan", "kraft heinz", "lockheed martin", "mgm resorts", "microsoft", "netflix",
                      "nike", "paypal", "pepsico", "pfizer", "ulta", "verizon", "walmart", "google", "alphabet", "tesla", "meta",
                      "infosys", "tcs", "tata", "wipro", "reliance", "hdfc", "icici", "flipkart", "hcl", "accenture", "ibm", "oracle")
_KNOWN_COUNTERPARTIES = {c.lower() for c in CUSTOMERS + VENDORS}
_COMPANY_SUFFIX = re.compile(r"\b((?:[A-Z][\w&.\-]*\s+){0,3}[A-Z][\w&.\-]*)\s+(Inc|Incorporated|Ltd|Limited|Corp|Corporation|Plc|LLC|Co)\b\.?")


@dataclass
class Plan:
    metric: str | None
    op: str
    periods: list[Period]
    filters: Filters = Filters()
    group: str | None = None       # customer | segment | region | month | quarter | day
    k: int = 1
    direction: str = "max"         # max | min
    unit_pref: str | None = None   # crore | lakh | million | exact
    confidence: float = 1.0
    assumptions: list[str] = field(default_factory=list)


@dataclass
class Answer:
    status: str                    # ANSWERED | OUT_OF_SCOPE | CLARIFY | NO_DATA
    text: str
    value: object = None
    unit: str | None = None
    plan: Plan | None = None
    evidence: list[dict] = field(default_factory=list)
    formula: str | None = None
    structured: dict | None = None


# ------------------------------------------------------------------------------------------------ scope
@lru_cache
def _external_names() -> tuple[str, ...]:
    names = set(EXTERNAL_COMPANIES) | {"amex", "jnj", "j&j", "coca cola", "coca-cola", "kenvue", "pepsi", "intel", "nvidia",
                                       "jpm", "amzn", "aapl", "msft", "googl", "nflx", "wmt", "jpmorgan chase",
                                       "salesforce", "uber", "airbnb", "disney", "starbucks", "mcdonald's", "visa", "mastercard"}
    fb = Path(__file__).resolve().parents[3] / "evaluation/data/raw/financebench/financebench_merged.jsonl"
    if fb.exists():
        import json

        for line in fb.read_text().splitlines():
            comp = json.loads(line).get("company", "")
            if comp:
                names.add(comp.lower())
                names.add(comp.lower().split()[0])
    names -= {"the", "american", "general", "best", "block", "corning", "foot"} - set(EXTERNAL_COMPANIES)
    return tuple(sorted(n for n in names if len(n) >= 2))


_NOT_COMPANY = {"xyz", "fy", "q1", "q2", "q3", "q4", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december", "january", "february", "company", "it", "ebitda", "who", "what", "which"}


def scope_check(q: str) -> str | None:
    low = q.lower()
    for name in _external_names():
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", low) and name not in _KNOWN_COUNTERPARTIES:
            return name.title() if name != "3m" else "3M"
    for m in _COMPANY_SUFFIX.finditer(q):
        name = m.group(1).strip()
        if name.lower() not in ("xyz",) and not name.lower().endswith("xyz") and name.lower() not in _KNOWN_COUNTERPARTIES:
            return f"{name} {m.group(2)}"
    # possessive proper noun that is not XYZ, a customer/vendor, a segment or a calendar word ("Infosys's revenue")
    for m in re.finditer(r"(?<!^)(?<![.?!]\s)\b([A-Z][A-Za-z&\-]{1,30}(?:\s[A-Z][A-Za-z&\-]{1,30})?)['’]s\b", q):
        name = m.group(1)
        lowname = name.lower()
        if ("xyz" in lowname or lowname.split()[-1] in ("ltd", "limited") and "xyz" in low or lowname in _NOT_COMPANY
                or lowname in _KNOWN_COUNTERPARTIES or any(k.endswith(lowname) or lowname in k for k in _KNOWN_COUNTERPARTIES)
                or any(lowname in s.lower() for s in SEGMENTS)):
            continue
        return name
    return None


# ------------------------------------------------------------------------------------------------ slot extraction
def _filters(q: str) -> tuple[Filters, str | None]:
    low = q.lower()
    seg = next((v for k, v in sorted(SEGMENT_ALIASES.items(), key=lambda kv: -len(kv[0]))
                if (len(k.split()) >= 2 and re.search(rf"\b{re.escape(k)}\b", low))
                or re.search(rf"\b{re.escape(k)}\s+(?:segment|business|division|unit|revenue|sales)\b", low)), None)
    reg = next((r for r in REGIONS if re.search(rf"\b{r.lower()}\b(?:\s+(?:region|india|zone))?", low)
                and (r != "Export" or re.search(r"\bexport", low))), None)
    if reg and reg != "Export" and not re.search(rf"\b{reg.lower()}\s+(?:region|india|zone)|region[s]?\s+{reg.lower()}|{reg.lower()}ern\b", low):
        reg = None  # 'north'/'south' only count as a region when phrased as a region
    cp = next((c for c in CUSTOMERS + VENDORS if c.lower() in low), None)
    group = None
    if re.search(r"\b(customer|client)s?\b", low) and not cp:
        group = "customer"
    elif re.search(r"\bsegments?\b|\bbusiness (unit|line)s?\b", low) and not seg:
        group = "segment"
    elif re.search(r"\bregions?\b|\bgeograph", low) and not reg:
        group = "region"
    elif re.search(r"\b(month)s?\b", low):
        group = "month"
    elif re.search(r"\bquarters?\b", low):
        group = "quarter"
    elif re.search(r"\b(day|date)s?\b", low):
        group = "day"
    return Filters(segment=seg, region=reg, counterparty=cp), group


def request_part(q: str) -> str:
    """Drop definition clauses ("DPO is defined as … average AP between FY2020 and FY2021") — they describe the formula,
    not what is being asked."""
    if re.search(r"\bdefined as\b|\bdefinition\b", q, re.I) and "?" in q:
        return q[: q.index("?") + 1]
    return q


def _mask_names(q: str) -> str:
    out = q
    for name in sorted(CUSTOMERS + VENDORS, key=len, reverse=True):
        out = re.sub(re.escape(name), "the customer", out, flags=re.I)
    return out


def _op_rules(q: str) -> str | None:
    low = _mask_names(q).lower()
    low = re.sub(r"\btop[- ]line\b", "topline", low)
    if re.search(r"\bwhat (?:drove|caused|explains?)\b|\bdrivers?\b|\breasons? for\b|\bwhy did\b", low):
        return "DRIVERS"
    if re.search(r"\bcagr\b|compound(?:ed)? annual growth", low):
        return "CAGR"
    if (re.search(r"\b(?:share|proportion|portion|mix)\b|\b(?:percentage|percent|%) of .{0,25}?(?:revenue|sales)\b", low)
            or (re.search(r"\bcontribut", low) and re.search(r"\b(?:percent|percentage|%|share)\b", low))) \
            and not re.search(r"capex|capital expenditure|percentage change|% change", low):
        return "SHARE"
    if re.search(r"\b(?:average|mean|avg)\s+(?:monthly|per month)\b|\bmonthly average\b|\bper month\b.{0,20}\baverage\b|"
                 r"\baverage\b.{0,60}\bper month\b|\bmean monthly\b", low):
        return "MONTHLY_AVERAGE"
    if re.search(r"\b(?:percentage|percent|%)\s+(?:change|increase|decrease|growth|decline|rise|fall)\b|\bgrowth rate\b|"
                 r"\bby what (?:percent|percentage)\b|\b(?:yoy|y-o-y|year[- ]over[- ]year|year on year)\b|\bgrow(?:th)? in %|\bgrew by what\b|"
                 r"\bin percent(?:age)?(?: terms)?\b|\bpercent(?:age)? terms\b|\bhow fast\b", low):
        return "PCT_CHANGE"
    if re.search(r"\b(?:which|what)\s+(month|quarter|day|date)\b|\bin which (month|quarter)\b|\b(highest|lowest|peak|best|worst|busiest)\s+(month|quarter|day)\b", low):
        return "EXTREME_PERIOD"
    if re.search(r"\b(?:top \d+|top|largest|biggest|highest|most|leading|smallest|lowest|least|rank|break ?down)\b", low) and \
            re.search(r"\b(customer|client|segment|region|business unit|geograph)", low):
        return "TOP"
    if re.search(r"\bwhich (customer|client|segment|region)\b|\bwho (?:was|is|were)\b", low):
        return "TOP"
    if re.search(r"\b(?:change|difference|increase|decrease|differ|grew|grow|declined?|rose|rise|fell|movement|moved?|moves|delta|variance)\b", low):
        return "CHANGE"
    if re.search(r"\b(?:average|mean|avg)\b", low):
        return "AVERAGE"
    if re.search(r"\b(?:total|combined|cumulative|aggregate|sum)\b", low) and len(find_periods(q)) >= 2:
        return "SUM"
    if re.search(r"\b(?:last|past|previous|trailing|recent)\s+(?:\d{1,3}|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+"
                 r"(?:months|quarters|days)\b|\bmonth[- ]?wise\b|\bmonthly breakdown\b|\beach month\b|\bby month\b|"
                 r"\bquarter[- ]?wise\b|\bmonth by month\b|\btrend\b", low):
        return "SERIES"
    return None


def _unit_pref(q: str) -> str | None:
    low = q.lower()
    if re.search(r"\bcrores?\b|\bcr\b", low):
        return "crore"
    if re.search(r"\blakhs?\b|\blacs?\b", low):
        return "lakh"
    if re.search(r"\bmillions?\b", low):
        return "million"
    return None


def lexicon_metric(q: str) -> str | None:
    low = " " + re.sub(r"[^\w%&'/\-\s.,]", " ", q.lower()) + " "
    best, blen = None, 0
    for key, syns in LEXICON.items():
        for s in syns:
            if re.search(rf"(?<![\w]){re.escape(s)}(?![\w])", low) and len(s) > blen:
                best, blen = key, len(s)
    return best


@lru_cache
def _model():
    from finguard.xyz.training import load_model

    return load_model()


def understand(q_full: str) -> Plan:
    q = request_part(q_full)
    model = _model()
    metric_pred, metric_p, op_pred, op_p = (None, 0.0, None, 0.0)
    if model is not None:
        metric_pred, metric_p, op_pred, op_p = model.predict(q)
    lex = lexicon_metric(q)
    # Hybrid policy (evaluated in training.evaluate): trust the classifier when confident; otherwise the lexicon.
    if metric_pred and (metric_p >= 0.55 or (not lex and metric_p >= 0.35)):
        metric, conf = metric_pred, metric_p
    else:
        metric, conf = lex, max(metric_p, 0.6 if lex else 0.0)
    if lex and metric_pred != lex and metric_p < 0.85 and len(lex) > 0:
        # an exact multi-word synonym is strong evidence; prefer it unless the classifier is very sure
        if any(len(s.split()) >= 2 and s in q.lower() for s in LEXICON[lex]):
            metric = lex
    op = _op_rules(q) or (op_pred if op_p >= 0.5 else None) or "VALUE"
    periods = find_periods(q)
    f, group = _filters(q)
    low = q.lower()
    k = 1
    if m := re.search(r"\btop\s+(\d{1,2})\b|\b(\d{1,2})\s+(?:biggest|largest|top|leading|highest|major|smallest|lowest)\b", low):
        k = int(m[1] or m[2])
    elif m := re.search(r"\btop\s+(two|three|five|ten)\b|\b(two|three|five|ten)\s+(?:biggest|largest|leading)\b", low):
        k = {"two": 2, "three": 3, "five": 5, "ten": 10}[m[1] or m[2]]
    elif re.search(r"\brank\b|\bbreak ?down\b|\bsplit\b|\blist (?:the|all)\b|\bby (?:region|segment|customer)s?\b", low):
        k = 10
    direction = "min" if re.search(r"\b(lowest|least|smallest|worst|minimum|weakest|slowest)\b", low) else "max"
    if op in ("CHANGE", "PCT_CHANGE", "CAGR", "DRIVERS") and len(periods) >= 2 and all(p.fy for p in periods[:2]):
        periods = sorted(periods[:2], key=lambda p: p.start)          # comparisons are chronological
    if op == "AVERAGE" and len(periods) == 1 and re.search(r"\bmonth", low):
        op = "MONTHLY_AVERAGE"
    if op in ("AVERAGE", "SUM") and RANGE_HINT.search(q):
        periods = expand_fy_range(periods)
    if metric and f.any() and not METRICS[metric].dims:
        f = Filters()                                                  # e.g. "software expense" is not a segment filter
    plan = Plan(metric, op, periods, f, group, k, direction, _unit_pref(q), conf,
                [p.assumption for p in periods if p.assumption])
    if op in ("TOP", "EXTREME_PERIOD") and not group:
        plan.group = "month" if op == "EXTREME_PERIOD" else "customer"
    if op == "SHARE" and not (f.segment or f.region or f.counterparty):
        plan.op = "TOP"
        plan.group = group or "segment"
        plan.k = 10
    if op in ("CHANGE", "PCT_CHANGE", "DRIVERS") and len(periods) == 1 and periods[0].kind == "FY":
        prev = periods[0].fy - 1
        plan.periods = [fy_period(prev), periods[0]]
        plan.assumptions.append(f"compared with the previous fiscal year FY{prev}")
    if op == "MONTHLY_AVERAGE" and not periods:
        plan.periods = []
    return plan


# ------------------------------------------------------------------------------------------------ execution
def _months(p: Period) -> list[Period]:
    out, d = [], p.start
    while d <= p.end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append(Period(f"{d:%B %Y}", d, min(p.end, nxt - timedelta(days=1)), "MONTH"))
        d = nxt
    return out


def _quarters(p: Period) -> list[Period]:
    fy = p.fy or (p.end.year + 1 if p.end.month >= 4 else p.end.year)
    return [Period(f"Q{q} FY{fy}", *quarter_bounds(fy, q), "QUARTER", fy) for q in range(1, 5)
            if quarter_bounds(fy, q)[0] >= p.start and quarter_bounds(fy, q)[1] <= p.end]


def value_of(calc: Calc, m: Metric, p: Period, f: Filters) -> Decimal:
    if f.any() and not m.dims:
        raise ValueError(f"{m.label} cannot be broken down by segment, region or customer")
    return m.fn(calc, p.start, p.end, f)


def execute(calc: Calc, plan: Plan) -> tuple[object, str, list[str]]:
    """Return (value, unit, extra-explanation lines). Value is Decimal, or list of (label, Decimal) for rankings."""
    m = METRICS[plan.metric]
    P, f = plan.periods, plan.filters
    if plan.op == "VALUE" and len(P) > 1 and len({p.kind for p in P}) == 1 and P[0].kind in ("MONTH", "QUARTER", "DAY"):
        plan.op = "SERIES"
    if plan.op == "SERIES":
        if len(P) == 1 and P[0].kind == "FY":
            P = plan.periods = _months(P[0])
        vals = [(p.label, value_of(calc, m, p, f)) for p in P]
        return vals, m.unit, []
    if plan.op == "VALUE":
        return value_of(calc, m, P[0], f), m.unit, []
    if plan.op in ("CHANGE", "PCT_CHANGE", "CAGR"):
        a, b = P[0], P[-1]
        va, vb = value_of(calc, m, a, f), value_of(calc, m, b, f)
        if plan.op == "CHANGE":
            return vb - va, m.unit, [f"{a.label}: {va}", f"{b.label}: {vb}"]
        if plan.op == "PCT_CHANGE":
            if va == 0:
                raise ZeroDivisionError("base period value is zero")
            return ((vb - va) * 100 / abs(va)).quantize(Decimal("0.01"), ROUND_HALF_UP), "PCT", [f"{a.label}: {va}", f"{b.label}: {vb}"]
        years = (b.fy or 0) - (a.fy or 0)
        if years <= 0 or va <= 0 or vb <= 0:
            raise ValueError("CAGR needs two fiscal years with positive values")
        cagr = (Decimal(float(vb / va) ** (1 / years) - 1) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return cagr, "PCT", [f"{a.label}: {va}", f"{b.label}: {vb}", f"{years} years"]
    if plan.op in ("AVERAGE", "SUM"):
        vals = [(p.label, value_of(calc, m, p, f)) for p in P]
        tot = sum((v for _, v in vals), Decimal(0))
        if plan.op == "SUM":
            return tot.quantize(Decimal("0.01")), m.unit, [f"{l}: {v}" for l, v in vals]
        return (tot / len(vals)).quantize(Decimal("0.01"), ROUND_HALF_UP), m.unit, [f"{l}: {v}" for l, v in vals]
    if plan.op == "MONTHLY_AVERAGE":
        months = P if len(P) > 1 and P[0].kind == "MONTH" else _months(P[0])
        vals = [value_of(calc, m, mp, f) for mp in months]
        return (sum(vals, Decimal(0)) / len(vals)).quantize(Decimal("0.01"), ROUND_HALF_UP), m.unit, [f"{len(months)} months"]
    if plan.op == "EXTREME_PERIOD":
        p = P[0]
        parts = {"month": _months(p), "quarter": _quarters(p)}.get(plan.group or "month")
        if plan.group == "day":
            parts = [Period(f"{p.start + timedelta(days=i):%d %b %Y}", p.start + timedelta(days=i), p.start + timedelta(days=i), "DAY")
                     for i in range((p.end - p.start).days + 1)]
        vals = [(x.label, value_of(calc, m, x, f)) for x in parts]
        vals.sort(key=lambda kv: kv[1], reverse=plan.direction == "max")
        return vals[: max(1, plan.k)], m.unit, []
    if plan.op in ("TOP", "SHARE"):
        if plan.metric not in GROUPABLE:
            raise ValueError(f"{m.label} is not available by {plan.group or 'segment'}")
        p = P[0]
        dim = {"customer": "counterparty", "segment": "segment", "region": "region"}[plan.group or "segment"]
        sign = -1 if m.key in ("revenue", "gross_profit", "product_revenue", "service_revenue") else 1
        if m.key == "invoice_count":
            raise ValueError("ranking by invoice count is not supported; ask for revenue by customer instead")
        if m.key == "gross_profit":
            rev = calc.s.group_flow(METRICS["revenue"].accounts, p.start, p.end, dim)
            cg = calc.s.group_flow(METRICS["cogs"].accounts, p.start, p.end, dim)
            grouped = {k: -rev.get(k, 0) - cg.get(k, 0) for k in set(rev) | set(cg)}
        else:
            grouped = {k: sign * v for k, v in calc.s.group_flow(m.accounts, p.start, p.end, dim).items()}
        total = sum(grouped.values())
        if plan.op == "SHARE":
            key = f.segment if dim == "segment" else f.region if dim == "region" else f.counterparty
            return (Decimal(grouped.get(key, 0)) * 100 / Decimal(total)).quantize(Decimal("0.01"), ROUND_HALF_UP), "PCT", \
                [f"{key}: {Decimal(grouped.get(key, 0)) / 100}", f"total: {Decimal(total) / 100}"]
        ranked = sorted(grouped.items(), key=lambda kv: kv[1], reverse=plan.direction == "max")
        return [(k, (Decimal(v) / 100).quantize(Decimal("0.01")), (Decimal(v) * 100 / Decimal(total)).quantize(Decimal("0.1")))
                for k, v in ranked[: max(1, plan.k)]], m.unit, [f"total: {Decimal(total) / 100}"]
    if plan.op == "DRIVERS":
        a, b = (P[0], P[-1]) if len(P) >= 2 else (fy_period(P[0].fy - 1), P[0])
        lines = []
        if m.key in ("revenue", "gross_profit", "cogs"):
            for dim in ("segment", "region"):
                ga = calc.s.group_flow(m.accounts, a.start, a.end, dim)
                gb = calc.s.group_flow(m.accounts, b.start, b.end, dim)
                deltas = sorted(((k, (-gb.get(k, 0) + ga.get(k, 0)) / 100) for k in set(ga) | set(gb)), key=lambda x: -abs(x[1]))
                lines.append(f"by {dim}: " + "; ".join(f"{k} {'+' if v >= 0 else ''}{_crore(Decimal(v))}" for k, v in deltas[:5]))
            delta = value_of(calc, m, b, Filters()) - value_of(calc, m, a, Filters())
            return delta, m.unit, lines
        # margins: change in each cost line as % of revenue
        ra, rb = Decimal(calc.revenue(a.start, a.end)), Decimal(calc.revenue(b.start, b.end))
        items = [("cost of sales", ("5000", "5100"))] + [(METRICS[k].label, METRICS[k].accounts) for k in
                                                           ("employee_expense", "rent", "marketing", "travel", "it_expense", "professional_fees",
                                                            "depreciation", "utilities", "repairs")]
        contrib = []
        for label, accs in items:
            sa = Decimal(calc.exp(accs, a.start, a.end)) * 100 / ra
            sb = Decimal(calc.exp(accs, b.start, b.end)) * 100 / rb
            contrib.append((label, (sa - sb).quantize(Decimal("0.01"))))
        contrib.sort(key=lambda x: -abs(x[1]))
        va, vb = value_of(calc, m, a, Filters()), value_of(calc, m, b, Filters())
        lines.append("contribution to margin change (percentage points, + improves margin): "
                     + "; ".join(f"{l} {'+' if v >= 0 else ''}{v} pp" for l, v in contrib[:5]))
        lines.append(f"revenue growth {((rb - ra) * 100 / ra).quantize(Decimal('0.1'))}%")
        return vb - va, m.unit if m.unit != "PCT" else "PP", [f"{a.label}: {va}", f"{b.label}: {vb}"] + lines
    raise ValueError(f"unsupported operation {plan.op}")


# ------------------------------------------------------------------------------------------------ formatting
def _indian(n: Decimal) -> str:
    sign = "-" if n < 0 else ""
    s = f"{abs(n):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return f"{sign}₹{whole}.{frac}"


def _crore(v: Decimal) -> str:
    return f"₹{(v / Decimal(10_000_000)).quantize(Decimal('0.01'), ROUND_HALF_UP):,} crore"


def fmt(v: Decimal, unit: str, pref: str | None = None) -> str:
    if unit == "INR":
        if pref == "lakh":
            return f"₹{(v / Decimal(100_000)).quantize(Decimal('0.01'), ROUND_HALF_UP):,} lakh ({_indian(v)})"
        if pref == "million":
            return f"₹{(v / Decimal(1_000_000)).quantize(Decimal('0.01'), ROUND_HALF_UP):,} million ({_indian(v)})"
        if abs(v) >= Decimal(10_000_000) or pref == "crore":
            return f"{_crore(v)} ({_indian(v)})"
        if abs(v) >= Decimal(100_000):
            return f"₹{(v / Decimal(100_000)).quantize(Decimal('0.01'), ROUND_HALF_UP):,} lakh ({_indian(v)})"
        return _indian(v)
    if unit == "PCT":
        return f"{v}%"
    if unit == "PP":
        return f"{v} percentage points"
    if unit == "X":
        return f"{v}x"
    if unit == "DAYS":
        return f"{v} days"
    return f"{int(v):,}"


def answer(calc: Calc, q: str, plan: Plan | None = None) -> Answer:
    a = _answer(calc, q, plan)
    if a.structured is None:
        a.structured = {"status": a.status, "title": {"OUT_OF_SCOPE": "Outside XYZ Ltd's records", "NO_DATA": "No data for that request",
                                                      "CLARIFY": "Need a bit more detail"}.get(a.status, "Answer"),
                        "summary": a.text, "blocks": [], "assumptions": [], "sources": []}
        if a.status == "CLARIFY":
            a.structured["title"] = "Not sure what you're asking"
            a.structured["blocks"].append({"type": "list", "title": "Try asking", "items": [
                "Tell me about XYZ Ltd", "What was the most recent transaction?", "Give me the last 6 months revenue",
                "Which customers owe us the most?", "Top 5 customers by revenue in FY2025",
                "Show the 5 largest vendor payments in FY2025", "Change in net profit from FY2022 to FY2023"]})
    return a


def _answer(calc: Calc, q: str, plan: Plan | None = None) -> Answer:
    other = scope_check(q)
    if other:
        return Answer("OUT_OF_SCOPE", f"I only have financial records for {COMPANY}. I can't answer questions about {other} — "
                                      f"no data for external companies is loaded, and I won't estimate it.")
    kb = knowledge_base(calc)
    if kb is not None and rag_first(q):                 # people, policies, events, board, relationships, history …
        ra = _rag_answer(kb, q)
        if ra.status == "ANSWERED":
            return ra                                   # otherwise fall through to the ledger engines
    plan = plan or understand(q)
    from finguard.xyz import records

    # a figure is clearly being asked for if a metric synonym matched, the classifier is very sure, or it is fairly
    # sure and a period was named ("How much did the Cloud Services segment earn in FY2020?")
    strong = plan.metric if (lexicon_metric(q) or plan.confidence >= 0.8 or (plan.confidence >= 0.55 and plan.periods)) else None
    rq = records.detect(request_part(q), strong)
    if rq and rq.intent == "NOT_RECORDED" and kb is not None:
        return _rag_answer(kb, q)                     # leadership, address, headcount … live in the company records
    if rq:                                     # transactions, customers/vendors, profile, coverage, accounts …
        res = records.run(calc, q, rq)
        ev = res.pop("evidence")
        val = res.pop("value")
        if kb is not None and rq.intent in ("PARTY", "PROFILE") and res["status"] == "ANSWERED":
            _attach_notes(kb, q + (" dossier account manager credit terms notes event" if rq.intent == "PARTY" else
                                   " company profile leadership"), res)
        return Answer(res["status"], res["summary"] + ("" if not res["assumptions"] else
                      " Assumption: " + "; ".join(res["assumptions"]) + "."),
                      [tuple(r) for r in val] if isinstance(val, list) else val, None, None, ev, None, res)
    if not plan.metric and kb is not None:
        return _rag_answer(kb, q)
    if not plan.metric:
        return Answer("CLARIFY", "I couldn't match that to XYZ Ltd's records. I can answer any figure (revenue, profit, margins, "
                                 "cash, balances) for a year, quarter, month or day, list transactions (most recent, largest, on a "
                                 "date, with a customer or vendor), and describe customers, vendors, segments and regions.", plan=plan)
    m = METRICS[plan.metric]
    if not plan.periods:
        # no period named: use the latest data rather than asking (balances at the last ledger date, flows for the latest FY)
        if m.kind == "BALANCE":
            plan.periods = [Period(f"{DATA_END.day} {DATA_END:%b %Y}", DATA_END, DATA_END, "DAY", fy_of(DATA_END))]
            plan.assumptions.append(f"no date given — balance as of the latest ledger date ({DATA_END.day} {DATA_END:%b %Y})")
        elif plan.op not in ("CHANGE", "PCT_CHANGE", "CAGR"):
            plan.periods = [fy_period(LAST_FY)]
            plan.assumptions.append(f"no period given — using the latest full fiscal year FY{LAST_FY} (Apr {LAST_FY - 1} – Mar {LAST_FY})")
        else:
            plan.periods = [fy_period(LAST_FY - 1), fy_period(LAST_FY)]
            plan.assumptions.append(f"no periods given — comparing the latest two fiscal years FY{LAST_FY - 1} and FY{LAST_FY}")
    bad = [p for p in plan.periods if not in_range(p)]
    if bad:
        return Answer("NO_DATA", f"There is no XYZ Ltd data for {', '.join(p.label for p in bad)}. {coverage_note()} "
                                 f"I won't extrapolate figures outside the recorded ledger.", plan=plan)
    if plan.op in ("CHANGE", "PCT_CHANGE", "CAGR") and len(plan.periods) < 2:
        return Answer("CLARIFY", f"Please name two periods to compare (e.g. FY2022 and FY2023).", plan=plan)
    if m.kind == "BALANCE" and plan.op in ("SUM", "MONTHLY_AVERAGE"):
        plan.op = "AVERAGE" if plan.op == "MONTHLY_AVERAGE" else "VALUE"
    try:
        value, unit, notes = execute(calc, plan)
    except (ValueError, ZeroDivisionError) as exc:
        return Answer("NO_DATA", f"I can't compute that: {exc}.", plan=plan)
    text = _sentence(plan, m, value, unit, notes)
    text = text[0].upper() + text[1:]
    if plan.assumptions:
        text += " Assumption: " + "; ".join(dict.fromkeys(plan.assumptions)) + "."
    ev = _evidence(calc, plan, m)
    a = Answer("ANSWERED", text, value, unit, plan, ev, m.formula)
    a.structured = structure(a, m, notes)
    if kb is not None and plan.op in ("DRIVERS", "CHANGE", "PCT_CHANGE"):
        _attach_notes(kb, q, a.structured)            # "why" context from the operations logs / board minutes
    return a


# ------------------------------------------------------------------------------------------------ RAG integration
_RAG_CUES = re.compile(
    r"\b(who (?:runs|leads|owns|founded)|ceo|founder|founded|managing director|chair(?:man|person)?|"
    r"board|minutes|directors?|headquarter(?:s|ed)?|head office|registered|located|offices?|employees|headcount|staff|"
    r"auditors?|banker|polic(?:y|ies)|per diem|hotel|travel rules|payment terms|credit terms|vendor terms|terms for|"
    r"account manager|dossier|relationship|notes? (?:on|about)|what happened|events?|milestones?|history|background|"
    r"outage|incident|certif\w*|iso|lockdown|covid|launched|opened|approved by|ratif\w*|decid\w*|budget|strategy|"
    r"what does (?:xyz|the company) do|explain|context|why|loans?|borrow\w*|role of|'s role)\b", re.I)


_ROLE_Q = re.compile(r"\b(who|whom|which|name of|appoint\w*)\b", re.I)
_ROLES = re.compile(r"\b(ceo|cfo|coo|cto|founders?|directors?|chair\w*|chief \w+ officer|head of \w+|auditors?|bankers?|banks?|"
                    r"account manager|board members?)\b", re.I)


_FIGURE_WORDS = re.compile(r"\b(how much|how many (?!employees|staff|people|days of credit)|total|sum|average|mean|growth|grow\w*|"
                           r"change|compare|trend|increase|decrease|margin|ratio|what (?:was|were)|highest|lowest|peak|"
                           r"top|largest|biggest|smallest|most|least|rank\w*|breakdown|by (?:month|quarter|segment|region|customer))\b", re.I)
_RULE_WORDS = re.compile(r"\b(target|minimum|maximum|limit|cap|polic(?:y|ies)|rules?|must|allowed|approv\w*|how often|"
                         r"frequency|written off)\b", re.I)
_KNOWLEDGE_START = re.compile(
    r"^\s*(?:tell me |please )?(who|whom|can|could|does|do|did|is|are|has|have|should|why|how long|how often|how quickly|"
    r"what happens|what does .{1,60} say|what notes|when (?:did|was|were|is|are|do|does)|"
    r"which (?!customers?|clients?|segments?|regions?|months?|quarters?|days?|years?|weeks?|dates?))\b", re.I)


def question_type(q: str) -> str:
    """'figure' — the ledger engines must answer (exact numbers); 'knowledge' — retrieval first; 'auto' — either."""
    from finguard.xyz.periods import find_periods

    if _RULE_WORDS.search(q) and not find_periods(q):
        return "knowledge"                               # "what is the TARGET DSO" is a rule, not the actual DSO
    if (lexicon_metric(q) and (find_periods(q) or _FIGURE_WORDS.search(q))) or (
            _FIGURE_WORDS.search(q) and re.search(r"\b(customers?|clients?|segments?|regions?|months?|quarters?)\b", q, re.I)):
        return "figure"
    if _KNOWLEDGE_START.search(q):
        return "knowledge"
    return "auto"


def rag_first(q: str) -> bool:
    """Knowledge-type question → retrieval answers it (falls back to the ledger engines if nothing relevant is found).
    ("CFO" alone is the cash-flow metric; "who is the CFO" is a person.)"""
    if _ROLE_Q.search(q) and _ROLES.search(q):
        return True
    qt = question_type(q)
    if qt == "figure":
        return False
    if qt == "knowledge":
        return True
    if re.search(r"\b(polic(?:y|ies)|minutes|board meeting|dossier|account manager|per diem|what happened)\b", q, re.I):
        return True
    from finguard.xyz.periods import find_periods

    # "what is the TARGET DSO / MINIMUM cash we keep / who MUST approve / how OFTEN" → a rule, not a figure
    if not find_periods(q) and re.search(r"\b(target|minimum|maximum|limit|cap|rules?|must|allowed|approv\w*|terms|how often|"
                                         r"when is|when are|frequency|keeps?|kept|carry|carries|rate)\b", q, re.I):
        return True
    # relationship questions about a named customer/vendor → the dossier / events, not the ledger profile
    from finguard.xyz.records import PARTIES

    if any(p.lower() in q.lower() for p in PARTIES) and re.search(
            r"\b(manag\w*|since when|since|customer since|relationship|win|won|signed|programme|deal|role|notes?)\b", q, re.I):
        return True
    if re.search(r"\bhow (?:was|is|were) (?:the )?business\b|\baffected\b|\bimpact\w*\b", q, re.I):
        return True
    return bool(_RAG_CUES.search(q)) and not lexicon_metric(q) and not re.search(r"\b(how much|total|sum)\b", q, re.I)


def knowledge_base(calc: Calc):
    from finguard.xyz.metrics import SQLSource

    kb = getattr(calc.s, "kb", None)
    if kb is not None:
        return kb
    if isinstance(calc.s, SQLSource):
        from finguard.xyz.kb import SQLKB

        store = SQLKB(calc.s.db, calc.s.org)
        try:
            if store.available():
                calc.s.kb = store
                return store
        except Exception:                                   # knowledge base not migrated / loaded — figures only
            return None
    return None


def _rag_answer(kb, q: str) -> Answer:
    from finguard.xyz import rag

    res = rag.answer(kb, q)
    st = rag.structured(q, res)
    return Answer(res["status"], res["answer"], None, None, None, rag.evidence(res), None, st)


def _attach_notes(kb, q: str, structured_obj: dict) -> None:
    from finguard.xyz import rag

    hits = kb.search(q, 4)
    if not rag.relevant(q, hits):
        return
    from finguard.xyz.records import PARTIES

    named = [p for p in PARTIES if p.lower() in q.lower()]
    if named:                                         # notes must be about the customer/vendor asked about
        hits = [h for h in hits if any(n.lower() in h.chunk.text.lower() for n in named)]
    items = []
    for h in hits[:2]:
        text, _ = rag.extractive_answer(q, [h], max_sents=1)
        if text:
            items.append({"text": text, "source": h.chunk.title})
    if items:
        structured_obj.setdefault("blocks", []).append({"type": "quote", "title": "Related company notes", "items": items})
        structured_obj.setdefault("sources", []).extend({"label": h.chunk.title, "detail": "company record"} for h in hits[:2])


def _sentence(plan: Plan, m: Metric, value, unit: str, notes: list[str]) -> str:
    P, f = plan.periods, plan.filters
    scope = ""
    if f.segment:
        scope += f" from the {f.segment} segment"
    if f.region:
        scope += f" from the {f.region} region"
    if f.counterparty:
        scope += f" from {f.counterparty}"
    label = m.label if m.label.split()[0].isupper() else m.label[0].lower() + m.label[1:]
    when = P[0].pretty if P else ""
    if plan.op == "VALUE":
        verb = "as at" if m.kind in ("BALANCE",) or (m.kind in ("MULTIPLE",) and m.key in ("current_ratio", "debt_to_equity")) else "for"
        when_b = f"{P[0].end.day} {P[0].end:%B %Y} (end of {P[0].label})" if verb == "as at" and P[0].kind != "DAY" else when
        return f"{COMPANY}'s {label}{scope} {verb} {when_b} was {fmt(value, unit, plan.unit_pref)}."
    if plan.op == "SERIES":
        rows = "; ".join(f"{l}: {fmt(v, unit, plan.unit_pref)}" for l, v in value)
        span = f"{P[0].label} – {P[-1].label}" if len(P) > 1 else P[0].label
        extra = ""
        if unit == "INR" and m.kind != "BALANCE":
            tot = sum((v for _, v in value), Decimal(0))
            extra = f" Total: {fmt(tot, unit, plan.unit_pref)}; monthly average: {fmt((tot / len(value)).quantize(Decimal('0.01')), unit, plan.unit_pref)}." \
                if P[0].kind == "MONTH" else f" Total: {fmt(tot, unit, plan.unit_pref)}."
        return f"{COMPANY}'s {label}{scope} for {span} ({len(value)} periods): {rows}.{extra}"
    if plan.op == "CHANGE":
        dirn = "increased" if value >= 0 else "decreased"
        return (f"{COMPANY}'s {label}{scope} {dirn} by {fmt(abs(value), unit, plan.unit_pref)} from {P[0].label} to "
                f"{P[-1].label} ({'; '.join(_pretty_notes(notes, m.unit, plan.unit_pref))}).")
    if plan.op == "PCT_CHANGE":
        return (f"{COMPANY}'s {label}{scope} changed by {value}% from {P[0].label} to {P[-1].label} "
                f"({'; '.join(_pretty_notes(notes, m.unit, plan.unit_pref))}).")
    if plan.op == "CAGR":
        return f"The compound annual growth rate of {COMPANY}'s {label} from {P[0].label} to {P[-1].label} was {value}% " \
               f"({'; '.join(_pretty_notes(notes[:2], m.unit, plan.unit_pref))})."
    if plan.op == "AVERAGE":
        return (f"The average {label}{scope} across {', '.join(p.label for p in P)} was {fmt(value, unit, plan.unit_pref)} "
                f"({'; '.join(_pretty_notes(notes, m.unit, plan.unit_pref))}).")
    if plan.op == "SUM":
        return (f"Total {label}{scope} across {', '.join(p.label for p in P)} was {fmt(value, unit, plan.unit_pref)}.")
    if plan.op == "MONTHLY_AVERAGE":
        when = f"{P[0].label} – {P[-1].label}" if len(P) > 1 else when
        return f"{COMPANY}'s average monthly {label}{scope} in {when} was {fmt(value, unit, plan.unit_pref)}."
    if plan.op == "EXTREME_PERIOD":
        which = "highest" if plan.direction == "max" else "lowest"
        lst = "; ".join(f"{l}: {fmt(v, unit, plan.unit_pref)}" for l, v in value)
        head = value[0]
        return f"The {which} {plan.group or 'month'} for {label}{scope} in {when} was {head[0]} " \
               f"({fmt(head[1], unit, plan.unit_pref)})." + (f" Ranking: {lst}." if len(value) > 1 else "")
    if plan.op == "TOP":
        noun = {"customer": "customer", "segment": "segment", "region": "region"}.get(plan.group or "segment", "group")
        rows = "; ".join(f"{i}. {k} — {fmt(v, unit, plan.unit_pref)} ({s}% of total)" for i, (k, v, s) in enumerate(value, 1))
        if len(value) == 1:
            k, v, s = value[0]
            which = "largest" if plan.direction == "max" else "smallest"
            return f"{COMPANY}'s {which} {noun} by {label} in {when} was {k} with {fmt(v, unit, plan.unit_pref)} ({s}% of the total)."
        return f"{label} by {noun} in {when}: {rows}."
    if plan.op == "SHARE":
        key = f.segment or f.region or f.counterparty
        return f"{key} accounted for {value}% of {COMPANY}'s {label} in {when} ({'; '.join(notes)})."
    if plan.op == "DRIVERS":
        return (f"{COMPANY}'s {label} changed by {fmt(value, unit)} from {plan.periods[0].label if len(plan.periods) > 1 else 'the prior year'} "
                f"to {plan.periods[-1].label}. Main drivers (computed from the ledger): "
                + " ".join(n + "." for n in _pretty_notes(notes, m.unit, plan.unit_pref)))
    return str(value)


def _pretty_notes(notes: list[str], unit: str, pref) -> list[str]:
    out = []
    for n in notes:
        if ": " in n:
            l, v = n.split(": ", 1)
            try:
                out.append(f"{l}: {fmt(Decimal(v), unit, pref)}")
                continue
            except Exception:
                pass
        out.append(n)
    return out


def _evidence(calc: Calc, plan: Plan, m: Metric) -> list[dict]:
    ev = []
    src = calc.s
    for p in plan.periods[:4]:
        item = {"id": f"ledger:{m.key}:{p.label}", "type": "ledger_aggregate", "metric": m.key, "period": p.label,
                "from": p.start.isoformat(), "to": p.end.isoformat(), "accounts": list(m.accounts),
                "formula": m.formula}
        if hasattr(src, "line_count") and m.accounts:
            span_start = p.start if m.kind != "BALANCE" else date(2016, 4, 1)
            item["journal_lines"] = src.line_count(m.accounts, span_start, p.end, plan.filters if m.dims else Filters())
        if hasattr(src, "sample_refs") and m.accounts:
            item["sample_documents"] = src.sample_refs(m.accounts, p.start, p.end, plan.filters if m.dims else Filters())
        ev.append(item)
    return ev


# ------------------------------------------------------------------------------------------------ structured output
def fmt_short(v: Decimal, unit: str, pref: str | None = None) -> str:
    if unit == "INR":
        if pref == "lakh" or (pref is None and abs(v) < Decimal(10_000_000)):
            return f"₹{(v / Decimal(100_000)).quantize(Decimal('0.01'), ROUND_HALF_UP):,} lakh" if abs(v) >= Decimal(100_000) else _indian(v)
        if pref == "million":
            return f"₹{(v / Decimal(1_000_000)).quantize(Decimal('0.01'), ROUND_HALF_UP):,} million"
        return _crore(v)
    return fmt(v, unit, pref)


def structure(a: Answer, m: Metric, notes: list[str]) -> dict:
    """Machine- and UI-friendly representation of an answer: title, summary, highlight figures, table, assumptions, sources."""
    plan, value, unit = a.plan, a.value, a.unit

    def _F(v, u, p=None):
        return fmt_short(v, u, p)

    pref = plan.unit_pref
    P = plan.periods
    label = m.label
    scope = ", ".join(f"{k}: {v}" for k, v in (("Segment", plan.filters.segment), ("Region", plan.filters.region),
                                                 ("Customer", plan.filters.counterparty)) if v)
    blocks: list[dict] = []
    highlights: list[dict] = []
    span = (f"{P[0].label} – {P[-1].label}" if len(P) > 1 else P[0].pretty) if P else ""
    title = f"{label} · {span}" + (f" · {scope}" if scope else "")
    op = plan.op
    if op == "VALUE":
        highlights.append({"label": label, "value": _F(value, unit, pref), "note": P[0].pretty})
    elif op in ("CHANGE", "PCT_CHANGE", "CAGR", "DRIVERS"):
        start = next((n.split(": ", 1)[1] for n in notes if n.startswith(P[0].label + ":")), None)
        end = next((n.split(": ", 1)[1] for n in notes if n.startswith(P[-1].label + ":")), None)
        mu = m.unit
        if start is not None and end is not None:
            highlights += [{"label": P[0].label, "value": _F(Decimal(start), mu, pref)},
                           {"label": P[-1].label, "value": _F(Decimal(end), mu, pref)}]
        name = {"CHANGE": "Change", "PCT_CHANGE": "% change", "CAGR": "CAGR", "DRIVERS": "Change"}[op]
        highlights.append({"label": name, "value": _F(value, unit if op != "DRIVERS" else (mu if mu != "PCT" else "PP"), pref),
                           "tone": "good" if value >= 0 else "bad"})
        if op == "DRIVERS":
            for n in notes:
                if n.startswith("by ") and ": " in n:
                    dim, rest = n[3:].split(": ", 1)
                    blocks.append({"type": "table", "title": f"Change by {dim}", "columns": [dim.title(), "Change"],
                                   "rows": [list(x.rsplit(" ", 2)[0:1]) + [" ".join(x.rsplit(" ", 2)[1:])] for x in rest.split("; ")]})
                elif n.startswith("contribution to margin change"):
                    rest = n.split(": ", 1)[1]
                    rows = []
                    for x in rest.split("; "):
                        nm, val = x.rsplit(" ", 2)[0], " ".join(x.rsplit(" ", 2)[1:])
                        rows.append([nm, val])
                    blocks.append({"type": "table", "title": "Contribution to margin change (+ improves margin)",
                                   "columns": ["Cost line", "Percentage points"], "rows": rows})
                elif n.startswith("revenue growth"):
                    highlights.append({"label": "Revenue growth", "value": n.split(" ", 2)[2]})
    elif op in ("SERIES", "AVERAGE", "SUM", "MONTHLY_AVERAGE"):
        rows_src = value if op == "SERIES" else None
        if op in ("AVERAGE", "SUM"):
            rows_src = [(n.split(": ", 1)[0], Decimal(n.split(": ", 1)[1])) for n in notes if ": " in n]
        if rows_src:
            vals = [v for _, v in rows_src]
            rows, prev = [], None
            for l, v in rows_src:
                chg = "" if prev in (None, 0) or m.unit not in ("INR", "COUNT") else f"{((v - prev) * 100 / abs(prev)).quantize(Decimal('0.1'))}%"
                rows.append([l, _F(v, m.unit, pref), chg])
                prev = v
            blocks.append({"type": "table", "title": f"{label} by period", "columns": ["Period", label, "Change vs previous"],
                           "rows": rows, "chart": {"kind": "bar", "values": [float(v) for v in vals],
                                                   "labels": [l for l, _ in rows_src]}})
            if m.unit in ("INR", "COUNT") and m.kind != "BALANCE":
                tot = sum(vals, Decimal(0))
                highlights.append({"label": "Total", "value": _F(tot, m.unit, pref)})
                highlights.append({"label": "Average per period", "value": _F((tot / len(vals)).quantize(Decimal("0.01")), m.unit, pref)})
            hi = max(rows_src, key=lambda x: x[1])
            lo = min(rows_src, key=lambda x: x[1])
            highlights += [{"label": "Highest", "value": _F(hi[1], m.unit, pref), "note": hi[0]},
                           {"label": "Lowest", "value": _F(lo[1], m.unit, pref), "note": lo[0]}]
        if op in ("AVERAGE", "MONTHLY_AVERAGE"):
            highlights.insert(0, {"label": "Average" if op == "AVERAGE" else "Monthly average", "value": _F(value, unit, pref)})
        if op == "SUM":
            highlights = [h for h in highlights if h["label"] != "Total"]
            highlights.insert(0, {"label": "Total", "value": _F(value, unit, pref)})
    elif op == "TOP":
        noun = (plan.group or "segment").title()
        blocks.append({"type": "table", "title": f"{label} by {noun.lower()}", "columns": ["Rank", noun, label, "Share of total"],
                       "rows": [[str(i), k, _F(v, unit, pref), f"{s}%"] for i, (k, v, s) in enumerate(value, 1)]})
        k, v, s = value[0]
        highlights.append({"label": f"{'Top' if plan.direction == 'max' else 'Lowest'} {noun.lower()}", "value": k,
                           "note": f"{_F(v, unit, pref)} · {s}%"})
    elif op == "EXTREME_PERIOD":
        blocks.append({"type": "table", "title": f"{label} — {'highest' if plan.direction == 'max' else 'lowest'} {plan.group}s",
                       "columns": ["Rank", (plan.group or "month").title(), label],
                       "rows": [[str(i), l, _F(v, unit, pref)] for i, (l, v) in enumerate(value, 1)]})
        highlights.append({"label": f"{'Highest' if plan.direction == 'max' else 'Lowest'} {plan.group}", "value": value[0][0],
                           "note": _F(value[0][1], unit, pref)})
    elif op == "SHARE":
        highlights.append({"label": f"Share of {label}", "value": f"{value}%"})
        for n in notes:
            k, v = n.split(": ", 1)
            highlights.append({"label": k.title() if k == "total" else k, "value": _F(Decimal(v).quantize(Decimal("0.01")), "INR", pref)})
    if highlights:
        blocks.insert(0, {"type": "highlights", "items": highlights})
    sources = [{"label": e["period"], "detail": f"accounts {', '.join(e['accounts'])}"
                + (f" · {e['journal_lines']:,} journal lines" if e.get("journal_lines") is not None else "")
                + (f" · e.g. {', '.join(e['sample_documents'])}" if e.get("sample_documents") else "")} for e in a.evidence]
    summary = a.text.split(" Assumption:")[0]
    if op == "SERIES" and value:
        hi = max(value, key=lambda x: x[1])
        tot = sum((v for _, v in value), Decimal(0))
        summary = (f"{COMPANY}'s {label[0].lower() + label[1:]} for {span} " +
                   (f"totalled {_F(tot, m.unit, pref)} across {len(value)} periods; " if m.unit in ("INR", "COUNT") and m.kind != "BALANCE" else "")
                   + f"highest in {hi[0]} ({_F(hi[1], m.unit, pref)}).")
    elif op == "TOP" and value:
        k, v, sh = value[0]
        summary = (f"{k} was {COMPANY}'s {'largest' if plan.direction == 'max' else 'smallest'} {(plan.group or 'segment')} by "
                   f"{label[0].lower() + label[1:]} in {P[0].label} ({_F(v, unit, pref)}, {sh}% of total).")
    elif op == "DRIVERS":
        top = next((b["rows"][0] for b in blocks if b.get("type") == "table" and b.get("rows")), None)
        summary = (f"{label} {'rose' if value >= 0 else 'fell'} by {_F(abs(value), unit if m.unit != 'PCT' else 'PP', pref)} from "
                   f"{P[0].label} to {P[-1].label}" + (f"; the largest factor was {top[0]} ({top[1]})." if top else "."))
    elif op == "EXTREME_PERIOD" and value:
        summary = (f"The {'highest' if plan.direction == 'max' else 'lowest'} {plan.group} for {label[0].lower() + label[1:]} in "
                   f"{P[0].label} was {value[0][0]} ({_F(value[0][1], unit, pref)}).")
    if a.value is not None and unit == "INR" and not isinstance(a.value, list):
        for h in blocks[0]["items"] if blocks and blocks[0]["type"] == "highlights" else []:
            if h["label"] in (label, "Total", "Average", "Monthly average", "Change"):
                h.setdefault("detail", _indian(a.value) if h["label"] in (label, "Total", "Average", "Monthly average") else None)
    return {"status": a.status, "title": title, "summary": summary, "blocks": blocks,
            "assumptions": list(dict.fromkeys(plan.assumptions)), "formula": m.formula, "sources": sources,
            "plan": {"metric": plan.metric, "operation": op, "periods": [p.label for p in P]}}
