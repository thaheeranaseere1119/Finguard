"""Synthetic XYZ Ltd company knowledge base ("company log dataset") for retrieval-augmented answering.

Every document is generated FROM the same deterministic 10-year ledger (finguard.xyz.generator), so narrative facts
(monthly revenue, top customers, big transactions, cash, dividends, the 2020 loan, capex) agree with the books.
Facts the ledger cannot contain — leadership, headcount, policies, board decisions, operational events, customer
relationship notes — are synthetic but internally consistent (e.g. headcount follows the payroll model, the dividend
policy matches the dividends actually paid, vendor terms match the 30–45 day payment lag in the ledger).

Document types (doc_type):
  company_profile · policy · monthly_log · board_minutes · customer_dossier · vendor_dossier · daily_transactions ·
  event_log
Output: a list of {doc_id, doc_type, title, period_start, period_end, entities, text} — written as JSONL by the CLI.
All names, people and events are fictitious.
"""

from __future__ import annotations

import calendar
import random
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from finguard.xyz.coa import (ACCOUNTS, COMPANY, CUSTOMERS, DATA_END, DATA_START, FIRST_FY, LAST_FY, OPEX_COUNTERPARTIES,
                              REGIONS, SEGMENTS, VENDORS, fy_bounds, fy_of, quarter_bounds)

LEADERS = [
    ("Arjun Mehta", "Managing Director & CEO", 2009, "co-founder; previously led enterprise sales at a global IT services firm"),
    ("Nandini Shah", "Chief Financial Officer", 2014, "chartered accountant; owns treasury, tax and investor relations"),
    ("Farhan Qureshi", "Chief Operating Officer", 2012, "runs delivery, procurement and the hardware supply chain"),
    ("Lakshmi Narayanan", "Chief Technology Officer", 2011, "co-founder; leads the cloud platform and product engineering"),
    ("Devika Pillai", "Head of Human Resources", 2016, "joined in FY2017 to scale hiring beyond 400 people"),
    ("Sanjay Bhatt", "Head of Sales — Export", 2018, "built the Singapore and Dubai channel partnerships"),
]
BOARD = ["Arjun Mehta (Chair)", "Lakshmi Narayanan", "Nandini Shah", "Justice (retd.) R. K. Menon (Independent)",
         "Priya Sethi (Independent)"]
ACCOUNT_MANAGERS = ["Kiran Rao", "Pooja Iyer", "Rahul Desai", "Sneha Kulkarni", "Imran Sheikh", "Anita George"]


def _rs(paise: int | Decimal) -> str:
    v = (Decimal(paise) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    if abs(v) >= Decimal(10_000_000):
        return f"₹{(v / Decimal(10_000_000)).quantize(Decimal('0.01'))} crore"
    if abs(v) >= Decimal(100_000):
        return f"₹{(v / Decimal(100_000)).quantize(Decimal('0.01'))} lakh"
    return f"₹{v:,}"


def _dt(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


def headcount(d: date) -> int:
    """Consistent with the payroll model in the generator (380 people at the start, +6% a year)."""
    return round(380 * 1.06 ** ((d - DATA_START).days / 365.25))


def _doc(doc_id, doc_type, title, text, start=None, end=None, entities=()):
    return {"doc_id": doc_id, "doc_type": doc_type, "title": title, "period_start": start.isoformat() if start else None,
            "period_end": end.isoformat() if end else None, "entities": sorted(set(entities)), "text": text.strip()}


# ------------------------------------------------------------------------------------------------ ledger facts
class Facts:
    """Single pass over the ledger: per-month / per-day / per-party aggregates (paise)."""

    def __init__(self, ledger) -> None:
        amt = defaultdict(int)
        rev = defaultdict(int)
        cogs = defaultdict(int)
        for l in ledger.lines:
            if l.account not in ("5000", "5100"):
                amt[l.entry_id] += l.debit
            if l.account in ("4000", "4100"):
                rev[l.entry_id] += l.credit - l.debit
            if l.account in ("5000", "5100"):
                cogs[l.entry_id] += l.debit - l.credit
        self.entries = ledger.entries
        self.amt = amt
        self.month = defaultdict(lambda: defaultdict(int))
        self.month_cust = defaultdict(lambda: defaultdict(int))
        self.month_seg = defaultdict(lambda: defaultdict(int))
        self.month_big = defaultdict(list)
        self.day = defaultdict(list)
        self.party = defaultdict(lambda: {"first": None, "last": None, "kinds": defaultdict(lambda: [0, 0]),
                                          "segments": defaultdict(int), "region": None, "fy": defaultdict(int)})
        cash = 0
        cash_end = {}
        cash_line = defaultdict(int)
        for l in ledger.lines:
            if l.account == "1000":
                cash_line[l.d] += l.debit - l.credit
        for d in sorted(cash_line):
            cash += cash_line[d]
            cash_end[(d.year, d.month)] = cash
        self.cash_end = cash_end
        for e in ledger.entries:
            k = (e.d.year, e.d.month)
            m = self.month[k]
            m["n"] += 1
            m[e.kind] += 1
            m[f"amt_{e.kind}"] += amt[e.id]
            if e.kind == "SALE":
                m["revenue"] += rev[e.id]
                m["cogs"] += cogs[e.id]
                self.month_cust[k][e.counterparty] += rev[e.id]
                self.month_seg[k][e.segment] += rev[e.id]
            if e.kind in ("CAPEX", "LOAN_DRAWDOWN", "DIVIDEND", "TAX_PAYMENT") or amt[e.id] >= 2 * 10**9:
                self.month_big[k].append(e)
            self.day[e.d].append(e)
            if e.counterparty:
                p = self.party[e.counterparty]
                p["first"] = p["first"] or e.d
                p["last"] = e.d
                p["kinds"][e.kind][0] += 1
                p["kinds"][e.kind][1] += amt[e.id]
                if e.kind == "SALE":
                    p["segments"][e.segment] += rev[e.id]
                    p["region"] = e.region
                    p["fy"][fy_of(e.d)] += rev[e.id]


# ------------------------------------------------------------------------------------------------ documents
def company_profile(f: Facts) -> dict:
    hc0, hc1 = headcount(DATA_START), headcount(DATA_END)
    lead = "\n".join(f"- {n} — {r} (with XYZ since {y}); {bio}." for n, r, y, bio in LEADERS)
    text = f"""
# XYZ Ltd — company profile

XYZ Ltd is a privately held Indian technology company founded in 2009 by Arjun Mehta and Lakshmi Narayanan. It is
registered in Bengaluru, Karnataka, with its head office at the Metro Estates business park (leased from Metro Estates
Pvt Ltd) and sales offices in Mumbai, Delhi NCR, Chennai and Kolkata. Export sales are served through channel partners
in Singapore and Dubai.

## What the company does
XYZ Ltd sells to mid-size and large Indian businesses through three business segments:
- Enterprise Software — licences, implementation and support for ERP, analytics and workflow products. Highest margin
  segment (cost of sales about 12% of revenue).
- Cloud Services — managed hosting, data platforms and SaaS operations, run on capacity bought from Echo Cloud Infra
  and Krypton Data Centres (cost of sales about 45% of revenue).
- Hardware Solutions — servers, networking and end-user devices with installation; largest segment by revenue but
  lowest margin (cost of sales about 68%). Inventory is bought weekly from component suppliers.
Customers are spread across five regions: North, South, East, West and Export.

## Leadership
{lead}

## Board of directors
{", ".join(BOARD)}. The board meets every quarter; minutes are kept by the company secretary.

## People
Headcount was about {hc0} employees in April 2016 and about {hc1} by March 2026, growing roughly 6% a year. Salaries
are paid on the last day of each month.

## Banking, auditors and advisers
- Banker and lender: Coral Bank (current accounts, fixed deposits and term loans). Coral Bank is also a customer of the
  Cloud Services segment.
- Statutory auditors: Rao & Associates LLP (also provide quarterly legal and consulting support).
- Advertising agency: Brightline Advertising. Travel desk: SkyWays Travel. Software subscriptions: CodeForge Software.
- Facilities maintenance: FixIt Services. Electricity: State Electricity Board.

## Financial calendar
Fiscal year runs from 1 April to 31 March (Indian FY). Advance tax is paid quarterly. The final dividend for a year is
paid on 20 September of the following year. The books used by FinGuard cover {_dt(DATA_START)} to {_dt(DATA_END)}
(FY{FIRST_FY}–FY{LAST_FY}).
"""
    return _doc("profile", "company_profile", "XYZ Ltd company profile", text, DATA_START, DATA_END,
                [n for n, *_ in LEADERS] + ["Coral Bank", "Rao & Associates LLP", "Metro Estates Pvt Ltd"])


def policies() -> list[dict]:
    P = [
        ("policy-expense", "Expense and travel policy", """
# Expense and travel policy (v4, approved by the board on 25 June 2021)
- Domestic travel is booked only through SkyWays Travel. Economy class for flights under 4 hours; business class needs
  CFO approval.
- Hotel cap: ₹7,500 per night in metro cities, ₹5,000 elsewhere. Per diem: ₹1,500 per day.
- Client entertainment above ₹25,000 per event needs prior approval from the business head.
- Claims must be filed within 30 days with receipts; claims are reimbursed with the next salary.
- During the COVID-19 lockdown (April–August 2020) all non-essential travel was suspended; travel spend fell by about 90%.
"""),
        ("policy-procurement", "Procurement and vendor payment policy", """
# Procurement and vendor payment policy (v3, effective 1 April 2019)
- Inventory for Hardware Solutions is replenished every Monday based on the previous week's cost of sales.
- Approved component suppliers: Alpha Components, Beacon Supplies, Crest Semiconductors, Dune Packaging, Helix Hardware.
- Standard vendor payment terms are 30 to 45 days from the invoice date. Cloud capacity bills from Echo Cloud Infra and
  Krypton Data Centres are paid on the 10th of the month after next.
- Any single purchase order above ₹2 crore needs COO and CFO sign-off; capital expenditure above ₹5 crore needs board
  approval.
- New vendors must pass KYC and bank-account verification before the first payment.
"""),
        ("policy-credit", "Credit and collection policy", """
# Credit and collection policy (v5, effective 1 April 2021)
- Standard customer credit terms are 45 days. Strategic accounts (top 10 by revenue) may get up to 60 days.
- Target days sales outstanding (DSO) is under 45 days. During FY2021 (COVID-19) DSO rose to about 70 days and
  collection calls were moved to weekly.
- Overdue accounts above 90 days are escalated to the CFO; no new hardware orders are shipped to customers more than
  120 days overdue.
- Bad debts are written off only with board approval (none were written off in FY2017–FY2026).
"""),
        ("policy-treasury", "Treasury and dividend policy", """
# Treasury and dividend policy
- Surplus cash is kept in Coral Bank fixed deposits, earning about 4% a year; interest is booked monthly.
- The company maintains at least ₹20 crore of cash at all times.
- Dividend policy: the board declares a final dividend of 20% of the previous fiscal year's net profit, paid on
  20 September. No interim dividends are paid.
- Borrowings: term loans from Coral Bank carry about 9.5% interest and are repaid ₹75 lakh per quarter.
- In May 2020 the company drew a ₹20 crore COVID-19 liquidity facility from Coral Bank to protect cash during the
  lockdown.
"""),
        ("policy-contractors", "Contractor engagement and payment policy", """
# Contractor engagement and payment policy (FinGuard, effective 1 October 2026)
- Contractors are engaged on written agreements; payment terms are taken from the approved contract only.
- Monthly claims must be backed by a valid invoice. A valid claim up to the contract amount is paid automatically.
- Claims up to 10% above the contract amount need a finance approver. Claims more than 10% above, invalid invoices or a
  change of payout bank account are escalated to the risk officer.
- Payout bank accounts are verified by finance before the first payment.
"""),
        ("policy-tax", "Tax compliance note", """
# Tax compliance note
- Corporate income tax is provided at an effective rate of about 25.17% of profit before tax.
- Advance tax is computed on cumulative profit and provided at the end of June, September, December and March; the
  payment follows about 15 days later.
- TDS is deducted on contractor and professional fees as required by law.
"""),
    ]
    return [_doc(i, "policy", t, txt) for i, t, txt in P]


def monthly_logs(f: Facts) -> list[dict]:
    docs = []
    d = DATA_START
    while d <= DATA_END:
        k = (d.year, d.month)
        m = f.month[k]
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        top = sorted(f.month_cust[k].items(), key=lambda kv: -kv[1])[:3]
        seg = sorted(f.month_seg[k].items(), key=lambda kv: -kv[1])
        gm = (Decimal(m["revenue"] - m["cogs"]) * 100 / m["revenue"]).quantize(Decimal("0.1")) if m["revenue"] else 0
        prev_k = (d.year - 1, d.month)
        yoy = ""
        if f.month[prev_k]["revenue"]:
            ch = (Decimal(m["revenue"] - f.month[prev_k]["revenue"]) * 100 / f.month[prev_k]["revenue"]).quantize(Decimal("0.1"))
            yoy = f" ({'+' if ch >= 0 else ''}{ch}% vs {calendar.month_name[d.month]} {d.year - 1})"
        notes = []
        if d.month in (10, 11):
            notes.append("Festival-season demand lifted hardware and software orders; marketing spend was doubled.")
        if d.month == 3:
            notes.append("Fiscal year-end: customers closed annual budgets, giving the strongest month of the year.")
        if date(2020, 4, 1) <= d <= date(2020, 8, 31):
            notes.append("COVID-19 lockdown: sales fell sharply, offices closed, travel and marketing were cut; "
                         "customers asked for longer payment terms.")
        if d == date(2020, 9, 1):
            notes.append("Business recovering after the lockdown; most customers resumed orders.")
        for e in f.month_big[k]:
            if e.kind == "CAPEX":
                notes.append(f"Capital expenditure {_rs(f.amt[e.id])} on {_dt(e.d)} ({e.doc_ref}, supplier {e.counterparty})"
                             + (" — new data centre build-out" if e.d == date(2022, 3, 8) else
                                " — Bengaluru campus expansion" if e.d == date(2018, 9, 12) else "") + ".")
            elif e.kind == "LOAN_DRAWDOWN":
                notes.append(f"Drew a {_rs(f.amt[e.id])} term loan from {e.counterparty} on {_dt(e.d)} ({e.description}).")
            elif e.kind == "DIVIDEND":
                notes.append(f"Paid the {e.description.lower()} of {_rs(f.amt[e.id])} to shareholders on {_dt(e.d)}.")
            elif e.kind == "TAX_PAYMENT":
                notes.append(f"Advance tax of {_rs(f.amt[e.id])} paid on {_dt(e.d)}.")
        text = f"""
# Monthly operations log — {calendar.month_name[d.month]} {d.year} (FY{fy_of(d)})
Revenue: {_rs(m['revenue'])}{yoy} from {m['SALE']} sales invoices. Gross margin {gm}%.
Segment revenue: {'; '.join(f'{s} {_rs(v)}' for s, v in seg)}.
Top customers: {'; '.join(f'{c} {_rs(v)}' for c, v in top)}.
Collections: {m['RECEIPT']} customer receipts totalling {_rs(m['amt_RECEIPT'])}. Vendor payments: {m['VENDOR_PAYMENT']}
totalling {_rs(m['amt_VENDOR_PAYMENT'])}. Inventory purchases: {m['PURCHASE']} ({_rs(m['amt_PURCHASE'])}).
Payroll: {_rs(m['amt_PAYROLL'])} for about {headcount(last)} employees. Cash and bank at month end: {_rs(f.cash_end.get(k, 0))}.
Total journal entries: {m['n']}.
{chr(10).join('Note: ' + n for n in notes)}
"""
        docs.append(_doc(f"month-{d:%Y-%m}", "monthly_log", f"Operations log {calendar.month_name[d.month]} {d.year}", text,
                         d, last, [c for c, _ in top] + [e.counterparty for e in f.month_big[k] if e.counterparty]))
        d = last + timedelta(days=1)
    return docs


def board_minutes(f: Facts) -> list[dict]:
    rng = random.Random(7)
    docs = []
    for fy in range(FIRST_FY, LAST_FY + 1):
        for q in range(1, 5):
            a, b = quarter_bounds(fy, q)
            months = [k for k in f.month if date(k[0], k[1], 1) >= a and date(k[0], k[1], 1) <= b]
            rev = sum(f.month[k]["revenue"] for k in months)
            pa, pb = quarter_bounds(fy - 1, q)
            prev = sum(f.month[k]["revenue"] for k in f.month if pa <= date(k[0], k[1], 1) <= pb)
            meet = b + timedelta(days=rng.randint(18, 28))
            if meet > DATA_END + timedelta(days=40):
                continue
            items = [f"Reviewed Q{q} FY{fy} results: revenue {_rs(rev)}"
                     + (f", {'up' if rev >= prev else 'down'} {abs(Decimal(rev - prev) * 100 / prev).quantize(Decimal('0.1'))}% year on year" if prev else "") + "."]
            if q == 1 and fy - 1 >= FIRST_FY:
                items.append(f"Approved the audited accounts for FY{fy - 1} presented by Rao & Associates LLP and "
                             f"recommended a final dividend of 20% of FY{fy - 1} net profit, payable on 20 September {a.year}.")
            if fy == 2021 and q == 1:
                items.append("Ratified the ₹20 crore COVID-19 liquidity facility drawn from Coral Bank on 15 May 2020 and "
                             "approved temporary cost measures (travel freeze, marketing cut to 40%, salary deferral "
                             "for senior management).")
            if fy == 2019 and q == 2:
                items.append("Approved ₹6 crore capital expenditure for the Bengaluru campus expansion.")
            if fy == 2022 and q == 4:
                items.append("Approved ₹12 crore capital expenditure for a new data centre to grow Cloud Services.")
            if fy == 2023 and q == 1:
                items.append("Approved a new Mumbai office lease (rent increased by ₹25 lakh per month from April 2022).")
            if fy == 2024 and q == 3:
                items.append("Noted ISO/IEC 27001 recertification of the cloud platform.")
            if fy == 2026 and q == 4:
                items.append("Approved the FY2027 budget and the rollout of FinGuard AI for contractor payment governance.")
            items.append(rng.choice(["Reviewed the top-10 customer concentration and collection status.",
                                     "Discussed the hardware margin and component price trends.",
                                     "Reviewed cyber-security incidents (none material) and the risk register.",
                                     "Reviewed hiring plans with the Head of Human Resources."]))
            text = (f"# Board meeting minutes — {_dt(meet)}\nBoard meeting held on {_dt(meet)} (Q{q} FY{fy}). "
                    f"Present: {', '.join(BOARD)}.\n" + "\n".join(f"- {i}" for i in items))
            docs.append(_doc(f"board-FY{fy}-Q{q}", "board_minutes", f"Board minutes {_dt(meet)} (Q{q} FY{fy})", text, a, b,
                             ["Coral Bank", "Rao & Associates LLP"]))
    return docs


def dossiers(f: Facts) -> list[dict]:
    rng = random.Random(11)
    docs = []
    for i, c in enumerate(CUSTOMERS):
        p = f.party.get(c)
        if not p or not p["first"]:
            continue
        sales = p["kinds"]["SALE"]
        segs = sorted(p["segments"].items(), key=lambda kv: -kv[1])
        best_fy = max(p["fy"].items(), key=lambda kv: kv[1])
        terms = 60 if i < 10 else 45
        text = f"""
# Customer dossier — {c}
Region: {p['region']}. Customer since {_dt(p['first'])}; most recent transaction {_dt(p['last'])}.
Account manager: {ACCOUNT_MANAGERS[i % len(ACCOUNT_MANAGERS)]}. Credit terms: {terms} days{' (strategic account)' if i < 10 else ''}.
Lifetime: {sales[0]} sales invoices worth {_rs(sales[1])}. Segments bought: {'; '.join(f'{s} {_rs(v)}' for s, v in segs)}.
Best year: FY{best_fy[0]} ({_rs(best_fy[1])}).
Notes: {rng.choice(['Pays reliably, usually within terms.', 'Often pays late at quarter end; follow up early.', 'Expanding use of Cloud Services.', 'Hardware refresh expected next fiscal year.', 'Price sensitive; negotiates annual rate cards.'])}
"""
        docs.append(_doc(f"customer-{i:02d}", "customer_dossier", f"Customer dossier: {c}", text, p["first"], p["last"], [c]))
    payees = list(VENDORS) + list(OPEX_COUNTERPARTIES.values())
    for i, v in enumerate(payees):
        p = f.party.get(v)
        if not p or not p["first"]:
            continue
        kinds = "; ".join(f"{k.replace('_', ' ').lower()} {n} ({_rs(a)})" for k, (n, a) in sorted(p["kinds"].items()))
        role = ("component supplier" if v in VENDORS[:4] or v in ("Helix Hardware", "Crest Semiconductors") else
                "cloud capacity provider" if v in ("Echo Cloud Infra", "Krypton Data Centres") else "service provider")
        text = f"""
# Vendor dossier — {v}
Role: {role}. First transaction {_dt(p['first'])}; most recent {_dt(p['last'])}.
Activity: {kinds}.
Payment terms: {'30–45 days' if role == 'component supplier' else '10th of the month after next' if role == 'cloud capacity provider' else 'on invoice'}.
Bank account verified by finance; KYC complete.
"""
        docs.append(_doc(f"vendor-{i:02d}", "vendor_dossier", f"Vendor dossier: {v}", text, p["first"], p["last"], [v]))
    return docs


def daily_transactions(f: Facts, top_n: int = 12) -> list[dict]:
    docs = []
    for d in sorted(f.day):
        es = f.day[d]
        by = defaultdict(lambda: [0, 0])
        for e in es:
            by[e.kind][0] += 1
            by[e.kind][1] += f.amt[e.id]
        biggest = sorted(es, key=lambda e: -f.amt[e.id])[:top_n]
        lines = "\n".join(f"- {e.doc_ref} | {e.kind.replace('_', ' ').lower()} | {e.counterparty or '—'} | {_rs(f.amt[e.id])} | {e.description}"
                          for e in biggest)
        text = (f"# Transaction log — {d:%A} {_dt(d)} (FY{fy_of(d)})\n{len(es)} journal entries: "
                + "; ".join(f"{k.replace('_', ' ').lower()} {n} ({_rs(a)})" for k, (n, a) in sorted(by.items()))
                + f".\nLargest entries:\n{lines}")
        docs.append(_doc(f"day-{d.isoformat()}", "daily_transactions", f"Transaction log {_dt(d)}", text, d, d,
                         [e.counterparty for e in biggest if e.counterparty]))
    return docs


def event_log() -> list[dict]:
    E = [
        (date(2016, 6, 14), "Opened the Chennai sales office with 12 staff."),
        (date(2017, 2, 2), "Launched the managed data-platform offering under Cloud Services."),
        (date(2017, 11, 20), "Signed a three-year enterprise software agreement with Orion Retail, the largest customer."),
        (date(2018, 9, 12), "Completed the ₹6 crore Bengaluru campus expansion (additional 40,000 sq ft)."),
        (date(2019, 4, 1), "Procurement and vendor payment policy v3 came into force (30–45 day vendor terms)."),
        (date(2019, 7, 15), "Appointed Sanjay Bhatt to lead export sales; signed channel partners in Singapore and Dubai."),
        (date(2020, 3, 24), "Nationwide COVID-19 lockdown announced; all offices moved to remote working."),
        (date(2020, 5, 15), "Drew a ₹20 crore COVID-19 liquidity facility from Coral Bank."),
        (date(2020, 9, 1), "Offices reopened at 50% capacity; sales recovering."),
        (date(2021, 6, 25), "Board approved expense and travel policy v4."),
        (date(2022, 3, 8), "Placed orders worth ₹12 crore for the new data centre (Nexus Facilities, Juno Power Systems)."),
        (date(2022, 4, 1), "Moved into the new Mumbai office; monthly rent rose by ₹25 lakh."),
        (date(2023, 1, 18), "Cloud platform outage of 3 hours due to a power failure at the primary data centre; no data lost; "
                            "customers credited per SLA."),
        (date(2023, 11, 10), "Recertified to ISO/IEC 27001 for the cloud platform."),
        (date(2024, 8, 5), "Crossed 550 employees; opened a delivery centre in Kolkata."),
        (date(2025, 2, 12), "Won a multi-year cloud migration programme with Lumen Healthcare."),
        (date(2025, 9, 20), "Paid the FY2025 final dividend (20% of FY2025 net profit)."),
        (date(2026, 3, 31), "Closed FY2026 with record annual revenue; FinGuard AI selected for contractor payment governance."),
    ]
    names = list(CUSTOMERS) + list(VENDORS) + ["Coral Bank"]
    return [_doc(f"event-{d.isoformat()}", "event_log", f"Company event {_dt(d)}",
                 f"# Company event — {_dt(d)}\nOn {_dt(d)}: {t}", d, d, [n for n in names if n in t]) for d, t in E]


def build_corpus(ledger) -> list[dict]:
    f = Facts(ledger)
    return ([company_profile(f)] + policies() + event_log() + board_minutes(f) + dossiers(f) + monthly_logs(f)
            + daily_transactions(f))
