"""Financial metric definitions over an abstract ledger source.

Each metric is defined ONCE in terms of base aggregates (account flows over a period, balances at a date, counts,
cash-flow categories). Two sources implement the base aggregates:
  * SQLSource    — PostgreSQL over fin_journal_lines (what the agent uses in production)
  * MemorySource — the generator's in-memory ledger (independent "gold" path used to verify answers)
Definitions follow FinanceBench conventions where they exist (e.g. DSO, DPO, inventory turnover use average balances).
All money is computed in integer paise and converted to Decimal rupees at the end.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Protocol

from finguard.xyz.coa import DATA_START

REVENUE = ("4000", "4100")
COGS = ("5000", "5100")
OPEX_EX_DEP = ("6000", "6100", "6200", "6300", "6400", "6500", "6600", "6800")
OPEX = OPEX_EX_DEP + ("6700",)


@dataclass(frozen=True)
class Filters:
    segment: str | None = None
    region: str | None = None
    counterparty: str | None = None

    def any(self) -> bool:
        return bool(self.segment or self.region or self.counterparty)


class Source(Protocol):
    def flow(self, accounts: tuple[str, ...], start: date, end: date, f: Filters = Filters()) -> int: ...   # debit − credit
    def balance(self, accounts: tuple[str, ...], as_of: date) -> int: ...                                   # debit − credit
    def count(self, kind: str, start: date, end: date, f: Filters = Filters()) -> int: ...
    def cash_by_category(self, category: str, start: date, end: date) -> int: ...
    def group_flow(self, accounts: tuple[str, ...], start: date, end: date, dim: str, f: Filters = Filters()) -> dict[str, int]: ...
    def line_count(self, accounts: tuple[str, ...], start: date, end: date, f: Filters = Filters()) -> int: ...


# ---------------------------------------------------------------------------------------------- metric registry
@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    kind: str          # FLOW | BALANCE | RATIO | DAYS | COUNT | MULTIPLE
    unit: str          # INR | PCT | X | DAYS | COUNT
    accounts: tuple[str, ...]
    fn: Callable[["Calc", date, date, Filters], Decimal]
    formula: str
    dims: bool = False  # supports segment/region/customer filters and grouping


class Calc:
    def __init__(self, src: Source) -> None:
        self.s = src

    # base helpers (paise, sign-adjusted)
    def inc(self, acc, a, b, f=Filters()) -> int:      # income-type (credit balance) flow
        return -self.s.flow(acc, a, b, f)

    def exp(self, acc, a, b, f=Filters()) -> int:      # expense / asset-increase flow
        return self.s.flow(acc, a, b, f)

    def bal_asset(self, acc, d) -> int:
        return self.s.balance(acc, d)

    def bal_liab(self, acc, d) -> int:
        return -self.s.balance(acc, d)

    def revenue(self, a, b, f=Filters()): return self.inc(REVENUE, a, b, f)
    def cogs(self, a, b, f=Filters()): return self.exp(COGS, a, b, f)
    def gross_profit(self, a, b, f=Filters()): return self.revenue(a, b, f) - self.cogs(a, b, f)
    def opex(self, a, b): return self.exp(OPEX, a, b)
    def ebitda(self, a, b): return self.gross_profit(a, b) - self.exp(OPEX_EX_DEP, a, b)
    def ebit(self, a, b): return self.gross_profit(a, b) - self.opex(a, b)
    def pbt(self, a, b): return self.ebit(a, b) + self.inc(("4200",), a, b) - self.exp(("7000",), a, b)
    def net_income(self, a, b): return self.pbt(a, b) - self.exp(("8000",), a, b)
    def capex(self, a, b): return self.exp(("1500",), a, b)
    def cfo(self, a, b): return self.s.cash_by_category("OPERATING", a, b)
    def cfi(self, a, b): return self.s.cash_by_category("INVESTING", a, b)
    def cff(self, a, b): return self.s.cash_by_category("FINANCING", a, b)
    def equity(self, d):
        # share capital + opening RE − dividends + cumulative profit to date
        return (self.bal_liab(("3000", "3100"), d) - self.bal_asset(("3200",), d)
                + self.net_income(DATA_START, d))
    def total_assets(self, d): return self.bal_asset(("1000", "1100", "1200", "1500", "1510"), d)
    def total_liabilities(self, d): return self.bal_liab(("2000", "2300", "2500"), d)
    def current_assets(self, d): return self.bal_asset(("1000", "1100", "1200"), d)
    def current_liabilities(self, d): return self.bal_liab(("2000", "2300"), d)
    def avg_bal(self, getter, a, b) -> Decimal:
        # FinanceBench convention: average of opening (day before period) and closing balance
        return (Decimal(getter(a - timedelta(days=1))) + Decimal(getter(b))) / 2


def R(p: int | Decimal) -> Decimal:
    return (Decimal(p) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pct(n, d) -> Decimal:
    if not d:
        raise ZeroDivisionError("denominator is zero")
    return (Decimal(n) * 100 / Decimal(d)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def ratio(n, d, places="0.01") -> Decimal:
    if not d:
        raise ZeroDivisionError("denominator is zero")
    return (Decimal(n) / Decimal(d)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def days_ratio(n, d, a: date, b: date) -> Decimal:
    if not d:
        raise ZeroDivisionError("denominator is zero")
    span = (b - a).days + 1
    return (Decimal(n) * span / Decimal(d)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _acct(code: str, label: str, key: str, income: bool = False) -> Metric:
    return Metric(key, label, "FLOW", "INR", (code,),
                  (lambda c, a, b, f: R(c.inc((code,), a, b, f) if income else c.exp((code,), a, b, f))),
                  f"{'credits − debits' if income else 'debits − credits'} on account {code} ({label}) in the period")


METRICS: dict[str, Metric] = {m.key: m for m in [
    Metric("revenue", "Revenue from operations", "FLOW", "INR", REVENUE, lambda c, a, b, f: R(c.revenue(a, b, f)),
           "product revenue (4000) + service revenue (4100)", dims=True),
    _acct("4000", "Product revenue", "product_revenue", income=True),
    _acct("4100", "Service revenue", "service_revenue", income=True),
    _acct("4200", "Other income (interest income)", "other_income", income=True),
    Metric("cogs", "Cost of sales", "FLOW", "INR", COGS, lambda c, a, b, f: R(c.cogs(a, b, f)),
           "cost of goods sold (5000) + cost of services (5100)", dims=True),
    Metric("gross_profit", "Gross profit", "FLOW", "INR", REVENUE + COGS, lambda c, a, b, f: R(c.gross_profit(a, b, f)),
           "revenue − cost of sales", dims=True),
    Metric("gross_margin", "Gross margin", "RATIO", "PCT", REVENUE + COGS,
           lambda c, a, b, f: pct(c.gross_profit(a, b, f), c.revenue(a, b, f)), "gross profit ÷ revenue × 100", dims=True),
    _acct("6000", "Employee benefit expense", "employee_expense"),
    _acct("6100", "Rent expense", "rent"),
    _acct("6200", "Utilities expense", "utilities"),
    _acct("6300", "Marketing and advertising expense", "marketing"),
    _acct("6400", "Travel expense", "travel"),
    _acct("6500", "IT and software expense", "it_expense"),
    _acct("6600", "Professional fees", "professional_fees"),
    _acct("6700", "Depreciation", "depreciation"),
    _acct("6800", "Repairs and maintenance", "repairs"),
    _acct("7000", "Finance costs (interest expense)", "interest_expense"),
    _acct("8000", "Income tax expense", "tax_expense"),
    Metric("opex", "Total operating expenses", "FLOW", "INR", OPEX, lambda c, a, b, f: R(c.opex(a, b)),
           "sum of employee, rent, utilities, marketing, travel, IT, professional fees, depreciation, repairs"),
    Metric("ebitda", "EBITDA", "FLOW", "INR", REVENUE + COGS + OPEX_EX_DEP, lambda c, a, b, f: R(c.ebitda(a, b)),
           "gross profit − operating expenses excluding depreciation"),
    Metric("ebitda_margin", "EBITDA margin", "RATIO", "PCT", REVENUE, lambda c, a, b, f: pct(c.ebitda(a, b), c.revenue(a, b)),
           "EBITDA ÷ revenue × 100"),
    Metric("operating_income", "Operating income (EBIT)", "FLOW", "INR", REVENUE + COGS + OPEX, lambda c, a, b, f: R(c.ebit(a, b)),
           "gross profit − total operating expenses"),
    Metric("operating_margin", "Operating margin", "RATIO", "PCT", REVENUE, lambda c, a, b, f: pct(c.ebit(a, b), c.revenue(a, b)),
           "operating income ÷ revenue × 100"),
    Metric("pbt", "Profit before tax", "FLOW", "INR", REVENUE + COGS + OPEX + ("4200", "7000"), lambda c, a, b, f: R(c.pbt(a, b)),
           "operating income + other income − finance costs"),
    Metric("net_income", "Net profit (profit after tax)", "FLOW", "INR", REVENUE + COGS + OPEX + ("4200", "7000", "8000"),
           lambda c, a, b, f: R(c.net_income(a, b)), "profit before tax − income tax expense"),
    Metric("net_margin", "Net profit margin", "RATIO", "PCT", REVENUE, lambda c, a, b, f: pct(c.net_income(a, b), c.revenue(a, b)),
           "net profit ÷ revenue × 100"),
    Metric("effective_tax_rate", "Effective tax rate", "RATIO", "PCT", ("8000",),
           lambda c, a, b, f: pct(c.exp(("8000",), a, b), c.pbt(a, b)), "income tax expense ÷ profit before tax × 100"),
    Metric("interest_coverage", "Interest coverage ratio", "MULTIPLE", "X", ("7000",),
           lambda c, a, b, f: ratio(c.ebit(a, b), c.exp(("7000",), a, b)), "operating income ÷ finance costs"),
    Metric("capex", "Capital expenditure", "FLOW", "INR", ("1500",), lambda c, a, b, f: R(c.capex(a, b)),
           "additions to property, plant and equipment (debits to 1500)"),
    Metric("capex_pct_revenue", "Capital expenditure as % of revenue", "RATIO", "PCT", ("1500",) + REVENUE,
           lambda c, a, b, f: pct(c.capex(a, b), c.revenue(a, b)), "capex ÷ revenue × 100"),
    Metric("cfo", "Cash flow from operating activities", "FLOW", "INR", ("1000",), lambda c, a, b, f: R(c.cfo(a, b)),
           "net cash movements in entries classified OPERATING"),
    Metric("cfi", "Cash flow from investing activities", "FLOW", "INR", ("1000",), lambda c, a, b, f: R(c.cfi(a, b)),
           "net cash movements in entries classified INVESTING"),
    Metric("cff", "Cash flow from financing activities", "FLOW", "INR", ("1000",), lambda c, a, b, f: R(c.cff(a, b)),
           "net cash movements in entries classified FINANCING (incl. interest paid, loans, dividends)"),
    Metric("fcf", "Free cash flow", "FLOW", "INR", ("1000", "1500"), lambda c, a, b, f: R(c.cfo(a, b) - c.capex(a, b)),
           "cash flow from operations − capital expenditure"),
    Metric("operating_cash_flow_ratio", "Operating cash flow ratio", "MULTIPLE", "X", ("1000", "2000", "2300"),
           lambda c, a, b, f: ratio(c.cfo(a, b), c.current_liabilities(b)), "cash from operations ÷ current liabilities at period end"),
    Metric("dividends", "Dividends paid", "FLOW", "INR", ("3200",), lambda c, a, b, f: R(c.exp(("3200",), a, b)),
           "dividends declared and paid (debits to 3200)"),
    Metric("cash", "Cash and bank balance", "BALANCE", "INR", ("1000",), lambda c, a, b, f: R(c.bal_asset(("1000",), b)),
           "closing balance of account 1000 at period end"),
    Metric("receivables", "Trade receivables (net AR)", "BALANCE", "INR", ("1100",), lambda c, a, b, f: R(c.bal_asset(("1100",), b)),
           "closing balance of account 1100 at period end"),
    Metric("inventory", "Inventories", "BALANCE", "INR", ("1200",), lambda c, a, b, f: R(c.bal_asset(("1200",), b)),
           "closing balance of account 1200 at period end"),
    Metric("payables", "Trade payables", "BALANCE", "INR", ("2000",), lambda c, a, b, f: R(c.bal_liab(("2000",), b)),
           "closing balance of account 2000 at period end"),
    Metric("borrowings", "Borrowings (debt)", "BALANCE", "INR", ("2500",), lambda c, a, b, f: R(c.bal_liab(("2500",), b)),
           "closing balance of long-term borrowings (2500)"),
    Metric("ppe_net", "Net property, plant and equipment", "BALANCE", "INR", ("1500", "1510"),
           lambda c, a, b, f: R(c.bal_asset(("1500", "1510"), b)), "gross PP&E − accumulated depreciation at period end"),
    Metric("total_assets", "Total assets", "BALANCE", "INR", ("1000", "1100", "1200", "1500", "1510"),
           lambda c, a, b, f: R(c.total_assets(b)), "cash + receivables + inventories + net PP&E at period end"),
    Metric("total_liabilities", "Total liabilities", "BALANCE", "INR", ("2000", "2300", "2500"),
           lambda c, a, b, f: R(c.total_liabilities(b)), "trade payables + tax payable + borrowings at period end"),
    Metric("equity", "Total shareholders' equity", "BALANCE", "INR", ("3000", "3100", "3200"),
           lambda c, a, b, f: R(c.equity(b)), "share capital + retained earnings (opening + cumulative profit − dividends)"),
    Metric("working_capital", "Working capital", "BALANCE", "INR", ("1000", "1100", "1200", "2000", "2300"),
           lambda c, a, b, f: R(c.current_assets(b) - c.current_liabilities(b)), "current assets − current liabilities"),
    Metric("current_ratio", "Current ratio", "MULTIPLE", "X", ("1000", "1100", "1200", "2000", "2300"),
           lambda c, a, b, f: ratio(c.current_assets(b), c.current_liabilities(b)), "current assets ÷ current liabilities"),
    Metric("debt_to_equity", "Debt-to-equity ratio", "MULTIPLE", "X", ("2500",),
           lambda c, a, b, f: ratio(c.bal_liab(("2500",), b), c.equity(b)), "borrowings ÷ shareholders' equity at period end"),
    Metric("roe", "Return on equity", "RATIO", "PCT", ("3000",),
           lambda c, a, b, f: pct(c.net_income(a, b), c.avg_bal(c.equity, a, b)), "net profit ÷ average equity × 100"),
    Metric("roa", "Return on assets", "RATIO", "PCT", ("1000",),
           lambda c, a, b, f: pct(c.net_income(a, b), c.avg_bal(c.total_assets, a, b)), "net profit ÷ average total assets × 100"),
    Metric("dso", "Days sales outstanding (DSO)", "DAYS", "DAYS", ("1100",) + REVENUE,
           lambda c, a, b, f: days_ratio(c.avg_bal(lambda d: c.bal_asset(("1100",), d), a, b), c.revenue(a, b), a, b),
           "days in period × average trade receivables ÷ revenue"),
    Metric("dpo", "Days payable outstanding (DPO)", "DAYS", "DAYS", ("2000",) + COGS,
           lambda c, a, b, f: days_ratio(c.avg_bal(lambda d: c.bal_liab(("2000",), d), a, b), c.cogs(a, b), a, b),
           "days in period × average trade payables ÷ cost of sales"),
    Metric("inventory_turnover", "Inventory turnover", "MULTIPLE", "X", ("1200",) + COGS,
           lambda c, a, b, f: ratio(c.exp(("5000",), a, b), c.avg_bal(lambda d: c.bal_asset(("1200",), d), a, b)),
           "cost of goods sold ÷ average inventories"),
    Metric("invoice_count", "Number of sales invoices", "COUNT", "COUNT", REVENUE,
           lambda c, a, b, f: Decimal(c.s.count("SALE", a, b, f)), "count of SALE journal entries", dims=True),
    Metric("transaction_count", "Number of journal transactions", "COUNT", "COUNT", (),
           lambda c, a, b, f: Decimal(c.s.count("*", a, b, f)), "count of all journal entries"),
    Metric("average_invoice", "Average invoice value", "FLOW", "INR", REVENUE,
           lambda c, a, b, f: R(Decimal(c.revenue(a, b, f)) / max(1, c.s.count("SALE", a, b, f))),
           "revenue ÷ number of sales invoices", dims=True),
]}

GROUPABLE = {"revenue", "cogs", "gross_profit", "invoice_count", "product_revenue", "service_revenue"}


# ---------------------------------------------------------------------------------------------- sources
class MemorySource:
    """Independent aggregation over the generator's in-memory ledger (gold path)."""

    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.days: dict[tuple[str, date], int] = defaultdict(int)
        self.dim: dict[tuple, int] = defaultdict(int)
        self.cnt: dict[tuple, int] = defaultdict(int)
        self.cf: dict[tuple[str, date], int] = defaultdict(int)
        self.lines_n: dict[tuple[str, date], int] = defaultdict(int)
        kinds = {e.id: e for e in ledger.entries}
        for e in ledger.entries:
            self.cnt[("*", e.d, None, None, None)] += 1
            self.cnt[(e.kind, e.d, e.segment, e.region, e.counterparty)] += 1
        for l in ledger.lines:
            v = l.debit - l.credit
            self.days[(l.account, l.d)] += v
            self.lines_n[(l.account, l.d)] += 1
            if l.segment or l.region or l.counterparty:
                self.dim[(l.account, l.d, l.segment, l.region, l.counterparty)] += v
            if l.account == "1000" and kinds[l.entry_id].cf:
                self.cf[(kinds[l.entry_id].cf, l.d)] += v
        self._sorted: dict[str, tuple[list[date], list[int]]] = {}
        for (acc, d), v in sorted(self.days.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            ds, cs = self._sorted.setdefault(acc, ([], []))
            ds.append(d)
            cs.append((cs[-1] if cs else 0) + v)
        # per-account / per-kind date-sorted indexes for filtered queries
        self._dim_acc: dict[str, tuple[list[date], list[tuple]]] = {}
        for (a, d, s, r, c), v in sorted(self.dim.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            ds, rows = self._dim_acc.setdefault(a, ([], []))
            ds.append(d)
            rows.append((s, r, c, v))
        self._cnt_kind: dict[str, tuple[list[date], list[tuple]]] = {}
        for (k, d, s, r, c), v in sorted(self.cnt.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            ds, rows = self._cnt_kind.setdefault(k, ([], []))
            ds.append(d)
            rows.append((s, r, c, v))

    @staticmethod
    def _match(f, s, r, c):
        return (not f.segment or s == f.segment) and (not f.region or r == f.region) and (not f.counterparty or c == f.counterparty)

    def _range(self, idx, key, start, end):
        ds, rows = idx.get(key, ([], []))
        from bisect import bisect_left

        return rows[bisect_left(ds, start):bisect_right(ds, end)]

    def flow(self, accounts, start, end, f=Filters()):
        if f.any():
            return sum(v for a in accounts for (s, r, c, v) in self._range(self._dim_acc, a, start, end) if self._match(f, s, r, c))
        return sum(self.balance((a,), end) - self.balance((a,), start - timedelta(days=1)) for a in accounts)

    def balance(self, accounts, as_of):
        tot = 0
        for a in accounts:
            ds, cs = self._sorted.get(a, ([], []))
            i = bisect_right(ds, as_of)
            tot += cs[i - 1] if i else 0
        return tot

    def count(self, kind, start, end, f=Filters()):
        return sum(v for (s, r, c, v) in self._range(self._cnt_kind, kind, start, end)
                   if self._match(f, s, r, c) and (kind != "*" or s is None))

    def cash_by_category(self, category, start, end):
        return sum(v for (c, d), v in self.cf.items() if c == category and start <= d <= end)

    def group_flow(self, accounts, start, end, dim, f=Filters()):
        out: dict[str, int] = defaultdict(int)
        idx = {"segment": 0, "region": 1, "counterparty": 2}[dim]
        for a in accounts:
            for row in self._range(self._dim_acc, a, start, end):
                if row[idx] and self._match(Filters(f.segment, f.region), row[0], row[1], row[2]):
                    out[row[idx]] += row[3]
        return dict(out)

    def line_count(self, accounts, start, end, f=Filters()):
        return sum(v for (a, d), v in self.lines_n.items() if a in accounts and start <= d <= end)


class SQLSource:
    """PostgreSQL aggregation over fin_journal_lines / fin_journal_entries for one organization."""

    def __init__(self, db, org_id) -> None:
        self.db, self.org = db, org_id
        self._cache: dict = {}

    def _q(self, sql: str, **params):
        from sqlalchemy import text

        key = (sql, tuple(sorted((k, str(v)) for k, v in params.items())))
        if key not in self._cache:
            self._cache[key] = self.db.execute(text(sql), {"o": self.org, **params}).all()
        return self._cache[key]

    @staticmethod
    def _dims(f: Filters, alias: str = "l") -> str:
        sql = ""
        if f.segment:
            sql += f" AND {alias}.segment = :seg"
        if f.region:
            sql += f" AND {alias}.region = :reg"
        if f.counterparty:
            sql += f" AND {alias}.counterparty = :cp"
        return sql

    @staticmethod
    def _dp(f: Filters) -> dict:
        return {k: v for k, v in (("seg", f.segment), ("reg", f.region), ("cp", f.counterparty)) if v}

    def flow(self, accounts, start, end, f=Filters()):
        if not accounts:
            return 0
        r = self._q("SELECT COALESCE(SUM(debit - credit), 0) FROM fin_journal_lines l WHERE l.org_id = :o "
                    "AND l.account_code = ANY(:acc) AND l.entry_date BETWEEN :a AND :b" + self._dims(f),
                    acc=list(accounts), a=start, b=end, **self._dp(f))
        return int(Decimal(r[0][0]) * 100)

    def balance(self, accounts, as_of):
        r = self._q("SELECT COALESCE(SUM(debit - credit), 0) FROM fin_journal_lines l WHERE l.org_id = :o "
                    "AND l.account_code = ANY(:acc) AND l.entry_date <= :d", acc=list(accounts), d=as_of)
        return int(Decimal(r[0][0]) * 100)

    def count(self, kind, start, end, f=Filters()):
        sql = "SELECT COUNT(*) FROM fin_journal_entries e WHERE e.org_id = :o AND e.entry_date BETWEEN :a AND :b"
        if kind != "*":
            sql += " AND e.kind = :k"
        r = self._q(sql + self._dims(f, "e"), a=start, b=end, k=kind, **self._dp(f))
        return int(r[0][0])

    def cash_by_category(self, category, start, end):
        r = self._q("SELECT COALESCE(SUM(l.debit - l.credit), 0) FROM fin_journal_lines l JOIN fin_journal_entries e "
                    "ON e.id = l.entry_id WHERE l.org_id = :o AND l.account_code = '1000' AND e.cf_category = :c "
                    "AND l.entry_date BETWEEN :a AND :b", c=category, a=start, b=end)
        return int(Decimal(r[0][0]) * 100)

    def group_flow(self, accounts, start, end, dim, f=Filters()):
        col = {"segment": "segment", "region": "region", "counterparty": "counterparty"}[dim]
        rows = self._q(f"SELECT l.{col}, SUM(debit - credit) FROM fin_journal_lines l WHERE l.org_id = :o "
                       f"AND l.account_code = ANY(:acc) AND l.entry_date BETWEEN :a AND :b AND l.{col} IS NOT NULL"
                       + self._dims(f) + f" GROUP BY l.{col}", acc=list(accounts), a=start, b=end, **self._dp(f))
        return {k: int(Decimal(v) * 100) for k, v in rows}

    def line_count(self, accounts, start, end, f=Filters()):
        if not accounts:
            return 0
        r = self._q("SELECT COUNT(*) FROM fin_journal_lines l WHERE l.org_id = :o AND l.account_code = ANY(:acc) "
                    "AND l.entry_date BETWEEN :a AND :b" + self._dims(f), acc=list(accounts), a=start, b=end, **self._dp(f))
        return int(r[0][0])

    def sample_refs(self, accounts, start, end, f=Filters(), n=3) -> list[str]:
        if not accounts:
            return []
        rows = self._q("SELECT e.doc_ref FROM fin_journal_lines l JOIN fin_journal_entries e ON e.id = l.entry_id "
                       "WHERE l.org_id = :o AND l.account_code = ANY(:acc) AND l.entry_date BETWEEN :a AND :b"
                       + self._dims(f) + " ORDER BY l.entry_date LIMIT :n", acc=list(accounts), a=start, b=end, n=n,
                       **self._dp(f))
        return [r[0] for r in rows]
