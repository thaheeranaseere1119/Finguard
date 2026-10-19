"""XYZ Ltd synthetic ledger, metric engine, period parsing and question understanding (no database)."""

from datetime import date
from decimal import Decimal

import pytest

from finguard.xyz.coa import DATA_END, DATA_START, fy_bounds, fy_of
from finguard.xyz.generator import generate
from finguard.xyz.metrics import Calc, Filters, MemorySource, METRICS
from finguard.xyz.periods import find_periods
from finguard.xyz.qa import answer, scope_check, understand


@pytest.fixture(scope="module")
def ledger():
    return generate()


@pytest.fixture(scope="module")
def calc(ledger):
    return Calc(MemorySource(ledger))


def test_ledger_is_balanced_deterministic_and_covers_120_months(ledger):
    assert sum(l.debit for l in ledger.lines) == sum(l.credit for l in ledger.lines)
    by_entry = {}
    for l in ledger.lines:
        d, c = by_entry.get(l.entry_id, (0, 0))
        by_entry[l.entry_id] = (d + l.debit, c + l.credit)
    assert all(d == c for d, c in by_entry.values())
    days = {e.d for e in ledger.entries}
    assert min(days) == DATA_START and max(days) == DATA_END
    assert len({(d.year, d.month) for d in days}) == 120
    assert len(days) >= 3600                       # transactions on (almost) every day of 10 years
    again = generate()
    assert len(again.entries) == len(ledger.entries) and again.lines[1000] == ledger.lines[1000]


def test_accounting_identity_holds_every_year_end(calc):
    for fy in range(2017, 2027):
        _, end = fy_bounds(fy)
        assets = calc.total_assets(end)
        assert assets == calc.total_liabilities(end) + calc.equity(end), fy


def test_cash_never_negative(ledger):
    bal, by_day = 0, {}
    for l in ledger.lines:
        if l.account == "1000":
            by_day[l.d] = by_day.get(l.d, 0) + l.debit - l.credit
    for d in sorted(by_day):
        bal += by_day[d]
        assert bal > 0, d


def test_metric_relationships(calc):
    a, b = fy_bounds(2024)
    f = Filters()
    rev, cogs = METRICS["revenue"].fn(calc, a, b, f), METRICS["cogs"].fn(calc, a, b, f)
    assert METRICS["gross_profit"].fn(calc, a, b, f) == rev - cogs
    seg_total = sum(METRICS["revenue"].fn(calc, a, b, Filters(segment=s)) for s in
                    ("Enterprise Software", "Cloud Services", "Hardware Solutions"))
    assert seg_total == rev
    q_total = sum(METRICS["revenue"].fn(calc, *__import__("finguard.xyz.coa", fromlist=["quarter_bounds"]).quarter_bounds(2024, q), f)
                  for q in range(1, 5))
    assert q_total == rev
    assert METRICS["fcf"].fn(calc, a, b, f) == METRICS["cfo"].fn(calc, a, b, f) - METRICS["capex"].fn(calc, a, b, f)


@pytest.mark.parametrize("text,labels", [
    ("revenue in FY2024", ["FY2024"]), ("in FY24", ["FY2024"]), ("FY 2022-23", ["FY2023"]), ("2021-22", ["FY2022"]),
    ("Q3 FY2023", ["Q3 FY2023"]), ("the second quarter of FY2020", ["Q2 FY2020"]), ("March 2021", ["March 2021"]),
    ("on 15 August 2019", ["15 Aug 2019"]), ("August 15, 2019", ["15 Aug 2019"]), ("from FY2018 to FY2020", ["FY2018", "FY2020"]),
])
def test_period_parsing(text, labels):
    assert [p.label for p in find_periods(text)] == labels


def test_fiscal_year_convention():
    assert fy_of(date(2023, 4, 1)) == 2024 and fy_of(date(2024, 3, 31)) == 2024
    q = find_periods("Q4 FY2024")[0]
    assert (q.start, q.end) == (date(2024, 1, 1), date(2024, 3, 31))


def test_scope_and_range_guards(calc):
    assert scope_check("What was 3M's capex in FY2018?")
    assert scope_check("Compare with Wipro Limited")
    assert scope_check("What was XYZ Ltd's revenue in FY2024?") is None
    assert scope_check("How much did Orion Retail buy from us?") is None     # an XYZ customer, not an external company
    assert answer(calc, "What was Apple's revenue in FY2022?").status == "OUT_OF_SCOPE"
    assert answer(calc, "What was revenue in FY2012?").status == "NO_DATA"
    assert answer(calc, "Hello there").status == "CLARIFY"


def test_understanding_and_exact_answers(calc):
    a = answer(calc, "What was XYZ Ltd's revenue in FY2024?")
    a2, b2 = fy_bounds(2024)
    assert a.status == "ANSWERED" and a.value == METRICS["revenue"].fn(calc, a2, b2, Filters())
    p = understand("Why did operating margin change in FY2022 compared with FY2021?")
    assert p.op == "DRIVERS" and [x.label for x in p.periods] == ["FY2021", "FY2022"]
    p = understand("What is the 3 year average capex for FY2017 - FY2019?")
    assert p.op == "AVERAGE" and [x.label for x in p.periods] == ["FY2017", "FY2018", "FY2019"]
    p = understand("What was software expense in May 2021?")
    assert p.metric == "it_expense" and not p.filters.any()
    p = understand("How much did Delta Steel contribute in revenue in FY2020?")
    assert p.op == "VALUE" and p.filters.counterparty == "Delta Steel"
    t = answer(calc, "Top 3 customers by revenue in FY2023")
    assert t.plan.k == 3 and len(t.value) == 3 and t.value[0][1] >= t.value[1][1] >= t.value[2][1]


def test_relative_windows_and_series(calc):
    a = answer(calc, "Give me the last 10 months revenue")
    assert a.status == "ANSWERED" and a.plan.op == "SERIES" and len(a.value) == 10
    assert a.value[0][0] == "June 2025" and a.value[-1][0] == "March 2026"
    assert "latest data" in a.text
    avg = answer(calc, "Average monthly sales over the past 6 months")
    assert avg.value == (sum((v for _, v in answer(calc, "revenue for the last 6 months").value), Decimal(0)) / 6).quantize(Decimal("0.01"))
    assert len(answer(calc, "Revenue for the last 4 quarters").value) == 4
