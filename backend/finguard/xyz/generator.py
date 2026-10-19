"""Deterministic generator for XYZ Ltd's 10-year daily double-entry ledger (synthetic, imaginary company).

Business model: an Indian technology company with three segments (Enterprise Software, Cloud Services, Hardware
Solutions) selling to ~50 business customers across five regions. The ledger contains, every day, sales invoices with
cost of sales, customer receipts (collected after a DSO-driven lag), inventory purchases and vendor payments, plus
monthly payroll, rent, utilities, marketing, IT, depreciation, interest, quarterly capex and advance tax, and annual
dividends. Growth, festival-season seasonality (Oct–Nov, March year-end) and a COVID-19 dip (Apr–Sep 2020) are built in.

All amounts are integers in paise (exact). Every entry balances (sum debit == sum credit). The same seed always
produces the same ledger. The in-memory result doubles as an independent "gold" source for verifying the Q&A engine.
"""

from __future__ import annotations

import calendar
import io
import math
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from finguard.xyz.coa import (ACCOUNTS, CUSTOMERS, DATA_END, DATA_START, OPEX_COUNTERPARTIES, REGIONS, SEGMENTS, VENDORS,
                              fy_of)

SEED = 20260401
CR = 100 * 10_000_000  # one crore rupees in paise
LAKH = 100 * 100_000


@dataclass
class Entry:
    id: int
    d: date
    doc_ref: str
    kind: str
    description: str
    counterparty: str | None = None
    segment: str | None = None
    region: str | None = None
    cf: str | None = None


@dataclass
class Line:
    entry_id: int
    d: date
    account: str
    debit: int
    credit: int
    segment: str | None = None
    region: str | None = None
    counterparty: str | None = None


@dataclass
class Ledger:
    entries: list[Entry] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)

    def add(self, d: date, kind: str, desc: str, legs: list[tuple[str, int, int]], *, cp=None, seg=None, reg=None,
            cf=None, ref=None, line_dims: bool = True) -> Entry:
        # a leg may carry its own counterparty as a 4th element (e.g. the cloud vendor on a sale's cost line)
        legs = [tuple(x) for x in legs if x[1] or x[2]]
        if not legs:
            return None  # type: ignore[return-value]
        assert sum(x[1] for x in legs) == sum(x[2] for x in legs), (kind, legs)
        e = Entry(len(self.entries) + 1, d, ref or f"{kind[:3]}-{d:%Y%m%d}-{len(self.entries) + 1:07d}", kind, desc[:200],
                  cp, seg, reg, cf)
        self.entries.append(e)
        for leg in legs:
            a, dr, cr = leg[:3]
            leg_cp = leg[3] if len(leg) > 3 else cp
            self.lines.append(Line(e.id, d, a, dr, cr, seg if line_dims else None, reg if line_dims else None,
                                   leg_cp if line_dims else None))
        return e


def _season(d: date) -> float:
    return {4: .9, 5: .9, 6: .95, 7: .95, 8: 1.0, 9: 1.05, 10: 1.2, 11: 1.15, 12: 1.0, 1: .95, 2: 1.0, 3: 1.35}[d.month]


def _covid(d: date) -> float:
    if d.year == 2020:
        return {4: .45, 5: .55, 6: .7, 7: .85, 8: .92}.get(d.month, 1.0)
    return 1.0


def _years(d: date) -> float:
    return (d - DATA_START).days / 365.25


def generate(seed: int = SEED) -> Ledger:
    rng = random.Random(seed)
    L = Ledger()
    # --- customers: fixed home region and size weight (Zipf-like)
    cust = [(c, REGIONS[i % 5] if i % 7 else "Export", 1.0 / (i + 1) ** 0.8) for i, c in enumerate(CUSTOMERS)]
    cust_w = [c[2] for c in cust]
    seg_mix = [(SEGMENTS[0], .40, 180_000, 0.12), (SEGMENTS[1], .35, 90_000, 0.45), (SEGMENTS[2], .25, 320_000, 0.68)]

    # --- opening balances (1 Apr 2016)
    L.add(DATA_START, "OPENING", "Opening balances brought forward", [
        ("1000", 22 * CR, 0), *[("1100", CR, 0, CUSTOMERS[i]) for i in range(12)], ("1200", 5 * CR, 0), ("1500", 10 * CR, 0),
        ("1510", 0, 2 * CR), *[("2000", 0, CR, VENDORS[i]) for i in range(4)], ("2500", 0, 15 * CR), ("3000", 0, 20 * CR),
        ("3100", 0, 8 * CR)], ref="OB-20160401")  # opening receivables/payables are settled by the first receipts/payments below

    receipts: dict[date, list[tuple[int, str, str, str]]] = defaultdict(list)   # date -> (amount, customer, segment, region)
    vendor_pay: dict[date, list[tuple[int, str]]] = defaultdict(list)          # date -> (amount, vendor)
    # Opening receivables / payables settle in the first weeks.
    for i in range(12):
        receipts[DATA_START + timedelta(days=5 + 3 * i)].append((CR, CUSTOMERS[i], SEGMENTS[i % 3], cust[i][1]))
    for i in range(4):
        vendor_pay[DATA_START + timedelta(days=7 + 6 * i)].append((CR, VENDORS[i]))

    cash = 22 * CR
    gross_ppe, acc_dep = 10 * CR, 2 * CR
    borrowings = 15 * CR
    tax_payable_due: list[tuple[date, int]] = []
    week_cogs = 0
    cloud_ap_month: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    fy_pbt: dict[int, int] = defaultdict(int)
    fy_tax: dict[int, int] = defaultdict(int)
    fy_ni: dict[int, int] = defaultdict(int)
    inv_no = 0
    d = DATA_START
    while d <= DATA_END:
        y, wd = _years(d), d.weekday()
        fy = fy_of(d)
        growth_n, price = 1.08 ** y, 1.04 ** y
        last_day = calendar.monthrange(d.year, d.month)[1]
        # ---------------- sales invoices
        lam = 18 * growth_n * _season(d) * _covid(d) * (0.4 if wd == 5 else 0.15 if wd == 6 else 1.18)
        n = _poisson(rng, lam)
        for _ in range(n):
            inv_no += 1
            c_name, c_reg, _w = rng.choices(cust, weights=cust_w)[0]
            r = rng.random()
            seg, _, mean, cost_ratio = seg_mix[0] if r < .40 else seg_mix[1] if r < .75 else seg_mix[2]
            amount = int(rng.lognormvariate(math.log(mean * price), 0.55) * 100)
            amount = max(amount, 5_000 * 100) // 100 * 100
            cost = int(amount * (cost_ratio + rng.uniform(-0.02, 0.02))) // 100 * 100
            rev_acc = "4100" if seg == SEGMENTS[1] else "4000"
            legs = [("1100", amount, 0), (rev_acc, 0, amount)]
            if seg == SEGMENTS[1]:
                vendor = rng.choice(("Echo Cloud Infra", "Krypton Data Centres"))
                legs += [("5100", cost, 0), ("2000", 0, cost, vendor)]
                cloud_ap_month[(d.year, d.month)][vendor] += cost
            else:
                legs += [("5000", cost, 0), ("1200", 0, cost)]
                week_cogs += cost
            fy_pbt[fy] += amount - cost
            L.add(d, "SALE", f"Sales invoice — {seg} — {c_name}", legs, cp=c_name, seg=seg, reg=c_reg,
                  ref=f"SI-{d:%Y%m%d}-{inv_no:06d}")
            dso = (70 if date(2020, 4, 1) <= d <= date(2020, 12, 31) else 48 - 0.6 * y)
            lag = max(7, int(rng.gauss(dso, 12)))
            receipts[d + timedelta(days=lag)].append((amount, c_name, seg, c_reg))
        # ---------------- customer receipts
        for amount, c_name, seg, reg in receipts.pop(d, []):
            L.add(d, "RECEIPT", f"Customer receipt — {c_name}", [("1000", amount, 0), ("1100", 0, amount)],
                  cp=c_name, seg=seg, reg=reg, cf="OPERATING")
            cash += amount
        # ---------------- inventory purchases (Mondays): replenish last week's product cost of sales
        if wd == 0 and week_cogs:
            vendor = rng.choice(VENDORS[:4] + ("Helix Hardware", "Crest Semiconductors"))
            amount = int(week_cogs * rng.uniform(1.0, 1.04)) // 100 * 100
            L.add(d, "PURCHASE", f"Inventory purchase — {vendor}", [("1200", amount, 0), ("2000", 0, amount)], cp=vendor)
            vendor_pay[d + timedelta(days=rng.randint(30, 45))].append((amount, vendor))
            week_cogs = 0
        # cloud infrastructure bills: pay the month-before-last accrual on the 10th
        if d.day == 10:
            pm = (d.replace(day=1) - timedelta(days=1)).replace(day=1)
            key = (pm.year, pm.month)
            if cloud_ap_month.get(key):
                for v_name, amt in cloud_ap_month.pop(key).items():
                    vendor_pay[d].append((amt, v_name))
        for amount, vendor in vendor_pay.pop(d, []):
            L.add(d, "VENDOR_PAYMENT", f"Vendor payment — {vendor}", [("2000", amount, 0), ("1000", 0, amount)],
                  cp=vendor, cf="OPERATING")
            cash -= amount

        def expense(acc: str, amount: int, desc: str, kind: str = "EXPENSE") -> None:
            nonlocal cash
            amount = amount // 100 * 100
            if amount <= 0:
                return
            L.add(d, kind, desc, [(acc, amount, 0), ("1000", 0, amount)], cp=OPEX_COUNTERPARTIES.get(acc), cf="OPERATING")
            cash -= amount
            fy_pbt[fy] -= amount

        covid_ops = 0.1 if date(2020, 4, 1) <= d <= date(2020, 8, 31) else 1.0
        if d.day == 1:
            rent = 45 * LAKH * 1.05 ** int(y) + (25 * LAKH if d >= date(2022, 4, 1) else 0)
            expense("6100", int(rent), f"Office rent — {d:%B %Y}")
        if d.day == 3:
            expense("6500", int(12 * LAKH * 1.10 ** y * rng.uniform(.95, 1.05)), f"Software subscriptions — {d:%B %Y}")
        if d.day in (5, 20):
            mk = 15 * LAKH * growth_n * (2.0 if d.month in (10, 11) else 1.0) * (0.4 if covid_ops < 1 else 1.0)
            expense("6300", int(mk * rng.uniform(.85, 1.15)), f"Marketing campaign — {d:%B %Y}")
        if d.day == 10:
            ut = 6 * LAKH * growth_n * (1.25 if d.month in (4, 5, 6) else 1.0)
            expense("6200", int(ut * rng.uniform(.9, 1.1)), f"Electricity and utilities — {d:%B %Y}")
        if wd < 5 and rng.random() < .85:
            expense("6400", int(rng.uniform(15_000, 60_000) * 100 * growth_n * covid_ops), "Business travel")
        if rng.random() < 0.07:
            expense("6800", int(rng.uniform(50_000, 300_000) * 100), "Repairs and maintenance")
        if d.day == 25 and d.month in (6, 9, 12, 3):
            expense("6600", int(18 * LAKH * price), "Legal and consulting fees (quarterly)")
        if d.day == 20 and d.month == 5:
            expense("6600", int(35 * LAKH * price), f"Statutory audit fee FY{fy - 1}")
        # ---------------- capex (investing)
        capex = 0
        if d.day == 15 and d.month in (6, 9, 12, 3):
            capex = int(1.2 * CR * growth_n)
        if d == date(2018, 9, 12):
            capex += 6 * CR
        if d == date(2022, 3, 8):
            capex += 12 * CR
        if capex:
            capex = capex // 100 * 100
            L.add(d, "CAPEX", "Purchase of property, plant and equipment", [("1500", capex, 0), ("1000", 0, capex)],
                  cp=rng.choice(("Helix Hardware", "Nexus Facilities", "Juno Power Systems")), cf="INVESTING")
            cash -= capex
            gross_ppe += capex
        # ---------------- borrowings (financing)
        if d == date(2020, 5, 15):
            L.add(d, "LOAN_DRAWDOWN", "Term loan drawdown — COVID-19 liquidity facility",
                  [("1000", 20 * CR, 0), ("2500", 0, 20 * CR)], cp="Coral Bank", cf="FINANCING")
            cash += 20 * CR
            borrowings += 20 * CR
        # ---------------- month end
        if d.day == last_day:
            pay = 380 * 1.06 ** y * 85_000 * 1.06 ** y * (0.9 if date(2020, 4, 1) <= d <= date(2020, 9, 30) else 1.0)
            expense("6000", int(pay * 100 * rng.uniform(.99, 1.01)), f"Salaries and wages — {d:%B %Y}", "PAYROLL")
            dep = min(gross_ppe // 60, gross_ppe - acc_dep) // 100 * 100
            if dep > 0:
                L.add(d, "DEPRECIATION", f"Depreciation — {d:%B %Y}", [("6700", dep, 0), ("1510", 0, dep)])
                acc_dep += dep
                fy_pbt[fy] -= dep
            if borrowings > 0:
                interest = int(borrowings * 0.095 / 12) // 100 * 100
                L.add(d, "INTEREST", f"Interest on term loans — {d:%B %Y}", [("7000", interest, 0), ("1000", 0, interest)],
                      cp="Coral Bank", cf="FINANCING")
                cash -= interest
                fy_pbt[fy] -= interest
            if cash > 0:
                ii = int(cash * 0.04 / 12) // 100 * 100
                L.add(d, "INTEREST_INCOME", f"Interest on deposits — {d:%B %Y}", [("1000", ii, 0), ("4200", 0, ii)],
                      cp="Coral Bank", cf="INVESTING")
                cash += ii
                fy_pbt[fy] += ii
            if d.month in (6, 9, 12, 3):
                if borrowings > 0:
                    rep = min(75 * LAKH, borrowings)
                    L.add(d, "LOAN_REPAYMENT", "Term loan instalment", [("2500", rep, 0), ("1000", 0, rep)],
                          cp="Coral Bank", cf="FINANCING")
                    cash -= rep
                    borrowings -= rep
                # advance tax on cumulative FY profit (true-up each quarter; never below zero for the year)
                due_total = max(0, int(fy_pbt[fy] * 0.2517))
                tax = (due_total - fy_tax[fy]) // 100 * 100
                if tax > 0:
                    L.add(d, "TAX_PROVISION", f"Income tax provision — Q{_q(d)} FY{fy}", [("8000", tax, 0), ("2300", 0, tax)])
                elif tax < 0:
                    L.add(d, "TAX_PROVISION", f"Income tax provision reversal — Q{_q(d)} FY{fy}", [("2300", -tax, 0), ("8000", 0, -tax)])
                fy_tax[fy] += tax
                if tax > 0:
                    tax_payable_due.append((d + timedelta(days=15), tax))
            if d.month == 3:
                fy_ni[fy] = fy_pbt[fy] - fy_tax[fy]
        for due, amt in [t for t in tax_payable_due if t[0] == d]:
            L.add(d, "TAX_PAYMENT", "Advance tax paid", [("2300", amt, 0), ("1000", 0, amt)], cp="Income Tax Department",
                  cf="OPERATING")
            cash -= amt
        tax_payable_due = [t for t in tax_payable_due if t[0] != d]
        # ---------------- dividends (20 Sep, 20% of prior FY profit)
        if d.month == 9 and d.day == 20 and fy - 1 in fy_ni and fy_ni[fy - 1] > 0:
            div = int(fy_ni[fy - 1] * 0.20) // 100 * 100
            L.add(d, "DIVIDEND", f"Final dividend for FY{fy - 1}", [("3200", div, 0), ("1000", 0, div)],
                  cp="Shareholders", cf="FINANCING")
            cash -= div
        d += timedelta(days=1)
    return L


def _q(d: date) -> int:
    return {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth for small λ; normal approximation for larger λ
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    lim, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= lim:
            return k
        k += 1


# ------------------------------------------------------------------------------------------- persistence
def load_into_db(db, org_id: uuid.UUID, ledger: Ledger) -> dict:
    """Replace the organization's ledger with the generated one (bulk COPY)."""
    from sqlalchemy import text

    db.execute(text("DELETE FROM fin_journal_lines WHERE org_id = :o"), {"o": org_id})
    db.execute(text("DELETE FROM fin_journal_entries WHERE org_id = :o"), {"o": org_id})
    db.execute(text("DELETE FROM fin_accounts WHERE org_id = :o"), {"o": org_id})
    for code, (name, typ, stmt) in ACCOUNTS.items():
        db.execute(text("INSERT INTO fin_accounts (org_id, code, name, type, statement) VALUES (:o,:c,:n,:t,:s)"),
                   {"o": org_id, "c": code, "n": name, "t": typ, "s": stmt})
    max_id = db.execute(text("SELECT COALESCE(MAX(id),0) FROM fin_journal_entries")).scalar_one()
    max_line = db.execute(text("SELECT COALESCE(MAX(id),0) FROM fin_journal_lines")).scalar_one()
    conn = db.connection().connection  # psycopg connection
    with conn.cursor() as cur:
        with cur.copy("COPY fin_journal_entries (id, org_id, entry_date, doc_ref, kind, description, counterparty, segment, "
                      "region, cf_category) FROM STDIN") as cp:
            for e in ledger.entries:
                cp.write_row((max_id + e.id, org_id, e.d, e.doc_ref, e.kind, e.description, e.counterparty, e.segment,
                              e.region, e.cf))
        with cur.copy("COPY fin_journal_lines (id, org_id, entry_id, entry_date, account_code, debit, credit, segment, "
                      "region, counterparty) FROM STDIN") as cp:
            for i, l in enumerate(ledger.lines, start=1):
                cp.write_row((max_line + i, org_id, max_id + l.entry_id, l.d, l.account, f"{l.debit / 100:.2f}",
                              f"{l.credit / 100:.2f}", l.segment, l.region, l.counterparty))
    return {"entries": len(ledger.entries), "lines": len(ledger.lines)}
