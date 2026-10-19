"""Company-records Q&A (transactions, customers/vendors, profile, coverage) — every fact re-derived from the raw ledger."""

from collections import defaultdict
from decimal import Decimal

import pytest

from finguard.xyz.coa import CUSTOMERS, DATA_END, fy_bounds
from finguard.xyz.generator import generate
from finguard.xyz.metrics import Calc, MemorySource
from finguard.xyz.qa import answer
from finguard.xyz.records import detect


@pytest.fixture(scope="module")
def ledger():
    return generate()


@pytest.fixture(scope="module")
def calc(ledger):
    return Calc(MemorySource(ledger))


def _amounts(ledger):
    amt = defaultdict(int)
    for l in ledger.lines:
        if l.account not in ("5000", "5100"):
            amt[l.entry_id] += l.debit
    return amt


@pytest.mark.parametrize("q,intent", [
    ("what was the recent transaction made?", "TXNS"), ("Show the last 5 transactions", "TXNS"),
    ("What was the largest transaction in FY2025?", "TXNS"), ("Transactions on 15 August 2019", "TXNS"),
    ("recent customer receipts", "TXNS"), ("When was the last dividend paid?", "TXNS"), ("Any big purchases last month?", "TXNS"),
    ("Details of OB-20160401", "DOC"), ("Tell me about Orion Retail", "PARTY"), ("Which customers owe us the most?", "OUTSTANDING"),
    ("How much do we owe vendors?", "OUTSTANDING"), ("Tell me about XYZ Ltd", "PROFILE"), ("how is the company doing", "PROFILE"),
    ("What are the business segments?", "PROFILE"), ("List all customers", "PARTIES"), ("Who are our suppliers?", "PARTIES"),
    ("What data do you have?", "COVERAGE"), ("Show the chart of accounts", "ACCOUNTS"), ("Types of transactions in FY2024", "KINDS"),
    ("Who is the CEO of XYZ?", "NOT_RECORDED"),
])
def test_record_questions_are_routed(calc, q, intent):
    a = answer(calc, q)
    assert a.structured["plan"]["operation"] == f"RECORDS:{intent}", (q, a.structured["plan"])


@pytest.mark.parametrize("q", [
    "What was XYZ Ltd's revenue in FY2024?", "Give me the last 10 months revenue", "How many transactions in FY2024?",
    "Cash flow from operating activities in FY2023", "What was the interest coverage ratio in Q1 FY2021?",
    "What is XYZ Ltd FY2021 days payable outstanding (DPO)?", "How much did the Hardware Solutions segment earn in FY2020?",
    "Top 5 customers by revenue in FY2025", "Total sales on 15 August 2019",
])
def test_metric_questions_stay_with_the_metric_engine(calc, q):
    a = answer(calc, q)
    assert a.status == "ANSWERED" and a.plan is not None and a.plan.metric, q


def test_most_recent_and_largest_transactions_match_the_ledger(calc, ledger):
    amt = _amounts(ledger)
    last = max(ledger.entries, key=lambda e: (e.d, e.id))
    a = answer(calc, "what was the recent transaction made?")
    assert a.status == "ANSWERED" and a.value[0][1] == last.doc_ref and last.d == DATA_END
    table = next(b for b in a.structured["blocks"] if b["type"] == "table")
    assert len(table["rows"]) == 10 and table["columns"][0] == "Date"
    s, e = fy_bounds(2025)
    big = max((x for x in ledger.entries if s <= x.d <= e), key=lambda x: (amt[x.id], x.d, x.id))
    b = answer(calc, "What was the largest transaction in FY2025?")
    assert b.value[0][1] == big.doc_ref and Decimal(b.value[0][5]) == Decimal(amt[big.id]) / 100
    vp = answer(calc, "Show the 5 largest vendor payments in FY2025")
    assert len(vp.value) == 5 and all(r[2] == "VENDOR_PAYMENT" for r in vp.value)
    assert [Decimal(r[5]) for r in vp.value] == sorted((Decimal(r[5]) for r in vp.value), reverse=True)
    day = answer(calc, "Transactions on 15 August 2019")
    assert all(r[0] == "2019-08-15" for r in day.value)


def test_party_balances_reconcile_to_the_balance_sheet(calc, ledger):
    """Every receivable/payable line carries its counterparty, so per-party balances add up to the ledger totals."""
    recv = sum(l.debit - l.credit for l in ledger.lines if l.account == "1100")
    pay = sum(l.credit - l.debit for l in ledger.lines if l.account == "2000")
    assert all(l.counterparty for l in ledger.lines if l.account in ("1100", "2000"))
    from finguard.xyz.records import store_for

    st = store_for(calc)
    assert sum(st.party_balances("1100", DATA_END).values()) == recv
    assert -sum(st.party_balances("2000", DATA_END).values()) == pay
    assert all(v >= 0 for v in st.party_balances("1100", DATA_END).values())   # no customer is over-collected
    o = answer(calc, "Which customers owe us the most?")
    hl = o.structured["blocks"][0]["items"]
    assert o.status == "ANSWERED" and hl[0]["label"] == "Total outstanding"


def test_customer_profile_and_company_profile(calc, ledger):
    a = answer(calc, "Tell me about Orion Retail")
    amt = _amounts(ledger)
    invoiced = sum(amt[e.id] for e in ledger.entries if e.kind == "SALE" and e.counterparty == "Orion Retail")
    assert a.status == "ANSWERED" and "customer" in a.text
    hl = {h["label"]: h for h in a.structured["blocks"][0]["items"]}
    from finguard.xyz.qa import fmt_short

    assert hl["Invoiced"]["value"] == fmt_short(Decimal(invoiced) / 100, "INR")
    p = answer(calc, "Tell me about XYZ Ltd")
    assert p.status == "ANSWERED" and "Enterprise Software" in p.text and p.structured["limitations"]
    c = answer(calc, "List all customers")
    assert len(next(b for b in c.structured["blocks"] if b["type"] == "table")["rows"]) == len(CUSTOMERS)


def test_unrecorded_facts_and_missing_periods(calc):
    assert answer(calc, "Who is the CEO of XYZ?").status == "NO_DATA"           # never guessed
    assert answer(calc, "Hello there").status == "CLARIFY"
    assert answer(calc, "Transactions in FY2030").status == "NO_DATA"
    r = answer(calc, "What is our revenue?")                                      # no period → latest FY, stated
    assert r.status == "ANSWERED" and r.plan.periods[0].label == "FY2026" and "no period given" in r.text
    c = answer(calc, "What is the cash balance?")
    assert c.status == "ANSWERED" and c.plan.periods[0].end == DATA_END


def test_detect_defers_to_a_confident_metric():
    assert detect("What was the largest transaction in FY2025?", None).intent == "TXNS"
    assert detect("What were sales in FY2024?", "revenue") is None
    assert detect("What is XYZ Ltd FY2021 days payable outstanding?", "dpo") is None


def test_spending_breakdown_matches_the_ledger(calc):
    from finguard.xyz.metrics import METRICS, Filters
    from finguard.xyz.coa import fy_bounds as fb

    a = answer(calc, "Give me the spending details in each category for FY2024")
    assert a.structured["plan"]["operation"] == "RECORDS:SPENDING"
    s, e = fb(2024)
    cats = dict((n, Decimal(v)) for n, v in a.value)
    assert cats["Hardware & products (COGS)"] + cats["Cloud services (cost of services)"] == METRICS["cogs"].fn(calc, s, e, Filters())
    assert cats["Employee costs (salaries)"] == METRICS["employee_expense"].fn(calc, s, e, Filters())
    table = next(b for b in a.structured["blocks"] if b["type"] == "table" and "categor" in b["title"])
    assert table["columns"][:4] == ["Category", "Group", "Account", "Amount"] and table["rows"][0][5]   # vs previous FY
    payees = next(b for b in a.structured["blocks"] if b["type"] == "table" and "payees" in b["title"].lower())
    assert payees["rows"]


@pytest.mark.parametrize("q,op", [
    ("Expense breakdown for March 2026", "RECORDS:SPENDING"), ("How much did we spend on each department last quarter?", "RECORDS:SPENDING"),
    ("Spending by segment in FY2025", "RECORDS:SPENDING"), ("Where did the money go in FY2021?", "RECORDS:SPENDING"),
    ("What was marketing spend in FY2023?", "VALUE"), ("How much did we spend on marketing each month in FY2023?", "SERIES"),
])
def test_spending_questions_route_correctly(calc, q, op):
    assert answer(calc, q).structured["plan"]["operation"] == op


def test_monthly_spending_compares_with_the_previous_calendar_month(calc):
    a = answer(calc, "Expense breakdown for March 2026")
    row = next(r for r in next(b for b in a.structured["blocks"] if b["type"] == "table")["rows"] if r[0].startswith("Employee"))
    assert abs(float(row[5].rstrip("%"))) < 5                      # one payroll run vs one payroll run, not two


# ---------------------------------------------------------------------------------------------- transaction conditions
def _kv(a):
    return dict(next(b for b in a.structured["blocks"] if b.get("title") == "Understood as")["items"])


def test_bare_transaction_question_gets_an_overview_not_an_arbitrary_list(calc, ledger):
    for q in ("Transaction", "transactions", "show me transactions", "gimme all transactions"):
        a = answer(calc, q)
        assert a.status == "ANSWERED" and a.structured["title"] == "Transactions overview", q
        assert f"{len(ledger.entries):,}" in a.text


def test_domain_question_returns_that_domain_with_true_count_and_total(calc, ledger):
    from finguard.xyz.txnfilter import HARDWARE_VENDORS

    amt = _amounts(ledger)
    want = [e for e in ledger.entries if e.segment == "Hardware Solutions" or e.counterparty in HARDWARE_VENDORS]
    a = answer(calc, "gimme all transactions related to hardware and products")
    assert a.status == "ANSWERED" and f"{len(want):,} Hardware & products transactions" in a.text
    assert len(a.value) == 100                         # "all" → a long page, with the full count and total stated
    refs = {e.doc_ref for e in want}
    assert all(r[1] in refs for r in a.value)
    hl = a.structured["blocks"][0]["items"]
    from finguard.xyz.records import _fmt

    assert hl[1]["value"] == _fmt(sum(amt[e.id] for e in want))
    assert "Hardware" in _kv(a)["Domain"]


def test_amount_period_party_and_category_conditions_are_all_applied(calc, ledger):
    amt = _amounts(ledger)
    s, e = fy_bounds(2025)
    a = answer(calc, "how many transactions above 1 crore in FY2025")
    want = [x for x in ledger.entries if s <= x.d <= e and amt[x.id] >= 10**7 * 100]
    assert a.text.startswith(f"{len(want):,} transactions") and "at least" in _kv(a)["Amount"]
    o = answer(calc, "sales to Orion Retail between 5 lakh and 10 lakh")
    want = [x for x in ledger.entries if x.kind == "SALE" and x.counterparty == "Orion Retail" and 5 * 10**7 <= amt[x.id] <= 10**8]
    assert f"{len(want):,} sales invoices with Orion Retail" in o.text
    m = answer(calc, "marketing transactions in FY2023")
    ids = {l.entry_id for l in ledger.lines if l.account == "6300" and l.debit > 0}
    s, e = fy_bounds(2023)
    assert len(m.value) == len([x for x in ledger.entries if x.id in ids and s <= x.d <= e]) and m.value
    assert all("Brightline" in (r[3] or "") for r in m.value)


def test_unknown_words_are_matched_to_the_ledger_or_reported_never_ignored(calc):
    p = answer(calc, "transactions related to packaging in FY2025")
    assert all(r[3] == "Dune Packaging" for r in p.value) and "Dune Packaging" in _kv(p)["Matched your words"]
    x = answer(calc, "transactions related to bitcoin")
    assert x.status == "CLARIFY" and "bitcoin" in x.text and not x.value
    two = answer(calc, "transactions with Crest Semiconductors and Alpha Components")
    assert {r[3] for r in two.value} <= {"Crest Semiconductors", "Alpha Components"}
    c = answer(calc, "how many hardware transactions in FY2025")
    assert c.structured["plan"]["operation"] == "RECORDS:TXNS" and "Hardware" in c.text


def test_counting_filtered_transactions_counts_entries_not_the_expense_amount(calc, ledger):
    from datetime import date

    ids = {l.entry_id for l in ledger.lines if l.account == "6400" and l.debit > 0}
    n = len([e for e in ledger.entries if e.id in ids and date(2026, 3, 1) <= e.d <= date(2026, 3, 31)])
    a = answer(calc, "how many travel transactions in March 2026")
    assert a.structured["plan"]["operation"] == "RECORDS:TXNS" and a.text.startswith(f"{n:,} Travel transactions")
