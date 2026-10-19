"""OCR / extraction accuracy on synthetic contracts with known ground truth.

Conditions: native (searchable) PDF, scanned PDF (light degradation), scanned PDF (heavy degradation), PNG, DOCX.
Metrics: field-level normalized exact match per field, amount/currency/date/frequency accuracy, critical-field error
rate, share of critical fields the pipeline flagged for human review (i.e., correctly refused to trust).
These are measurements on SYNTHETIC documents, not on real-world contracts.
"""

from __future__ import annotations

import random
import time
from datetime import date

from dateutil.relativedelta import relativedelta

from finguard.documents.text_extraction import extract
from finguard.extraction.pipeline import field_status, run_extraction
from finguard.samples import ContractSpec, contract_lines, docx, png, scanned_pdf, searchable_pdf

FIELDS = ["contract_id", "contractor_legal_name", "counterparty_name", "payment_amount", "currency", "frequency",
          "due_day_of_month", "start_date", "end_date", "total_value", "invoice_required", "notice_period_days"]
CRITICAL = ["contractor_legal_name", "payment_amount", "currency", "frequency", "start_date", "end_date"]
NAMES = ["Priya Raman", "Rahul Verma", "Ananya Iyer", "Vikram Shah", "Meera Nair", "Karan Malhotra", "Sneha Kapoor",
         "Dev Patel", "Arjun Rao", "Lakshmi Menon"]
COMPANIES = ["Acme Analytics Pvt Ltd", "Globex Consulting LLP", "Initech Systems Pvt Ltd", "Umbrella Data Services"]


def specs(n: int, seed: int = 11) -> list[ContractSpec]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        start = date(2026, rng.randint(1, 12), 1)
        months = rng.choice([6, 12, 24])
        amount = rng.choice([12000, 20000, 25000, 45000, 60000, 85000, 150000, 240000])
        out.append(ContractSpec(
            contract_no=f"EV-{2026}-{i:04d}", company=rng.choice(COMPANIES), contractor=rng.choice(NAMES), amount=amount,
            currency="INR", frequency=rng.choice(["MONTHLY", "MONTHLY", "QUARTERLY"]), due_day=rng.choice([1, 5, 7, 10, 15]),
            start=start, end=start + relativedelta(months=months) - relativedelta(days=1),
            total_value=amount * (months if rng.random() < 0.5 else 0) or None,
            invoice_required=rng.random() < 0.4, notice_days=rng.choice([15, 30, 60]),
            currency_style=rng.choice(["Rs.", "INR"])))
    return out


CONDITIONS = {
    "native_pdf": lambda s: (searchable_pdf(contract_lines(s)), "application/pdf"),
    "scanned_light": lambda s: (scanned_pdf(contract_lines(s), noise=0.001, blur=0.4, rotate=0.3), "application/pdf"),
    "scanned_heavy": lambda s: (scanned_pdf(contract_lines(s), noise=0.02, blur=1.4, rotate=1.2, seed=3), "application/pdf"),
    "png_image": lambda s: (png(contract_lines(s), noise=0.004, blur=0.6, rotate=0.5), "image/png"),
    "docx": lambda s: (docx(contract_lines(s)), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
}


def evaluate(n: int = 20, conditions: list[str] | None = None) -> dict:
    results = {}
    for cond in conditions or list(CONDITIONS):
        per_field = {f: [0, 0] for f in FIELDS}
        crit_errors = crit_total = flagged_wrong = wrong_total = 0
        ocr_conf = []
        t0 = time.perf_counter()
        for s in specs(n):
            data, mime = CONDITIONS[cond](s)
            layer = extract(data, mime)
            if layer.mean_ocr_confidence is not None:
                ocr_conf.append(layer.mean_ocr_confidence)
            out = run_extraction(layer)
            truth = s.truth()
            for f in FIELDS:
                exp = truth.get(f)
                if exp is None:
                    continue
                c = out.fields.get(f)
                got = c.value if c else None
                ok = got is not None and str(got).strip().lower() == str(exp).strip().lower()
                per_field[f][0] += ok
                per_field[f][1] += 1
                if f in CRITICAL:
                    crit_total += 1
                    from finguard.extraction.schema import FIELD_MAP

                    status, _ = field_status(c, True, FIELD_MAP[f].kind)
                    if not ok:
                        crit_errors += 1
                        wrong_total += 1
                        flagged_wrong += status != "OK"  # the pipeline refused to trust the wrong value
        elapsed = time.perf_counter() - t0
        acc = {f: (v[0] / v[1] if v[1] else None) for f, v in per_field.items()}
        results[cond] = {
            "documents": n, "field_accuracy": acc,
            "amount_accuracy": acc["payment_amount"], "currency_accuracy": acc["currency"],
            "date_accuracy": (acc["start_date"] + acc["end_date"]) / 2,
            "frequency_accuracy": acc["frequency"],
            "critical_field_error_rate": crit_errors / crit_total if crit_total else None,
            "wrong_critical_values_flagged_for_review": (flagged_wrong / wrong_total) if wrong_total else None,
            "mean_ocr_confidence": sum(ocr_conf) / len(ocr_conf) if ocr_conf else None,
            "seconds_per_document": elapsed / n,
        }
    return {"synthetic": True, "conditions": results,
            "note": "Synthetic contracts generated by finguard.samples; fields compared after deterministic normalization."}
