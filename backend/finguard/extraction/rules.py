"""Rule-based (deterministic) contract extractor with page/line/bbox provenance.

Every candidate records where it came from. Amount mentions are classified by their surrounding context into
recurring payment, total value, or other (penalty, deposit, fee) — "₹20,000 payable every month" is a payment
amount, never a total value unless the text says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from finguard.core.redact import redact_text
from finguard.extraction import parsers as P


@dataclass
class Candidate:
    value: str | None
    display: str | None
    page: int | None = None
    source_text: str | None = None
    bbox: list[float] | None = None
    ocr_conf: float | None = None
    confidence: float = 0.9
    issues: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    derived: bool = False
    null_reason: str | None = None


@dataclass
class _Span:
    page: int
    start: int
    end: int
    text: str
    bbox: list[float] | None
    conf: float


class DocIndex:
    """Page texts with an offset → line map so regex hits can be linked to a page, line and bounding box."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.spans: list[_Span] = []
        self.page_text: dict[int, str] = {}
        for p in pages:
            offset, parts = 0, []
            for ln in p.get("lines") or [{"text": t, "bbox": None, "conf": 100} for t in p["text"].splitlines()]:
                t = ln["text"]
                self.spans.append(_Span(p["page"], offset, offset + len(t), t, ln.get("bbox"), float(ln.get("conf") or 100)))
                parts.append(t)
                offset += len(t) + 1
            self.page_text[p["page"]] = "\n".join(parts)

    def locate(self, page: int, start: int, end: int) -> tuple[str, list[float] | None, float]:
        hit = [s for s in self.spans if s.page == page and s.end >= start and s.start <= end]
        if not hit:
            return "", None, 100.0
        text = " ".join(s.text for s in hit)
        boxes = [s.bbox for s in hit if s.bbox]
        bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None
        return text, bbox, min(s.conf for s in hit)

    def iter_pages(self):
        yield from self.page_text.items()

    def sentence_at(self, page: int, start: int, end: int) -> str:
        text = self.page_text[page]
        s = max(text.rfind(".", 0, start), text.rfind("\n\n", 0, start)) + 1
        e_candidates = [i for i in (text.find(".", end), text.find("\n\n", end)) if i != -1]
        e = min(e_candidates) + 1 if e_candidates else len(text)
        return " ".join(text[s:e].split())[:400]


def _cand(idx: DocIndex, page: int, start: int, end: int, value: str | None, display: str | None,
          conf: float = 0.9, **kw: Any) -> Candidate:
    line_text, bbox, ocr_conf = idx.locate(page, start, end)
    ocr_factor = 1.0 if ocr_conf >= 99 else max(0.4, ocr_conf / 100)
    return Candidate(value, display, page, redact_text(idx.sentence_at(page, start, end) or line_text), bbox,
                     None if ocr_conf >= 99 else ocr_conf, round(conf * ocr_factor, 3), **kw)


RECURRING_CTX = re.compile(r"per\s+(month|week|quarter|annum|year)|monthly|weekly|quarterly|annually|yearly|fortnightly|"
                           r"every\s+(month|week|quarter|year|two weeks|\w+day)|each\s+(calendar\s+)?(month|week|quarter|year)|"
                           r"(retainer|fee|remuneration|compensation|payment)\s+of|payable", re.I)
TOTAL_CTX = re.compile(r"total\s+(contract\s+)?(value|consideration|amount|fees?|price)|aggregate|in\s+total|"
                       r"(shall|will)\s+not\s+exceed|maximum\s+(amount|value)|contract\s+value|not\s+to\s+exceed", re.I)
OTHER_CTX = re.compile(r"previous(?:ly)?\s+(?:fee|amount|rate)|replacing|formerly|penalt|late\s+fee|interest|deposit|liquidated|damages|reimburs|advance\s+of|security|"
                       r"insurance|indemn|bonus|one[- ]time|signing", re.I)


def _amounts(idx: DocIndex) -> tuple[list[Candidate], list[Candidate]]:
    """Classify each currency amount by the sentence that contains it (not a fixed character window)."""
    pay, total = [], []
    for page, text in idx.iter_pages():
        for start, end, amt, cur in P.find_amounts(text):
            sentence = idx.sentence_at(page, start, end)
            sent_start = max(text.rfind(".", 0, max(0, start - 4)), text.rfind("\n\n", 0, start)) + 1
            near_before = text[max(sent_start, start - 60):start]
            if OTHER_CTX.search(near_before) and not TOTAL_CTX.search(near_before):
                continue
            tail = text[end:end + 40].split(".")[0].split("\n")[0]
            if TOTAL_CTX.search(sentence) and not re.search(r"per\s+(month|week|quarter|annum|year)|monthly|weekly|every|each", tail, re.I):
                total.append(_cand(idx, page, start, end, str(amt), f"{amt} {cur}", alternatives=[{"currency": cur}]))
            elif OTHER_CTX.search(sentence) and not RECURRING_CTX.search(sentence):
                continue
            elif RECURRING_CTX.search(sentence):
                c = _cand(idx, page, start, end, str(amt), f"{amt} {cur}", alternatives=[{"currency": cur}])
                c.alternatives[0]["frequency"] = P.detect_frequency(sentence)
                pay.append(c)
    return pay, total


def _resolve(cands: list[Candidate], what: str) -> Candidate | None:
    """Pick one candidate; flag a conflict when distinct values are present."""
    if not cands:
        return None
    distinct = {c.value for c in cands}
    best = cands[0]
    if len(distinct) > 1:
        best.issues.append(f"conflicting {what} values in document: {', '.join(sorted(distinct))}")
        best.alternatives = [{"value": c.value, "page": c.page, "source_text": c.source_text} for c in cands]
        best.confidence = round(best.confidence * 0.5, 3)
    elif len(cands) > 1:
        best.confidence = min(0.99, round(best.confidence + 0.05, 3))  # corroborated
    return best


_PARTIES = re.compile(
    r"between\s*:?\s*(?P<a>[A-Z][\w&.,'’\- \n]{2,120}?)\s*(?:,[^()]{0,160})?\((?:hereinafter\s+)?(?:referred\s+to\s+as\s+)?(?:the\s+)?[\"“']?(?P<la>[A-Za-z ]{3,30})[\"”']?\)"
    r".{0,40}?\band\s+(?P<b>[A-Z][\w&.,'’\- \n]{2,120}?)\s*(?:,[^()]{0,160})?\((?:hereinafter\s+)?(?:referred\s+to\s+as\s+)?(?:the\s+)?[\"“']?(?P<lb>[A-Za-z ]{3,30})[\"”']?\)",
    re.I | re.S)
_LABELLED = re.compile(r"^\s*(contractor|consultant|service provider|vendor|freelancer|client|company)\s*(?:name)?\s*[:\-]\s*(.+)$", re.I | re.M)
CONTRACTOR_LABELS = ("contractor", "consultant", "service provider", "vendor", "freelancer", "supplier")
_CONTRACT_ID = re.compile(r"(?:contract|agreement)\s*(?:no\.?|number|id|ref(?:erence)?)\s*[:#.\-]?\s*([A-Z0-9][A-Z0-9\-/_.]{2,40})", re.I)
_START_CTX = re.compile(r"(effective|commenc\w*|start\w*|begin\w*|with\s+effect\s+from|from)\b", re.I)
_END_CTX = re.compile(r"(until|expir\w*|end\w*|terminat\w*\s+on|through|till|up\s+to|ending)\b", re.I)
_FIRST_PAY = re.compile(r"first\s+(?:payment|instal+ment)[^.]{0,60}?", re.I)


def extract_rules(pages: list[dict], day_first: bool = True) -> dict[str, Candidate]:
    idx = DocIndex(pages)
    out: dict[str, Candidate] = {}
    full = "\n".join(t for _, t in idx.iter_pages())
    first_page, first_text = next(iter(idx.iter_pages()), (1, ""))

    # Title: first heading line containing AGREEMENT/CONTRACT.
    for ln_start, line in _line_offsets(first_text):
        if re.search(r"\b(agreement|contract)\b", line, re.I) and len(line) < 140:
            out["contract_title"] = _cand(idx, first_page, ln_start, ln_start + len(line), line.strip().title(), line.strip())
            break
    for page, text in idx.iter_pages():
        if m := _CONTRACT_ID.search(text):
            out["contract_id"] = _cand(idx, page, m.start(1), m.end(1), m.group(1).rstrip("."), m.group(1).rstrip("."))
            break

    # Parties.
    for page, text in idx.iter_pages():
        if m := _PARTIES.search(text):
            a, la, b, lb = (" ".join(m.group(g).split()).strip(" ,") for g in ("a", "la", "b", "lb"))
            contractor, cp = (a, b) if any(l in la.lower() for l in CONTRACTOR_LABELS) else (b, a)
            out["parties"] = _cand(idx, page, m.start(), m.end(), f"{a} ({la}); {b} ({lb})", f"{a} ({la}) and {b} ({lb})")
            if any(l in (la + lb).lower() for l in CONTRACTOR_LABELS):
                s = m.start("a") if contractor == a else m.start("b")
                out["contractor_legal_name"] = _cand(idx, page, s, s + len(contractor), contractor, contractor)
                s2 = m.start("b") if contractor == a else m.start("a")
                out["counterparty_name"] = _cand(idx, page, s2, s2 + len(cp), cp, cp)
            break
    if "contractor_legal_name" not in out:
        for page, text in idx.iter_pages():
            for m in _LABELLED.finditer(text):
                label, name = m.group(1).lower(), m.group(2).strip()
                key = "contractor_legal_name" if label in CONTRACTOR_LABELS else "counterparty_name"
                if key not in out:
                    out[key] = _cand(idx, page, m.start(2), m.end(2), name[:200], name[:200], conf=0.85)

    # Amounts / currency / frequency.
    pay, total = _amounts(idx)
    # Currency/frequency confidence is independent of a conflict between amount VALUES when all candidates agree.
    base_conf = max((p.confidence for p in pay), default=0.0)
    currencies = [p.alternatives[0]["currency"] for p in pay if p.alternatives and p.alternatives[0].get("currency")]
    freqs_found = [p.alternatives[0]["frequency"] for p in pay if p.alternatives and p.alternatives[0].get("frequency")]
    if c := _resolve(pay, "recurring payment amount"):
        out["payment_amount"] = c
        if currencies:
            cc = Candidate(currencies[0], currencies[0], c.page, c.source_text, c.bbox, c.ocr_conf, base_conf)
            if len(set(currencies)) > 1:
                cc.issues.append(f"multiple currencies for recurring amounts: {', '.join(sorted(set(currencies)))}")
                cc.confidence = round(cc.confidence * 0.5, 3)
            out["currency"] = cc
        if freqs_found:
            freq = freqs_found[0]
            same = len(set(freqs_found)) == 1
            out["frequency"] = Candidate(freq[0], freq[0].title(), c.page, c.source_text, c.bbox, c.ocr_conf,
                                         base_conf if same else round(base_conf * 0.5, 3),
                                         issues=[] if same else ["conflicting frequencies for recurring amounts"])
            if freq[1] != 1 or freq[2]:
                out["interval_count"] = Candidate(str(freq[1]), str(freq[1]), c.page, c.source_text, c.bbox, c.ocr_conf, base_conf)
                if freq[2]:
                    out["custom_unit"] = Candidate(freq[2], freq[2], c.page, c.source_text, c.bbox, c.ocr_conf, base_conf)
    if c := _resolve(total, "total value"):
        out["total_value"] = c
    if "frequency" not in out:
        for page, text in idx.iter_pages():
            f = P.detect_frequency(text)
            if f:
                m = next(pat.search(text) for pat, v in P.FREQ_PATTERNS if v == f and pat.search(text))
                out["frequency"] = _cand(idx, page, m.start(), m.end(), f[0], f[0].title(), conf=0.6,
                                         issues=["frequency inferred from document text, not tied to the amount"])
                break

    # Due rule.
    for page, text in idx.iter_pages():
        if r := P.detect_due_rule(text):
            kind, val, s, e = r
            key = "due_day_of_month" if kind == "DOM" else "due_weekday"
            disp = (f"day {val} of the month" if val != 31 else "last day of the month") if kind == "DOM" else \
                ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][val]
            out[key] = _cand(idx, page, s, e, str(val), disp)
            break

    # Dates.
    starts, ends, firsts = [], [], []
    for page, text in idx.iter_pages():
        for s, e, d, ambiguous in P.find_dates(text, day_first):
            before = text[max(0, s - 70):s]
            issues = ["ambiguous numeric date (day/month order); interpreted per policy"] if ambiguous else []
            c = _cand(idx, page, s, e, d.isoformat(), d.strftime("%d %b %Y"), conf=0.6 if ambiguous else 0.9, issues=issues)
            if _FIRST_PAY.search(before[-70:]):
                firsts.append(c)
            elif _END_CTX.search(before[-35:]):
                ends.append(c)
            elif _START_CTX.search(before[-45:]):
                starts.append(c)
    if c := _resolve(starts, "start date"):
        out["start_date"] = c
    if c := _resolve(ends, "end date"):
        out["end_date"] = c
    if c := _resolve(firsts, "first payment date"):
        out["first_payment_date"] = c
    if "end_date" not in out and "start_date" in out:
        for page, text in idx.iter_pages():
            if dur := P.detect_duration(text):
                from datetime import date as _d

                sd = _d.fromisoformat(out["start_date"].value)
                ed = sd + dur[0] - timedelta(days=1)
                c = _cand(idx, page, dur[1], dur[2], ed.isoformat(), ed.strftime("%d %b %Y"), conf=0.75, derived=True,
                          issues=["end date derived from contract term and start date"])
                out["end_date"] = c
                break

    # Simple clause classifiers.
    def first_match(pattern: str, field_name: str, value_fn=None, conf: float = 0.85) -> None:
        rx = re.compile(pattern, re.I)
        for page, text in idx.iter_pages():
            if m := rx.search(text):
                sentence = idx.sentence_at(page, m.start(), m.end())
                val = value_fn(m, sentence) if value_fn else sentence
                if val is not None:
                    out[field_name] = _cand(idx, page, m.start(), m.end(), str(val), str(val)[:200], conf=conf)
                return

    first_match(r"\bin\s+advance\b", "payment_timing", lambda m, s: "ADVANCE")
    if "payment_timing" not in out:
        first_match(r"\bin\s+arrears?\b", "payment_timing", lambda m, s: "ARREARS")
    first_match(r"grace\s+period\s+of\s+(\d{1,3})\s+days", "grace_period_days", lambda m, s: m.group(1))
    first_match(r"\bpro[- ]?rat(?:a|ed)\b", "proration_rule", lambda m, s: "DAILY")
    first_match(r"(?:upon|on|against|after)\s+(?:receipt\s+of\s+)?(?:a\s+)?(?:valid\s+|proper\s+|duly\s+)?(?:tax\s+)?invoice|invoice[s]?\s+(?:shall|must|will)\s+be\s+(?:submitted|raised|issued)",
                "invoice_required", lambda m, s: "true")
    first_match(r"(\d{1,3})\s*(?:\(\w+\)\s*)?days[’']?\s+(?:prior\s+)?(?:written\s+)?notice", "notice_period_days",
                lambda m, s: m.group(1))
    first_match(r"\b(TDS|withhold\w*|tax\s+deducted|GST|deduct\w*\s+at\s+source)\b", "tax_withholding")
    first_match(r"\brenew\w*", "renewal_terms")
    first_match(r"\bterminat\w*[^.]{0,160}\bnotice\b", "termination_terms")
    if "termination_terms" not in out:
        first_match(r"\btermination\b", "termination_terms")
    first_match(r"\b(deliverables?|acceptance|milestones?)\b", "deliverables", conf=0.7)
    first_match(r"\b(amend\w*|modif\w*|variation)\b[^.]{0,80}\b(writing|written|signed|mutual)", "amendment_clause")
    first_match(r"\b(account\s+(?:no\.?|number)|IFSC|SWIFT|IBAN|beneficiary\s+account)\b", "bank_details_present",
                lambda m, s: "true")
    first_match(r"\b(automatic(?:ally)?\s+(?:be\s+)?(?:paid|pay|debit\w*|transfer\w*|disburs\w*|process\w*)|"
                r"(?:paid|made|processed|disbursed|transferred|remitted)\s+automatically|standing\s+instruction|auto[- ]?pay)",
                "auto_execute_permitted", lambda m, s: "true")

    # Signatures: labelled signature lines only ("Signed: Name", "Signature: Name", "For and on behalf of X: Name").
    sig_rx = re.compile(r"^\s*(?:signed(?:\s+by)?|signature|for\s+and\s+on\s+behalf\s+of[^:\n]{0,80})\s*[:\-]\s*"
                        r"(?:name\s*[:\-]\s*)?(?:/s/\s*)?([A-Z][A-Za-z.'\- ]{2,60}?)\s*$", re.I | re.M)
    sig_names = [(page, m) for page, text in idx.iter_pages() for m in sig_rx.finditer(text)]
    if sig_names:
        page, m = sig_names[0]
        names = list(dict.fromkeys(mm.group(1).strip() for _, mm in sig_names))[:6]
        out["signatories"] = _cand(idx, page, m.start(1), m.end(1), "; ".join(names), "; ".join(names), conf=0.75)
        out["signature_present"] = Candidate("true", "Signature block with signatory names", page,
                                             out["signatories"].source_text, out["signatories"].bbox, None, 0.6,
                                             issues=["signature presence inferred from text; verify on the original"])
    else:
        out["signature_present"] = Candidate("false", "Not detected", None, None, None, None, 0.5,
                                             issues=["no signature block detected in the text layer"])
    return out


def _line_offsets(text: str):
    off = 0
    for line in text.split("\n"):
        yield off, line
        off += len(line) + 1


def summarize_amount_conflicts(cands: dict[str, Candidate]) -> list[str]:
    flags = []
    pa, tv = cands.get("payment_amount"), cands.get("total_value")
    if pa and tv and pa.value and tv.value and Decimal(pa.value) > Decimal(tv.value):
        flags.append("payment amount exceeds total contract value")
    return flags
