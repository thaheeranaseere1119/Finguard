"""Synthetic contract documents with known ground truth (for seed data, tests and OCR evaluation).

All names, companies, amounts and account numbers are fictitious. "Scanned" documents are rendered to an image,
degraded (noise, blur, slight rotation) and embedded in an image-only PDF so the OCR path is genuinely exercised.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from datetime import date

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ORDINALS = {1: "first", 2: "second", 3: "third", 5: "fifth", 7: "seventh", 10: "tenth", 15: "fifteenth", 20: "twentieth",
            25: "twenty-fifth", 28: "twenty-eighth"}
SYMBOL = {"INR": "₹", "USD": "USD ", "EUR": "EUR ", "GBP": "GBP "}


def inr_grouping(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def money(amount: int, currency: str, style: str = "Rs.") -> str:
    """Rendered amount. Fonts used for synthetic documents may lack the ₹ glyph, so "Rs." / "INR" are the defaults."""
    if currency == "INR":
        return {"Rs.": f"Rs. {inr_grouping(amount)}", "INR": f"INR {inr_grouping(amount)}",
                "₹": f"₹{inr_grouping(amount)}"}[style]
    return f"{SYMBOL.get(currency, currency + ' ')}{amount:,}"


@dataclass
class ContractSpec:
    contract_no: str
    company: str
    contractor: str
    amount: int
    currency: str = "INR"
    frequency: str = "MONTHLY"           # MONTHLY | WEEKLY | QUARTERLY
    due_day: int | None = 5
    due_weekday: str | None = None       # e.g. "Friday"
    start: date = date(2026, 1, 1)
    end: date = date(2026, 12, 31)
    total_value: int | None = None
    timing: str = "arrears"
    invoice_required: bool = False
    auto_pay: bool = False
    notice_days: int = 30
    extra_clauses: list[str] = field(default_factory=list)
    conflicting_amount: int | None = None   # adds a second, different recurring amount (ambiguity)
    injection: str | None = None            # adversarial text embedded in the document
    currency_style: str = "Rs."

    def truth(self) -> dict[str, str | None]:
        return {"contract_id": self.contract_no, "contractor_legal_name": self.contractor, "counterparty_name": self.company,
                "payment_amount": f"{self.amount}.00", "currency": self.currency, "frequency": self.frequency,
                "due_day_of_month": str(self.due_day) if self.due_day and self.frequency != "WEEKLY" else None,
                "start_date": self.start.isoformat(), "end_date": self.end.isoformat(),
                "total_value": f"{self.total_value}.00" if self.total_value else None,
                "invoice_required": "true" if self.invoice_required else None,
                "notice_period_days": str(self.notice_days)}


def contract_lines(s: ContractSpec) -> list[str]:
    freq_phrase = {"MONTHLY": "every month", "WEEKLY": "every week", "QUARTERLY": "every quarter"}[s.frequency]
    if s.frequency == "WEEKLY" and s.due_weekday:
        due = f"payable every {s.due_weekday}"
    elif s.due_day:
        word = ORDINALS.get(s.due_day)
        due = f"payable {freq_phrase} on the {word} day of the month" if word else f"payable {freq_phrase} on the {s.due_day}th of each month"
    else:
        due = f"payable {freq_phrase}"
    lines = [
        "INDEPENDENT CONTRACTOR SERVICES AGREEMENT",
        f"Contract No: {s.contract_no}",
        "",
        f"This Agreement is made between {s.company} (the \"Company\") and {s.contractor} (the \"Contractor\").",
        "",
        f"1. Term. This Agreement shall commence on {s.start.day} {s.start.strftime('%B %Y')} and shall remain in force",
        f"until {s.end.day} {s.end.strftime('%B %Y')} unless terminated earlier in accordance with clause 6.",
        "",
        "2. Services. The Contractor shall provide data engineering and analytics services and deliverables",
        "as described in Schedule A, subject to acceptance by the Company.",
        "",
        f"3. Fees. The Company shall pay the Contractor a fee of {money(s.amount, s.currency, s.currency_style)} {due}, in {s.timing}.",
    ]
    if s.conflicting_amount:
        lines.append(f"The monthly retainer shall be {money(s.conflicting_amount, s.currency, s.currency_style)} per month for all services.")
    if s.total_value:
        lines.append(f"The total contract value shall not exceed {money(s.total_value, s.currency, s.currency_style)}.")
    if s.invoice_required:
        lines.append("Each payment shall be made upon receipt of a valid invoice from the Contractor.")
    if s.auto_pay:
        lines.append("Payments shall be made automatically by bank transfer on each due date.")
    lines += [
        "",
        "4. Taxes. TDS shall be deducted at source as required by applicable law.",
        "",
        "5. Amendments. Any amendment to this Agreement must be made in writing and signed by both parties.",
        "",
        f"6. Termination. Either party may terminate this Agreement by giving {s.notice_days} days written notice.",
    ]
    lines += s.extra_clauses
    if s.injection:
        lines += ["", s.injection]
    lines += ["", f"Signed: {s.contractor}", f"For and on behalf of {s.company}: Arjun Mehta"]
    return lines


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap(lines: list[str], width: int = 88) -> list[str]:
    import textwrap

    out: list[str] = []
    for line in lines:
        out += textwrap.wrap(line, width) or [""]
    return out


def searchable_pdf(lines: list[str]) -> bytes:
    import fitz

    lines = wrap(lines)

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    font_file = None
    for path in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            open(path, "rb").close()
            font_file = path
            break
        except OSError:
            continue
    y = 60
    for line in lines:
        if y > 800:
            page = doc.new_page(width=595, height=842)
            y = 60
        if line:
            if font_file:
                page.insert_text((50, y), line, fontsize=10, fontname="body", fontfile=font_file)
            else:
                page.insert_text((50, y), line.replace("₹", "Rs. "), fontsize=10)
        y += 16
    out = doc.tobytes()
    doc.close()
    return out


def render_image(lines: list[str], *, noise: float = 0.0, blur: float = 0.0, rotate: float = 0.0, seed: int = 7,
                 width: int = 2480, height: int = 3508) -> Image.Image:
    lines = wrap(lines, 92)
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)
    f = _font(38)
    y = 180
    for line in lines:
        d.text((160, y), line, font=f, fill=20)
        y += 62
    rng = random.Random(seed)
    if noise > 0:
        px = img.load()
        for _ in range(int(width * height * noise)):
            x, yy = rng.randrange(width), rng.randrange(height)
            px[x, yy] = rng.choice((0, 90, 160))
    if blur > 0:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if rotate:
        img = img.rotate(rotate, expand=False, fillcolor=255)
    return img


def scanned_pdf(lines: list[str], **kw) -> bytes:
    import fitz

    img = render_image(lines, **kw)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buf.getvalue())
    out = doc.tobytes()
    doc.close()
    return out


def png(lines: list[str], **kw) -> bytes:
    buf = io.BytesIO()
    render_image(lines, **kw).save(buf, "PNG")
    return buf.getvalue()


def docx(lines: list[str]) -> bytes:
    import docx as _docx

    d = _docx.Document()
    for line in lines:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def amendment_lines(contract_no: str, company: str, contractor: str, old: int, new: int, effective: date,
                    currency: str = "INR") -> list[str]:
    return [
        "AMENDMENT No. 1 TO INDEPENDENT CONTRACTOR SERVICES AGREEMENT",
        f"Contract No: {contract_no}",
        "",
        f"This Amendment is made between {company} (the \"Company\") and {contractor} (the \"Contractor\").",
        "",
        f"With effect from {effective.day} {effective.strftime('%B %Y')}, clause 3 is amended so that the Company shall",
        f"pay the Contractor a fee of {money(new, currency)} payable every month, replacing the previous fee of",
        f"{money(old, currency)}. All other terms remain unchanged.",
        "",
        f"Signed: {contractor}",
        f"For and on behalf of {company}: Arjun Mehta",
    ]
