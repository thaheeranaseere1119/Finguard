"""Company financials (ledger-backed): statements, trends, transaction explorer and grounded Q&A."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from finguard.api.deps import current_principal
from finguard.authz import abac
from finguard.authz.principal import Principal
from finguard.core.db import get_db
from finguard.core.errors import NotFound, RateLimited
from finguard.core.ratelimit import hit
from finguard.models import FinJournalEntry, FinJournalLine
from finguard.services.audit import AuditService
from finguard.xyz.coa import ACCOUNTS, FIRST_FY, LAST_FY, fy_bounds
from finguard.xyz.metrics import METRICS, Calc, Filters, SQLSource

router = APIRouter(prefix="/financials", tags=["financials"])


def has_ledger(db: Session, org_id) -> bool:
    return db.execute(select(FinJournalEntry.id).where(FinJournalEntry.org_id == org_id).limit(1)).first() is not None


def _calc(db: Session, p: Principal) -> Calc:
    abac.require(p, "financials:read")
    if not has_ledger(db, p.org_id):
        raise NotFound("no financial ledger is loaded for your organization")
    return Calc(SQLSource(db, p.org_id))


def _num(v) -> str:
    return str(v) if v is not None else None


@router.get("/overview")
def overview(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    c = _calc(db, p)
    years = []
    for fy in range(FIRST_FY, LAST_FY + 1):
        a, b = fy_bounds(fy)
        row = {"fy": fy}
        for k in ("revenue", "gross_profit", "operating_income", "net_income", "cfo", "capex", "cash", "operating_margin",
                  "net_margin", "invoice_count"):
            row[k] = _num(METRICS[k].fn(c, a, b, Filters()))
        years.append(row)
    return {"company": "XYZ Ltd", "currency": "INR", "first_fy": FIRST_FY, "last_fy": LAST_FY, "years": years,
            "entries": db.execute(select(func.count()).select_from(FinJournalEntry).where(FinJournalEntry.org_id == p.org_id)).scalar_one()}


@router.get("/statements")
def statements(fy: int = Query(..., ge=2000, le=2100), p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    c = _calc(db, p)
    a, b = fy_bounds(fy)
    pa, pb = fy_bounds(fy - 1)
    prev_ok = fy - 1 >= FIRST_FY

    def row(key):
        cur = METRICS[key].fn(c, a, b, Filters())
        prv = METRICS[key].fn(c, pa, pb, Filters()) if prev_ok else None
        return {"key": key, "label": METRICS[key].label, "unit": METRICS[key].unit, "current": _num(cur), "previous": _num(prv)}

    return {
        "fy": fy, "period": {"from": a.isoformat(), "to": b.isoformat()},
        "income_statement": [row(k) for k in ("revenue", "product_revenue", "service_revenue", "cogs", "gross_profit",
                                              "employee_expense", "rent", "utilities", "marketing", "travel", "it_expense",
                                              "professional_fees", "repairs", "depreciation", "opex", "operating_income",
                                              "other_income", "interest_expense", "pbt", "tax_expense", "net_income")],
        "balance_sheet": [row(k) for k in ("cash", "receivables", "inventory", "ppe_net", "total_assets", "payables",
                                           "borrowings", "total_liabilities", "equity")],
        "cash_flow": [row(k) for k in ("cfo", "cfi", "cff", "capex", "fcf", "dividends")],
        "ratios": [row(k) for k in ("gross_margin", "operating_margin", "net_margin", "ebitda_margin", "current_ratio",
                                    "debt_to_equity", "roe", "roa", "dso", "dpo", "inventory_turnover", "effective_tax_rate",
                                    "interest_coverage")],
    }


@router.get("/monthly")
def monthly(fy: int = Query(..., ge=2000, le=2100), p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    _calc(db, p)
    a, b = fy_bounds(fy)
    rows = db.execute(text("""
        SELECT date_trunc('month', entry_date)::date AS m,
               SUM(CASE WHEN account_code IN ('4000','4100') THEN credit - debit ELSE 0 END) AS revenue,
               SUM(CASE WHEN account_code IN ('5000','5100') THEN debit - credit ELSE 0 END) AS cogs,
               SUM(CASE WHEN account_code LIKE '6%' THEN debit - credit ELSE 0 END) AS opex
        FROM fin_journal_lines WHERE org_id = :o AND entry_date BETWEEN :a AND :b GROUP BY 1 ORDER BY 1"""),
                      {"o": p.org_id, "a": a, "b": b}).all()
    seg = db.execute(text("""SELECT segment, SUM(credit - debit) FROM fin_journal_lines WHERE org_id = :o
        AND account_code IN ('4000','4100') AND entry_date BETWEEN :a AND :b GROUP BY 1 ORDER BY 2 DESC"""),
                     {"o": p.org_id, "a": a, "b": b}).all()
    reg = db.execute(text("""SELECT region, SUM(credit - debit) FROM fin_journal_lines WHERE org_id = :o
        AND account_code IN ('4000','4100') AND entry_date BETWEEN :a AND :b GROUP BY 1 ORDER BY 2 DESC"""),
                     {"o": p.org_id, "a": a, "b": b}).all()
    cust = db.execute(text("""SELECT counterparty, SUM(credit - debit) FROM fin_journal_lines WHERE org_id = :o
        AND account_code IN ('4000','4100') AND entry_date BETWEEN :a AND :b GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""),
                      {"o": p.org_id, "a": a, "b": b}).all()
    return {"fy": fy, "months": [{"month": m.isoformat(), "revenue": str(r), "cogs": str(cg), "opex": str(ox)} for m, r, cg, ox in rows],
            "by_segment": [{"name": s, "revenue": str(v)} for s, v in seg],
            "by_region": [{"name": s, "revenue": str(v)} for s, v in reg],
            "top_customers": [{"name": s, "revenue": str(v)} for s, v in cust]}


@router.get("/transactions")
def transactions(date_from: date | None = None, date_to: date | None = None, kind: str | None = None, q: str | None = None,
                 limit: int = Query(50, le=200), offset: int = 0, p: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)) -> dict:
    _calc(db, p)
    stmt = select(FinJournalEntry).where(FinJournalEntry.org_id == p.org_id)
    if date_from:
        stmt = stmt.where(FinJournalEntry.entry_date >= date_from)
    if date_to:
        stmt = stmt.where(FinJournalEntry.entry_date <= date_to)
    if kind:
        stmt = stmt.where(FinJournalEntry.kind == kind)
    if q:
        like = f"%{q[:60]}%"
        stmt = stmt.where((FinJournalEntry.description.ilike(like)) | (FinJournalEntry.counterparty.ilike(like))
                          | (FinJournalEntry.doc_ref.ilike(like)))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(FinJournalEntry.entry_date.desc(), FinJournalEntry.id.desc()).limit(limit).offset(offset)).scalars().all()
    ids = [r.id for r in rows]
    lines: dict[int, list] = {}
    if ids:
        for l in db.execute(select(FinJournalLine).where(FinJournalLine.entry_id.in_(ids))).scalars():
            lines.setdefault(l.entry_id, []).append({"account": l.account_code, "account_name": ACCOUNTS[l.account_code][0],
                                                     "debit": str(l.debit), "credit": str(l.credit)})
    return {"total": total, "items": [{"id": r.id, "date": r.entry_date.isoformat(), "doc_ref": r.doc_ref, "kind": r.kind,
                                       "description": r.description, "counterparty": r.counterparty, "segment": r.segment,
                                       "region": r.region, "cf_category": r.cf_category, "lines": lines.get(r.id, [])}
                                      for r in rows]}


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


@router.post("/ask")
def ask(body: AskIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict:
    from finguard.xyz.qa import answer

    if not hit(f"finq:{p.user_id}", 60, 60):
        raise RateLimited("too many financial questions; try again in a minute")
    c = _calc(db, p)
    a = answer(c, body.question)
    AuditService(db).record("financials.question", actor=p, resource_type="financial_ledger", new_state=a.status,
                            payload={"question": body.question[:300], "metric": a.plan.metric if a.plan else None,
                                     "op": a.plan.op if a.plan else None})
    db.commit()
    return answer_view(a)


def answer_view(a) -> dict:
    v = a.value
    if isinstance(v, list):
        v = [[str(x) for x in row] if isinstance(row, tuple) else str(row) for row in v]
    elif isinstance(v, Decimal):
        v = str(v)
    return {"status": a.status, "answer": a.text, "value": v, "unit": a.unit, "formula": a.formula, "evidence": a.evidence,
            "structured": a.structured,
            "plan": ({"metric": a.plan.metric, "operation": a.plan.op, "periods": [x.label for x in a.plan.periods],
                      "filters": {k: getattr(a.plan.filters, k) for k in ("segment", "region", "counterparty") if getattr(a.plan.filters, k)},
                      "group": a.plan.group, "k": a.plan.k, "confidence": round(a.plan.confidence, 3),
                      "assumptions": a.plan.assumptions} if a.plan else None)}
