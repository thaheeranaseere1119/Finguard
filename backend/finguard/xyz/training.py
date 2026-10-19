"""Question corpus, intent-model training and evaluation for XYZ Ltd financial Q&A.

Corpus (all questions are about XYZ Ltd only):
  * TEMPLATE questions in FinanceBench style ("What is the FY2018 capital expenditure for XYZ Ltd?") and TAT-QA style
    ("What is the percentage change in revenue from 2019 to 2020?"), plus conversational phrasings. Templates are split
    70/30: the test split uses phrasings the model never saw during training.
  * FINANCEBENCH-REWRITE (test only): real FinanceBench questions whose metric the ledger supports, with the company
    replaced by XYZ Ltd, years mapped into FY2017–FY2026 and USD units replaced by INR.
  * TATQA-REWRITE (test only): real TAT-QA arithmetic question frames ("change in … from 2018 to 2019") with the line
    item replaced by an XYZ Ltd ledger item and the years mapped.
  * OUT-OF-SCOPE (test only): original FinanceBench questions about real companies → must be refused; periods
    outside the data → must say "no data".
Gold answers are computed from the gold plan on the generator's in-memory ledger (independent of the parser and SQL).

The trained model is a pair of TF-IDF (word 1–2 grams + char 2–5 grams) logistic-regression classifiers predicting the
metric and the operation. Periods, filters and numbers are masked first so the model learns intent wording only.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from finguard.xyz.coa import CUSTOMERS, FIRST_FY, LAST_FY, MONTHS, REGIONS, SEGMENTS, VENDORS, quarter_bounds
from finguard.xyz.metrics import GROUPABLE, METRICS, Calc, Filters
from finguard.xyz.periods import Period, fy_period

MODEL_DIR = Path(__file__).resolve().parent / "model"
MODEL_PATH = MODEL_DIR / "xyz_intent.joblib"
EVAL_ROOT = Path(__file__).resolve().parents[3] / "evaluation"

FLOW_INR = [k for k, m in METRICS.items() if m.kind == "FLOW" and k not in ("average_invoice",)]
BALANCES = [k for k, m in METRICS.items() if m.kind == "BALANCE"]
RATIOS = [k for k, m in METRICS.items() if m.kind in ("RATIO", "MULTIPLE", "DAYS")]
DAILY_OK = ["revenue", "invoice_count", "product_revenue", "service_revenue", "cogs", "gross_profit", "transaction_count",
            "average_invoice"]

# ------------------------------------------------------------------------------------------------ templates
# {cs}=company possessive, {c}=company, {m}=metric phrase, {p}/{p1}/{p2}/{p3}=periods, {seg}/{reg}/{cust}, {k}
T: dict[str, list[str]] = {
    "VALUE": [
        "What was {cs} {m} in {p}?", "What is the {p} {m} for {c}?", "How much {m} did {c} report for {p}?",
        "Report {cs} {m} for {p}.", "{m} for {p}?", "Tell me the {m} of {c} in {p}.", "What was the {m} during {p}?",
        "Give me {cs} {m} for {p}.", "Can you tell me how much the {m} was in {p}?", "What figure did {c} post for {m} in {p}?",
        "Please provide the {m} reported by {c} for {p}.", "I need the {m} number for {p}.", "Show {m} for {p}",
        "What did {c} record as {m} in {p}?", "What is {cs} {p} {m}? Answer using the ledger.",
        "As per the books, what was the {m} in {p}?", "Find the {m} of {c} for the period {p}.",
        "Kindly share the {m} for {p}.", "What amount of {m} was recorded in {p}?", "{p} {m} of {c}?",
    ],
    "BAL_VALUE": [
        "What was {cs} {m} at the end of {p}?", "What is {cs} year end {p} {m}?", "How much {m} did {c} have as of the end of {p}?",
        "What was the closing {m} for {p}?", "Report the {m} on {cs} balance sheet for {p}.", "{m} as at end of {p}?",
        "What {m} did {c} carry at {p} close?", "Tell me the {m} position at the end of {p}.", "Balance of {m} at {p} year end?",
        "What was the {m} reported on the balance sheet at the close of {p}?",
    ],
    "CHANGE": [
        "What is the change in {m} from {p1} to {p2}?", "By how much did {cs} {m} increase between {p1} and {p2}?",
        "What was the difference in {m} between {p1} and {p2}?", "How did {m} move from {p1} to {p2}?",
        "What was the absolute change in {cs} {m} between {p1} and {p2}?", "How much higher or lower was {m} in {p2} than in {p1}?",
        "Compute the change in {m} for {c} from {p1} to {p2}.", "What is the variance in {m} between {p1} and {p2}?",
        "How much did {m} grow from {p1} to {p2}?", "What was the increase or decrease in {m} from {p1} to {p2}?",
        "Difference between {p1} and {p2} {m}?", "How much did the {m} change between {p1} and {p2} for {c}?",
    ],
    "PCT_CHANGE": [
        "What is the percentage change in {m} from {p1} to {p2}?", "By what percent did {cs} {m} grow from {p1} to {p2}?",
        "What was the growth rate of {m} between {p1} and {p2}?", "What is the % change in {m} from {p1} to {p2}?",
        "How much did {m} grow in percentage terms between {p1} and {p2}?", "What was the year-over-year growth in {m} in {p2}?",
        "What was the YoY change in {cs} {m} for {p2}?", "Percentage increase in {m} from {p1} to {p2}?",
        "Calculate the percent change of {m} between {p1} and {p2}.", "By what percentage did {m} change from {p1} to {p2}?",
        "What is the year on year growth of {m} for {p2}?", "Growth rate of {cs} {m} from {p1} to {p2} in percent?",
    ],
    "AVERAGE": [
        "What is the average {m} for {p1}, {p2} and {p3}?", "What was the mean {m} across {p1} to {p3}?",
        "What is the {p1} - {p3} 3 year average of {m} for {c}?", "Calculate the average {m} over {p1}, {p2} and {p3}.",
        "What was {cs} average {m} between {p1} and {p3}?", "Average {m} for {p1} through {p3}?",
        "What is the arithmetic mean of {m} for the years {p1}, {p2}, {p3}?", "Give the average annual {m} from {p1} to {p3}.",
    ],
    "MONTHLY_AVERAGE": [
        "What was the average monthly {m} in {p}?", "What is {cs} monthly average {m} for {p}?",
        "On average, how much {m} did {c} book per month in {p}?", "Average {m} per month during {p}?",
        "What was the mean monthly {m} for {p}?", "How much {m} per month on average in {p}?",
    ],
    "SUM": [
        "What was the total {m} from {p1} to {p3}?", "What is the combined {m} for {p1} and {p2}?",
        "What was the cumulative {m} across {p1}, {p2} and {p3}?", "Sum of {m} for {p1} and {p2}?",
        "Add up {cs} {m} for {p1}, {p2} and {p3}.", "What was the aggregate {m} over {p1} to {p3}?",
    ],
    "TOP": [
        "Who was {cs} largest customer by {m} in {p}?", "Which customer contributed the most {m} in {p}?",
        "List the top {k} customers by {m} in {p}.", "Which segment generated the highest {m} in {p}?",
        "Which region had the most {m} in {p}?", "Rank the business segments by {m} for {p}.",
        "Top {k} clients by {m} during {p}?", "Which region contributed the least {m} in {p}?",
        "Who were the {k} biggest customers in terms of {m} for {p}?", "Which business segment led {m} in {p}?",
        "Break down {m} by region for {p}.", "Which customer had the smallest {m} in {p}?",
    ],
    "EXTREME_PERIOD": [
        "Which month had the highest {m} in {p}?", "In which quarter of {p} was {m} the lowest?",
        "Which month of {p} recorded peak {m}?", "What was the best month for {m} in {p}?",
        "Which quarter had the highest {m} in {p}?", "Which day had the highest {m} in {p}?",
        "What was the weakest month for {m} during {p}?", "Which month in {p} saw the lowest {m}?",
    ],
    "SHARE": [
        "What percentage of {p} {m} came from {seg}?", "What share of {m} did the {reg} region contribute in {p}?",
        "How much of {cs} {m} in {p} was contributed by {cust}, as a percentage?", "What proportion of {m} was from {seg} in {p}?",
        "What was {seg}'s share of total {m} in {p}?", "What % of {m} came from the {reg} region in {p}?",
        "What was the contribution of {seg} to {m} in {p} in percent?",
    ],
    "CAGR": [
        "What is the CAGR of {m} from {p1} to {p2}?", "What was the compound annual growth rate of {cs} {m} between {p1} and {p2}?",
        "Calculate the {p1} to {p2} CAGR for {m}.", "At what compound annual rate did {m} grow from {p1} to {p2}?",
        "CAGR of {m} between {p1} and {p2}?",
    ],
    "FILTER_VALUE": [
        "What was revenue from {cust} in {p}?", "How much did the {seg} segment earn in {p}?",
        "What was the {reg} region's {m} in {p}?", "How much {m} came from {cust} during {p}?",
        "What {m} did {seg} generate in {p}?", "What was {m} in the {reg} region for {p}?",
        "Total {m} billed to {cust} in {p}?", "{seg} segment {m} for {p}?", "How much did {cust} contribute in {m} in {p}?",
    ],
    "DRIVERS": [
        "What drove the change in {m} in {p2}?", "What explains the movement in {cs} {m} from {p1} to {p2}?",
        "What were the main drivers of {m} change between {p1} and {p2}?", "Why did {m} change in {p2} compared with {p1}?",
        "What caused the {m} to move from {p1} to {p2}?",
    ],
}


def _split_of(op: str, i: int) -> str:
    return "train" if (i * 7 + len(op)) % 10 < 7 else "test"


def fmt_fy(fy: int, rng: random.Random) -> str:
    return rng.choice([f"FY{fy}", f"FY{fy}", f"FY{str(fy)[2:]}", f"fiscal {fy}", f"FY {fy - 1}-{str(fy)[2:]}",
                       f"{fy - 1}-{str(fy)[2:]}", f"fiscal year {fy}", f"financial year {fy - 1}-{str(fy)[2:]}"])


def fmt_q(fy: int, q: int, rng: random.Random) -> str:
    ordw = ["first", "second", "third", "fourth"][q - 1]
    return rng.choice([f"Q{q} FY{fy}", f"Q{q} FY{str(fy)[2:]}", f"the {ordw} quarter of FY{fy}", f"Q{q} of fiscal {fy}"])


def fmt_month(y: int, mo: int, rng: random.Random) -> str:
    n = MONTHS[mo - 1]
    return rng.choice([f"{n} {y}", f"{n[:3]} {y}", f"{n}, {y}"])


def fmt_day(d: date, rng: random.Random) -> str:
    return rng.choice([f"{d.day} {MONTHS[d.month - 1]} {d.year}", f"{MONTHS[d.month - 1]} {d.day}, {d.year}", d.isoformat(),
                       f"{d.day}{'th' if 4 <= d.day <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d.day % 10, 'th')} {MONTHS[d.month - 1]} {d.year}"])


@dataclass
class Example:
    qid: str
    question: str
    metric: str | None
    op: str
    periods: list[tuple[str, str, str, str, int | None]]   # (label, start, end, kind, fy)
    filters: dict
    group: str | None
    k: int
    direction: str
    source: str        # TEMPLATE | FINANCEBENCH | TATQA | OOS | OUT_OF_RANGE
    split: str
    expected_status: str = "ANSWERED"
    gold: str | None = None
    gold_unit: str | None = None

    def plan(self):
        from finguard.xyz.qa import Plan

        return Plan(self.metric, self.op, [Period(l, date.fromisoformat(a), date.fromisoformat(b), k, fy) for l, a, b, k, fy in self.periods],
                    Filters(**self.filters), self.group, self.k, self.direction)


def _p(p: Period):
    return (p.label, p.start.isoformat(), p.end.isoformat(), p.kind, p.fy)


def _company(rng):
    return rng.choice([("XYZ Ltd's", "XYZ Ltd"), ("XYZ Ltd's", "XYZ Ltd"), ("XYZ's", "XYZ"), ("the company's", "the company"),
                       ("XYZ Limited's", "XYZ Limited")])


def _syn(metric: str, rng) -> str:
    from finguard.xyz.qa import LEXICON

    return rng.choice(LEXICON[metric])


def template_examples(n_per_op: int = 400, seed: int = 7) -> list[Example]:
    rng = random.Random(seed)
    out: list[Example] = []
    fys = list(range(FIRST_FY, LAST_FY + 1))
    for op, temps in T.items():
        for j in range(n_per_op):
            ti = j % len(temps)
            tpl = temps[ti]
            split = _split_of(op, ti)
            cs, c = _company(rng)
            real_op = {"BAL_VALUE": "VALUE", "FILTER_VALUE": "VALUE"}.get(op, op)
            fil, group, k, direction = {}, None, 1, "max"
            kw: dict = {"cs": cs, "c": c}
            if op == "BAL_VALUE":
                metric = rng.choice(BALANCES + ["current_ratio", "debt_to_equity", "working_capital"])
                fy = rng.choice(fys)
                periods = [fy_period(fy)]
                kw["p"] = fmt_fy(fy, rng)
            elif op == "VALUE":
                kind = rng.choices(["FY", "Q", "M", "D"], weights=[5, 2, 2, 1])[0]
                fy = rng.choice(fys)
                if kind == "D":
                    metric = rng.choice(DAILY_OK[:5])
                    d = date(fy - 1, 4, 1) + timedelta(days=rng.randint(0, 364))
                    periods, kw["p"] = [Period(f"{d:%d %b %Y}", d, d, "DAY")], fmt_day(d, rng)
                elif kind == "M":
                    metric = rng.choice(FLOW_INR[:20] + ["invoice_count"])
                    mo = rng.randint(1, 12)
                    y = fy if mo <= 3 else fy - 1
                    import calendar

                    a, b = date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
                    periods, kw["p"] = [Period(f"{a:%B %Y}", a, b, "MONTH")], fmt_month(y, mo, rng)
                elif kind == "Q":
                    metric = rng.choice(FLOW_INR + RATIOS[:6])
                    q = rng.randint(1, 4)
                    a, b = quarter_bounds(fy, q)
                    periods, kw["p"] = [Period(f"Q{q} FY{fy}", a, b, "QUARTER", fy)], fmt_q(fy, q, rng)
                else:
                    metric = rng.choice(FLOW_INR + RATIOS + ["invoice_count", "transaction_count", "average_invoice"])
                    periods, kw["p"] = [fy_period(fy)], fmt_fy(fy, rng)
            elif op in ("CHANGE", "PCT_CHANGE", "CAGR", "DRIVERS"):
                if op == "DRIVERS":
                    metric = rng.choice(["revenue", "gross_profit", "operating_margin", "net_margin", "ebitda_margin"])
                else:
                    metric = rng.choice(FLOW_INR + BALANCES + (RATIOS if op == "CHANGE" else []))
                if op == "CAGR":
                    metric = rng.choice(["revenue", "net_income", "gross_profit", "ebitda", "employee_expense", "opex", "cash", "total_assets"])
                a = rng.choice(fys[:-1])
                b = rng.choice([y for y in fys if y > a]) if op != "DRIVERS" and "{p2}" in tpl and "{p1}" in tpl else a + 1
                if ("{p1}" not in tpl):  # YoY phrasing: only {p2}
                    b = rng.choice(fys[1:])
                    a = b - 1
                periods = [fy_period(a), fy_period(b)]
                kw.update(p1=fmt_fy(a, rng), p2=fmt_fy(b, rng))
            elif op in ("AVERAGE", "SUM"):
                metric = rng.choice(FLOW_INR + (BALANCES if op == "AVERAGE" else []))
                a = rng.choice(fys[:-2])
                ys = [a, a + 1, a + 2]
                periods = [fy_period(y) for y in ys]
                kw.update(p1=fmt_fy(ys[0], rng), p2=fmt_fy(ys[1], rng), p3=fmt_fy(ys[2], rng))
                if "{p3}" not in tpl:
                    periods = periods[:2]
                elif "{p2}" not in tpl:
                    # "p1 to p3" phrasing: a range of 3 years
                    kw.update(p1=f"FY{ys[0]}", p3=f"FY{ys[2]}")
            elif op == "MONTHLY_AVERAGE":
                metric = rng.choice(FLOW_INR[:24] + ["invoice_count"])
                fy = rng.choice(fys)
                periods, kw["p"] = [fy_period(fy)], fmt_fy(fy, rng)
            elif op == "TOP":
                metric = rng.choice(["revenue", "revenue", "gross_profit", "cogs"])
                fy = rng.choice(fys)
                periods, kw["p"] = [fy_period(fy)], fmt_fy(fy, rng)
                low = tpl.lower()
                group = "customer" if ("customer" in low or "client" in low) else "segment" if "segment" in low else "region"
                k = rng.choice([3, 5, 10]) if "{k}" in tpl else (10 if ("rank" in low or "break down" in low) else 1)
                direction = "min" if any(w in low for w in ("least", "smallest")) else "max"
                kw["k"] = k
            elif op == "EXTREME_PERIOD":
                metric = rng.choice(["revenue", "invoice_count", "cogs", "gross_profit", "marketing", "employee_expense"])
                fy = rng.choice(fys)
                low = tpl.lower()
                group = "quarter" if "quarter" in low else "day" if "day" in low else "month"
                if group == "day":
                    mo = rng.randint(1, 12)
                    y = fy if mo <= 3 else fy - 1
                    import calendar

                    a, b = date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
                    periods, kw["p"] = [Period(f"{a:%B %Y}", a, b, "MONTH")], fmt_month(y, mo, rng)
                    metric = rng.choice(["revenue", "invoice_count"])
                else:
                    periods, kw["p"] = [fy_period(fy)], fmt_fy(fy, rng)
                direction = "min" if any(w in low for w in ("lowest", "weakest")) else "max"
            elif op == "SHARE":
                metric = "revenue"
                fy = rng.choice(fys)
                periods, kw["p"] = [fy_period(fy)], fmt_fy(fy, rng)
                if "{seg}" in tpl:
                    seg = rng.choice(SEGMENTS)
                    fil = {"segment": seg}
                    kw["seg"] = rng.choice([seg, seg + " segment"]) if "segment" not in tpl else seg
                    kw["seg"] = seg
                elif "{reg}" in tpl:
                    reg = rng.choice(REGIONS)
                    fil = {"region": reg}
                    kw["reg"] = reg
                else:
                    cust = rng.choice(CUSTOMERS[:20])
                    fil = {"counterparty": cust}
                    kw["cust"] = cust
            else:  # FILTER_VALUE
                metric = rng.choice(["revenue", "revenue", "gross_profit", "cogs", "invoice_count"])
                fy = rng.choice(fys)
                periods, kw["p"] = [fy_period(fy)], fmt_fy(fy, rng)
                if "{cust}" in tpl:
                    cust = rng.choice(CUSTOMERS[:30])
                    fil, kw["cust"] = {"counterparty": cust}, cust
                elif "{seg}" in tpl:
                    seg = rng.choice(SEGMENTS)
                    fil, kw["seg"] = {"segment": seg}, seg
                else:
                    reg = rng.choice(REGIONS)
                    fil, kw["reg"] = {"region": reg}, reg
                if "revenue from" in tpl or "earn" in tpl or "contribute" in tpl:
                    metric = "revenue" if "contribute" not in tpl else metric
                if "earn" in tpl:
                    metric = "revenue"
            if "{m}" in tpl:
                kw["m"] = _syn(metric, rng)
            q = tpl.format(**{**{"p": "", "p1": "", "p2": "", "p3": "", "seg": "", "reg": "", "cust": "", "k": 1}, **kw})
            q = q[0].upper() + q[1:]
            out.append(Example(f"tpl-{op}-{j:04d}", q, metric, real_op, [_p(p) for p in periods], fil, group, k, direction,
                               "TEMPLATE", split))
    return out


# ------------------------------------------------------------------------------------------------ FinanceBench / TAT-QA rewrites
FB_MAP = [
    (r"capex as a %|capex as % of|capital expenditure as a %|capex as a percentage", "capex_pct_revenue"),
    (r"operating cash flow ratio", "operating_cash_flow_ratio"),
    (r"days payable outstanding|\bdpo\b", "dpo"), (r"days sales outstanding|\bdso\b", "dso"),
    (r"inventory turnover", "inventory_turnover"), (r"free cash flow", "fcf"),
    (r"cash from operations|operating cash flow|cash flow from operations|cash flows from operating", "cfo"),
    (r"capital expenditure|\bcapex\b", "capex"), (r"operating margin", "operating_margin"), (r"gross margin", "gross_margin"),
    (r"ebitda margin", "ebitda_margin"), (r"\bebitda\b", "ebitda"), (r"effective tax rate", "effective_tax_rate"),
    (r"interest coverage", "interest_coverage"), (r"return on assets|\broa\b", "roa"), (r"return on equity|\broe\b", "roe"),
    (r"net profit margin|net income margin|net margin|net earnings as a percent", "net_margin"),
    (r"net income|net profit|net earnings", "net_income"), (r"operating income", "operating_income"),
    (r"current ratio", "current_ratio"), (r"total assets", "total_assets"),
    (r"net ar\b|accounts receivable", "receivables"), (r"accounts payable", "payables"),
    (r"\binventor", "inventory"), (r"total revenue|net revenue|\brevenue\b|net sales", "revenue"),
    (r"dividend", "dividends"), (r"depreciation", "depreciation"), (r"\bcogs\b|cost of goods sold", "cogs"),
]
FB_SKIP = re.compile(r"segment|acquisition|securities|legal|registered|geograph|guidance|quick ratio|restructuring|"
                     r"employees|headcount|why|which|what are|outlook|margin profile|debt securities|product line|"
                     r"dividend payout ratio|capital-intensive|per share|eps|rating|pension|lease|fixed asset turnover|asset turnover|"
                     r"cash conversion cycle|\bccc\b|as a percent of (?:net )?sales|% of (?:net )?sales|categories|"
                     r"working capital ratio|quick ratio|\bq[1-4]\b", re.I)


def financebench_rewrites() -> tuple[list[Example], list[Example]]:
    path = EVAL_ROOT / "data/raw/financebench/financebench_merged.jsonl"
    if not path.exists():
        return [], []
    rewrites, oos = [], []
    for line in path.read_text().splitlines():
        r = json.loads(line)
        q0, comp = r["question"], r["company"]
        oos.append(Example(f"fb-oos-{r['financebench_id']}", q0, None, "VALUE", [], {}, None, 1, "max", "OOS", "test",
                           expected_status="OUT_OF_SCOPE"))
        if FB_SKIP.search(q0):
            continue
        metric = next((m for pat, m in FB_MAP if re.search(pat, q0, re.I)), None)
        years = [int(y) for y in re.findall(r"\bFY\s?'?(\d{4})\b", q0)] or [int(2000 + int(y)) for y in re.findall(r"\bFY(\d{2})\b", q0)]
        if not metric or not years:
            continue
        mapping = {y: min(LAST_FY, max(FIRST_FY + 1, y + 4)) for y in years}
        q = q0
        aliases = {comp, comp.split()[0], comp.upper(), comp.replace("-", " "), comp.replace(" ", "-")}
        if comp == "Johnson & Johnson":
            aliases |= {"JnJ", "J&J"}
        if comp == "American Express":
            aliases |= {"AMEX", "Amex"}
        for alias in sorted(aliases, key=len, reverse=True):
            q = re.sub(rf"{re.escape(alias)}(?:'s)?", "XYZ Ltd" if not alias.endswith("'s") else "XYZ Ltd's", q)
        q = re.sub(r"XYZ Ltd's?'s", "XYZ Ltd's", q)
        for y, ny in mapping.items():
            q = re.sub(rf"FY\s?'?{y}", f"FY{ny}", q)
            q = re.sub(rf"FY{str(y)[2:]}\b", f"FY{ny}", q)
        q = re.sub(r"USD\s+(millions|billions|thousands)|\bUSD\b|\$", "INR crore", q)
        q = re.sub(r"in INR crore millions", "in INR crore", q)
        from finguard.xyz.qa import scope_check

        if scope_check(q):          # another company still named after rewriting → not a clean XYZ question
            continue
        mapped = sorted(set(mapping.values()))
        low = q.lower()
        if metric in ("dpo", "dso", "roa", "roe", "inventory_turnover"):
            # 'average balance between FYa and FYb' is part of the metric definition, not an average question
            op, periods = "VALUE", [fy_period(mapped[-1])]
        elif re.search(r"average", low) and len(mapped) >= 2:
            op, periods = "AVERAGE", [fy_period(y) for y in range(mapped[0], mapped[-1] + 1)]
        elif re.search(r"drove|caused", low):
            op, periods = "DRIVERS", [fy_period(mapped[-1] - 1), fy_period(mapped[-1])]
        elif re.search(r"change|growth|grow", low) and len(mapped) >= 2:
            op = "PCT_CHANGE" if re.search(r"%|percent|growth rate|rate of", low) else "CHANGE"
            periods = [fy_period(mapped[0]), fy_period(mapped[-1])]
        elif re.search(r"change|growth|grow", low):
            op = "PCT_CHANGE" if re.search(r"%|percent|growth", low) else "CHANGE"
            periods = [fy_period(mapped[0] - 1), fy_period(mapped[0])]
        elif len(mapped) == 1:
            op, periods = "VALUE", [fy_period(mapped[0])]
        else:
            continue
        rewrites.append(Example(f"fb-{r['financebench_id']}", q, metric, op, [_p(p) for p in periods], {}, None, 1, "max",
                                "FINANCEBENCH", "test"))
    return rewrites, oos


TATQA_FRAME = re.compile(r"^(?P<pre>what (?:is|was) the (?P<op>percentage change|change|average|difference|increase|decrease)"
                         r"(?: in| of)? (?:the )?)(?P<item>[^?]+?)(?P<mid> (?:from|between|in) )(?P<y1>(?:19|20)\d{2})"
                         r"(?P<mid2> (?:to|and) )(?P<y2>(?:19|20)\d{2})(?P<post>.*)$", re.I)


def tatqa_rewrites(limit: int = 250, seed: int = 11) -> list[Example]:
    from finguard.xyz.qa import LEXICON

    path = EVAL_ROOT / "data/raw/tatqa/tatqa_dataset_dev.json"
    if not path.exists():
        return []
    rng = random.Random(seed)
    out = []
    candidates = [k for k in FLOW_INR + BALANCES if k in LEXICON]
    for doc in json.loads(path.read_text()):
        for q in doc["questions"]:
            m = TATQA_FRAME.match(q["question"].strip())
            if not m:
                continue
            metric = rng.choice(candidates)
            item = rng.choice(LEXICON[metric])
            y1, y2 = int(m["y1"]), int(m["y2"])
            a, b = sorted((y1, y2))
            na = min(LAST_FY - 1, max(FIRST_FY, a + 4))
            nb = na + (b - a if b > a else 1)
            if nb > LAST_FY:
                continue
            opw = m["op"].lower()
            op = {"percentage change": "PCT_CHANGE", "average": "AVERAGE"}.get(opw, "CHANGE")
            if op == "AVERAGE":
                periods = [fy_period(na), fy_period(nb)]
                text = f"{m['pre']}{item}{m['mid']}{na}{m['mid2']}{nb}{m['post']}"
            else:
                periods = [fy_period(na), fy_period(nb)]
                text = f"{m['pre']}{item}{m['mid']}{na}{m['mid2']}{nb}{m['post']}"
            text = text[0].upper() + text[1:]
            out.append(Example(f"tq-{q['uid'][:12]}", text, metric, op, [_p(p) for p in periods], {}, None, 1, "max", "TATQA", "test"))
            if len(out) >= limit:
                return out
    return out


def fresh_examples() -> list[Example]:
    """The frozen hand-written final test (fresh_test.py), authored before error analysis."""
    import calendar

    from finguard.xyz.fresh_test import FRESH

    out = []
    for i, (q, metric, op, pers, fil, group, k, direction, status) in enumerate(FRESH):
        periods = []
        for p in pers:
            if isinstance(p, int):
                periods.append(fy_period(p))
            elif isinstance(p, tuple):
                a, b = quarter_bounds(p[0], p[1])
                periods.append(Period(f"Q{p[1]} FY{p[0]}", a, b, "QUARTER", p[0]))
            elif ":" in p:
                a, b = (date.fromisoformat(x) for x in p.split(":"))
                periods.append(Period(f"{a:%B %Y}", a, b, "MONTH"))
            else:
                d = date.fromisoformat(p)
                periods.append(Period(f"{d:%d %b %Y}", d, d, "DAY"))
        src = "FRESH" if status == "ANSWERED" else "FRESH_REFUSAL"
        out.append(Example(f"fresh-{i:02d}", q, metric, op, [_p(x) for x in periods], fil, group, k, direction, src, "test",
                           expected_status=status))
    return out


def out_of_range_examples() -> list[Example]:
    qs = ["What was XYZ Ltd's revenue in FY2012?", "What is the net profit of XYZ Ltd for FY2030?",
          "How much cash did XYZ Ltd have at the end of FY2014?", "What were total sales on 5 January 2015?",
          "What was the operating margin in FY2031?", "What was XYZ's capex in fiscal 2013?"]
    return [Example(f"oor-{i}", q, None, "VALUE", [], {}, None, 1, "max", "OUT_OF_RANGE", "test", expected_status="NO_DATA")
            for i, q in enumerate(qs)]


# ------------------------------------------------------------------------------------------------ model
MASKS = [
    (re.compile(r"\b(?:fy|fiscal(?: year)?|financial year)\s?'?\d{2,4}(?:\s*-\s*\d{2,4})?\b", re.I), " PERIOD "),
    (re.compile(r"\bq[1-4]\b", re.I), " QTR "),
    (re.compile(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b", re.I), " MONTHNAME "),
    (re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b\d{1,2}/\d{1,2}/\d{4}\b"), " DATE "),
    (re.compile(r"\b(?:19|20)\d{2}\b"), " YEAR "),
    (re.compile(r"\d+(?:st|nd|rd|th)?\b"), " NUM "),
]


def normalize(q: str) -> str:
    s = q
    for name in sorted(CUSTOMERS + VENDORS, key=len, reverse=True):
        s = re.sub(re.escape(name), " CUSTOMER ", s, flags=re.I)
    for seg in SEGMENTS:
        s = re.sub(re.escape(seg), " SEGMENTNAME ", s, flags=re.I)
    s = re.sub(r"xyz(?: ltd| limited)?(?:'s)?", " COMPANY ", s, flags=re.I)
    for rx, rep in MASKS:
        s = rx.sub(rep, s)
    return " ".join(s.lower().split())


class IntentModel:
    def __init__(self, metric_clf, op_clf, meta: dict) -> None:
        self.metric_clf, self.op_clf, self.meta = metric_clf, op_clf, meta

    def predict(self, q: str):
        x = [normalize(q)]
        mp = self.metric_clf.predict_proba(x)[0]
        op = self.op_clf.predict_proba(x)[0]
        mi, oi = mp.argmax(), op.argmax()
        return self.metric_clf.classes_[mi], float(mp[mi]), self.op_clf.classes_[oi], float(op[oi])


def _pipeline():
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import FeatureUnion, make_pipeline
    from sklearn.feature_extraction.text import TfidfVectorizer

    return make_pipeline(
        FeatureUnion([("w", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1)),
                      ("c", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True, min_df=2))]),
        LogisticRegression(C=20, max_iter=3000))


def train(examples: list[Example]) -> IntentModel:
    import joblib

    tr = [e for e in examples if e.split == "train" and e.metric]
    X = [normalize(e.question) for e in tr]
    mclf = _pipeline().fit(X, [e.metric for e in tr])
    oclf = _pipeline().fit(X, [e.op for e in tr])
    meta = {"trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "train_examples": len(tr),
            "metrics": sorted(set(e.metric for e in tr)), "ops": sorted(set(e.op for e in tr)),
            "corpus_sha256": hashlib.sha256("\n".join(e.question for e in tr).encode()).hexdigest()}
    model = IntentModel(mclf, oclf, meta)
    MODEL_DIR.mkdir(exist_ok=True)
    joblib.dump({"metric": mclf, "op": oclf, "meta": meta}, MODEL_PATH)
    (MODEL_DIR / "xyz_intent.meta.json").write_text(json.dumps(meta, indent=2))
    return model


def load_model() -> IntentModel | None:
    if not MODEL_PATH.exists():
        return None
    import joblib

    d = joblib.load(MODEL_PATH)
    return IntentModel(d["metric"], d["op"], d["meta"])


# ------------------------------------------------------------------------------------------------ gold & evaluation
def compute_gold(examples: list[Example], mem_calc: Calc) -> None:
    from finguard.xyz.qa import execute

    for e in examples:
        if e.expected_status != "ANSWERED":
            continue
        try:
            v, unit, _ = execute(mem_calc, e.plan())
            e.gold, e.gold_unit = _canon(v), unit
        except (ValueError, ZeroDivisionError) as exc:
            e.expected_status, e.gold = "NO_DATA", str(exc)


def _canon(v) -> str:
    if isinstance(v, list):
        return json.dumps([[str(x) for x in row] if isinstance(row, tuple) else str(row) for row in v])
    return str(v)


def _match(gold: str, got) -> bool:
    g = _canon(got)
    if g == gold:
        return True
    try:
        return abs(Decimal(g) - Decimal(gold)) <= Decimal("0.011")
    except Exception:
        return False


def evaluate(examples: list[Example], sql_calc: Calc, model: IntentModel | None, max_per_source: int | None = None) -> dict:
    from finguard.xyz import qa

    res = {}
    rows = []
    by: dict[str, list] = {}
    for e in examples:
        if e.split != "test":
            continue
        by.setdefault(e.source, []).append(e)
    for src, exs in by.items():
        if max_per_source:
            exs = exs[:max_per_source]
        stats = {"n": 0, "status_ok": 0, "answer_ok": 0, "metric_ok": 0, "op_ok": 0, "clf_metric_ok": 0, "lex_metric_ok": 0,
                 "latency_ms": []}
        for e in exs:
            t0 = time.perf_counter()
            a = qa.answer(sql_calc, e.question)
            dt = (time.perf_counter() - t0) * 1000
            stats["n"] += 1
            stats["latency_ms"].append(dt)
            ok_status = a.status == e.expected_status
            stats.setdefault("safe_non_answer", 0)
            if e.expected_status in ("OUT_OF_SCOPE", "NO_DATA"):
                stats["safe_non_answer"] += a.status != "ANSWERED"
            stats["status_ok"] += ok_status
            ok_ans = ok_status and (e.expected_status != "ANSWERED" or _match(e.gold, a.value))
            stats["answer_ok"] += ok_ans
            if e.metric:
                stats["metric_ok"] += bool(a.plan and a.plan.metric == e.metric)
                stats["op_ok"] += bool(a.plan and a.plan.op == e.op)
                if model:
                    stats["clf_metric_ok"] += model.predict(e.question)[0] == e.metric
                stats["lex_metric_ok"] += qa.lexicon_metric(e.question) == e.metric
            rows.append({"qid": e.qid, "source": src, "question": e.question, "expected_status": e.expected_status,
                         "status": a.status, "gold": e.gold, "got": _canon(a.value) if a.value is not None else None,
                         "correct": ok_ans, "metric": e.metric, "pred_metric": a.plan.metric if a.plan else None,
                         "op": e.op, "pred_op": a.plan.op if a.plan else None, "answer": a.text})
        lat = sorted(stats.pop("latency_ms"))
        n = max(1, stats["n"])
        labelled = sum(1 for e in exs if e.metric) or 1
        res[src] = {"n": stats["n"], "answer_accuracy": stats["answer_ok"] / n, "status_accuracy": stats["status_ok"] / n,
                    "metric_accuracy_pipeline": stats["metric_ok"] / labelled, "op_accuracy_pipeline": stats["op_ok"] / labelled,
                    "metric_accuracy_classifier_only": stats["clf_metric_ok"] / labelled,
                    "metric_accuracy_lexicon_only": stats["lex_metric_ok"] / labelled,
                    "no_fabricated_answer_rate": (stats.get("safe_non_answer", 0) / n) if src in ("OOS", "OUT_OF_RANGE", "FRESH_REFUSAL") else None,
                    "latency_ms_p50": lat[len(lat) // 2] if lat else None, "latency_ms_p95": lat[int(0.95 * (len(lat) - 1))] if lat else None}
    return {"by_source": res, "rows": rows}
