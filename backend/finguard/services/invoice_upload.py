"""Contractor-uploaded invoices (the contractor's own PDF or photo, instead of the system-generated invoice).

upload → validate file (type/size/magic bytes) → reject duplicates (same file hash, same invoice number) → text layer
(native PDF text, OCR for scans/photos) → read invoice number, total, currency, invoice date → document checks
(readable total, number present, OCR confidence, no hidden instructions, date sensible, entered values agree with the
document) → the SAME validate_and_route() as generated invoices:
  all checks pass + amount equals the approved schedule → accepted by rule, payment released to the risk engine;
  price increase / anything unclear → finance analyst.
The amount used is what the DOCUMENT says; values typed by the contractor are only cross-checked against it.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finguard.authz.principal import Principal
from finguard.core.errors import Conflict, InvalidState, ValidationFailed
from finguard.documents import storage
from finguard.extraction import parsers as P
from finguard.models import ContractDocument, Invoice
from finguard.models.enums import ContractStatus, InvoiceStatus
from finguard.models.enums import ScheduledPaymentStatus as SPS
from finguard.services.audit import AuditService

ALLOWED_MIME = {"application/pdf", "image/png", "image/jpeg"}
MIN_OCR_CONFIDENCE = 60.0
_NUMBER_RE = re.compile(r"\binvoice\s*(?:no\.?|number|num\.?|#|id)\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9\-/_.]{2,40})", re.I)
_TOTAL_WORDS = re.compile(r"\b(grand\s+total|total\s+(?:amount\s+)?(?:due|payable)|amount\s+(?:due|payable)|balance\s+due|"
                          r"net\s+payable|invoice\s+total|total)\b", re.I)
_DATE_WORDS = re.compile(r"\b(invoice\s+date|date\s+of\s+(?:issue|invoice)|issue\s+date|dated|date)\b", re.I)
_BARE_NUM = re.compile(r"(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{2})")
_OCR_RUN = re.compile(r"(?:₹|rs\.?|inr|\$|usd|us\$)?\s*[‘'’`]?(\d[\d,;:.'’ ]*\d)", re.I)
_OCR_CUR = re.compile(r"(₹|\brs\.?|\binr\b|\$|\busd\b|us\$)", re.I)


def ocr_amount(line: str) -> tuple[Decimal, str | None] | None:
    """OCR-tolerant total: '40;000: 00' / '‘40, 000. 00' → 40000.00 (digit groups joined, a final 2-digit group = paise)."""
    tail = line.split(":", 1)[1] if ":" in line else line
    runs = _OCR_RUN.findall(tail)
    if not runs:
        return None
    groups = [g for g in re.split(r"[,;:.'’ ]+", runs[-1]) if g]
    if not groups or any(not g.isdigit() for g in groups):
        return None
    if len(groups) >= 2 and len(groups[-1]) == 2:
        whole, frac = "".join(groups[:-1]), groups[-1]
    else:
        whole, frac = "".join(groups), "00"
    if len(groups) > 1 and any(len(g) not in (2, 3) for g in groups[1:-1 if len(groups[-1]) == 2 else None]):
        return None                                   # not a plausible thousands grouping
    try:
        amt = P.parse_amount_text(f"{whole}.{frac}")
    except P.ParseError:
        return None
    m = _OCR_CUR.search(tail)
    return amt, (P.normalize_currency(m.group(1).rstrip(".")) if m else None)


def read_invoice(text: str, default_currency: str, ocr: bool = False) -> dict[str, Any]:
    """Pull the key fields out of an invoice's text. Every value records how it was found."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out: dict[str, Any] = {"invoice_number": None, "amount": None, "currency": None, "currency_assumed": False,
                           "invoice_date": None, "amount_source": None}
    if m := _NUMBER_RE.search(text):
        num = m.group(1).strip(".-/")
        if not re.fullmatch(r"(?i)date|dated|no|number", num):
            out["invoice_number"] = num[:64]
    # total: the last amount on the strongest "total" line (subtotals ignored); else the largest amount anywhere
    best = None
    for i, line in enumerate(lines):
        if not _TOTAL_WORDS.search(line) or re.search(r"\bsub\s*-?\s*total\b", line, re.I):
            continue
        rank = 2 if re.search(r"grand|due|payable|balance|net", line, re.I) else 1
        window = line + " " + (lines[i + 1] if i + 1 < len(lines) else "")
        amts = P.find_amounts(line) or P.find_amounts(window)
        if ocr and (oa := ocr_amount(line)) is not None and (not amts or oa[0] > amts[-1][2]):
            cand = (rank, oa[0], oa[1] or (amts[-1][3] if amts else None), "ocr")
        elif amts:
            cand = (rank, amts[-1][2], amts[-1][3], "currency")
        elif (bare := _BARE_NUM.findall(line)):
            try:
                cand = (rank, P.parse_amount_text(bare[-1]), None, "bare")
            except P.ParseError:
                continue
        else:
            continue
        if best is None or cand[0] >= best[0]:
            best = cand
    if best:
        out["amount"], out["currency"], out["amount_source"] = best[1], best[2], "total line"
    else:
        # no readable "Total" line: best guess only (the document check below then sends the invoice to review)
        cands = [(a[2], a[3]) for a in P.find_amounts(text)]
        if ocr:
            cands += [oa for line in lines if (oa := ocr_amount(line)) is not None]
        if cands:
            top = max(cands, key=lambda a: a[0])
            out["amount"], out["currency"], out["amount_source"] = top[0], top[1], "largest amount on the invoice (no readable total line)"
    if out["amount"] is not None and not out["currency"]:
        out["currency"], out["currency_assumed"] = default_currency, True
    # invoice date: a date on a "date" line, else the first date in the document
    for line in lines:
        if _DATE_WORDS.search(line) and (ds := P.find_dates(line)):
            out["invoice_date"] = ds[0][2]
            break
    if out["invoice_date"] is None and (ds := P.find_dates(text)):
        out["invoice_date"] = ds[0][2]
    return out


def upload_invoice(db: Session, actor: Principal, sp_id: Any, filename: str, data: bytes,
                   entered_number: str | None = None, entered_amount: str | None = None) -> Invoice:
    from finguard.documents.text_extraction import extract
    from finguard.extraction.injection import scan_text
    from finguard.services.invoicing import _owned_payment, _period_billable, validate_and_route
    from finguard.services.payments import version_for_period

    from finguard.authz import abac

    abac.require(actor, "invoices:submit_own")
    sp, c = _owned_payment(db, actor, sp_id)
    if c.status not in (ContractStatus.ACTIVE, ContractStatus.SUSPENDED):
        raise InvalidState("invoices can be submitted only for active contracts")
    if sp.status in (SPS.PAID, SPS.SUPERSEDED, SPS.CANCELLED):
        raise InvalidState(f"payment is {sp.status}; it cannot be invoiced")
    existing = db.execute(select(Invoice).where(Invoice.scheduled_payment_id == sp.id,
                                                Invoice.status != InvoiceStatus.REJECTED)).scalars().first()
    if existing:
        raise Conflict(f"invoice {existing.invoice_number} was already submitted for this period ({existing.status})")
    v = version_for_period(db, c.id, sp.period_start)
    if v is None or not _period_billable(v, sp):
        raise InvalidState("this service period cannot be invoiced yet (it has not ended)")
    upload = storage.validate_upload(filename, data)
    if upload.mime_type not in ALLOWED_MIME:
        raise ValidationFailed("upload the invoice as a PDF, PNG or JPEG")
    dup = db.execute(select(ContractDocument.id).where(ContractDocument.org_id == c.org_id, ContractDocument.kind == "INVOICE",
                                                       ContractDocument.sha256 == upload.sha256)).first()
    if dup:
        raise Conflict("this exact invoice file was already submitted")

    layer = extract(upload.data, upload.mime_type)
    text = "\n".join(p.text for p in layer.pages)
    fields = read_invoice(text, sp.currency, ocr=layer.method in ("OCR", "MIXED"))
    inj = scan_text(text)
    typed_amount = None
    if entered_amount:
        try:
            typed_amount = P.parse_amount_text(entered_amount)
        except P.ParseError as exc:
            raise ValidationFailed(str(exc)) from exc
    amount = fields["amount"] if fields["amount"] is not None else typed_amount
    if amount is None:
        raise ValidationFailed("couldn't read the invoice total from this file — upload a clearer copy, or enter the amount")
    number = (fields["invoice_number"] or (entered_number or "").strip() or f"UPL-{upload.sha256[:10].upper()}")[:64]
    if db.execute(select(Invoice.id).where(Invoice.contract_id == c.id, Invoice.invoice_number == number)).first():
        raise Conflict(f"invoice number {number} was already used on this contract")

    key = storage.store(actor.org_id, upload)
    doc = ContractDocument(org_id=c.org_id, contract_id=c.id, kind="INVOICE", original_filename=upload.safe_filename,
                           storage_key=key, mime_type=upload.mime_type, size_bytes=len(upload.data), sha256=upload.sha256,
                           page_count=upload.page_count, uploaded_by=actor.user_id)
    db.add(doc)
    db.flush()
    inv = Invoice(org_id=c.org_id, contract_id=c.id, scheduled_payment_id=sp.id, invoice_number=number, amount=amount,
                  currency=fields["currency"] or sp.currency, document_id=doc.id, submitted_by=actor.user_id,
                  status=InvoiceStatus.SUBMITTED)
    db.add(inv)
    db.flush()

    conf = layer.mean_ocr_confidence

    def chk(name, ok, why):
        return {"check": name, "passed": bool(ok), "detail": None if ok else why, "kind": "document"}

    d: date | None = fields["invoice_date"]
    # the invoice must not name ANOTHER contract of this organisation (e.g. an invoice for a different engagement)
    from finguard.models import Contract as _C

    other_refs = [r for (r,) in db.execute(select(_C.external_ref).where(_C.org_id == c.org_id, _C.id != c.id,
                                                                          _C.external_ref.is_not(None))).all()
                  if r and r != c.external_ref and r.lower() in text.lower()]
    doc_checks = [
        chk("invoice is for this contract", not other_refs,
            f"the invoice refers to contract {', '.join(other_refs)}, not {c.external_ref or c.title}"),
        chk("invoice total readable", fields["amount"] is not None and fields["amount_source"] == "total line",
            "no readable Total / Amount Due line — the amount is only a best guess"),
        chk("amount plausible", fields["amount"] is None or fields["amount"] >= Decimal(sp.amount) / 2,
            f"read total {fields['amount']} is less than half the scheduled {sp.amount} — possible misread"),
        chk("invoice number on the document", fields["invoice_number"] is not None, "no invoice number found on the document"),
        chk("scan quality", conf is None or conf >= MIN_OCR_CONFIDENCE,
            f"OCR confidence {conf} is below {MIN_OCR_CONFIDENCE}; a person should read it"),
        chk("no hidden instructions in the document", not inj.detected,
            f"instruction-like text detected ({', '.join(inj.families)})"),
        chk("invoice date present", d is not None, "no invoice date found on the document"),
        chk("invoice date not before the service period", d is None or d >= sp.period_start,
            f"invoice dated {d} is before the service period starts ({sp.period_start})"),
        chk("invoice date not in the future", d is None or d <= date.today(), f"invoice is dated in the future ({d})"),
        chk("entered amount matches the document", typed_amount is None or fields["amount"] is None or typed_amount == fields["amount"],
            f"you entered {typed_amount} but the document says {fields['amount']}"),
        chk("entered number matches the document", not entered_number or not fields["invoice_number"]
            or entered_number.strip().upper() == fields["invoice_number"].upper(),
            f"you entered {entered_number} but the document says {fields['invoice_number']}"),
    ]
    extracted = {"invoice_number": fields["invoice_number"], "amount": str(fields["amount"]) if fields["amount"] is not None else None,
                 "currency": fields["currency"], "currency_assumed": fields["currency_assumed"],
                 "invoice_date": d.isoformat() if d else None, "amount_source": fields["amount_source"],
                 "text_method": layer.method, "ocr_confidence": conf}
    AuditService(db).record("invoice.uploaded", actor=actor, resource_type="invoice", resource_id=inv.id, contract_id=c.id,
                            new_state=InvoiceStatus.SUBMITTED,
                            payload={"document_sha256": doc.sha256, "extracted": extracted, "injection": inj.to_dict()})
    inv.release = validate_and_route(db, inv, sp, c, extra_checks=doc_checks) | {"extracted": extracted}
    return inv
