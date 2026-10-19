"""Structured agent output.

Every agent run returns, alongside the plain-text answer, a structured document the UI (or any API client) renders:

    {"title", "summary", "status",
     "blocks": [ {"type": "highlights", "items": [{"label", "value", "note"?, "detail"?, "tone"?}]}
               | {"type": "table", "title", "columns": [...], "rows": [[...]], "chart"?: {...}}
               | {"type": "kv", "title", "items": [[key, value], ...]}
               | {"type": "list", "title", "items": [str, ...]}
               | {"type": "quote", "title", "items": [{"text", "source"}]} ],
     "assumptions": [...], "limitations": [...], "sources": [{"label", "detail"}], "tools": [...] }

Values are display strings computed deterministically from tool results — the structure never contains model-invented figures.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _money(v: Any, cur: str = "INR") -> str:
    if v in (None, ""):
        return "—"
    d = Decimal(str(v))
    sym = "₹" if cur == "INR" else f"{cur} "
    if abs(d) >= Decimal(10_000_000) and cur == "INR":
        return f"₹{(d / Decimal(10_000_000)).quantize(Decimal('0.01')):,} crore"
    whole, frac = f"{abs(d):.2f}".split(".")
    if cur == "INR" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    elif cur != "INR":
        whole = f"{int(whole):,}"
    return f"{'-' if d < 0 else ''}{sym}{whole}.{frac}"


def _title(s: str) -> str:
    return s.replace("_", " ").title() if s else "—"


def tool_section(name: str, d: dict) -> dict:
    """Convert one tool result into {title, summary, blocks, sources, assumptions}."""
    out: dict = {"title": _title(name), "summary": "", "blocks": [], "sources": [], "assumptions": []}
    if name == "company_financials":
        st = d.get("structured") or {}
        return {"title": st.get("title", "Company financials"), "summary": st.get("summary", d.get("answer", "")),
                "blocks": st.get("blocks", []), "sources": st.get("sources", []), "assumptions": st.get("assumptions", []),
                "formula": st.get("formula"), "status": st.get("status")}
    if name == "get_contract_terms":
        t = d.get("approved_terms")
        out["title"] = f"Contract terms — {d.get('title')}"
        if t:
            out["summary"] = (f"“{d['title']}” is {d['status']}: {_money(t['payment_amount'], t['currency'])} {t['frequency'].lower()}"
                              f"{', due day ' + str(t['due_day_of_month']) if t.get('due_day_of_month') else ''}, "
                              f"{t['start_date']} → {t['end_date']} (approved version {t['version_no']}).")
            out["blocks"].append({"type": "highlights", "items": [
                {"label": "Payment", "value": _money(t["payment_amount"], t["currency"]), "note": _title(t["frequency"])},
                {"label": "Term", "value": f"{t['start_date']} → {t['end_date']}"},
                {"label": "Status", "value": _title(d["status"])}]})
            out["blocks"].append({"type": "kv", "title": "Approved terms", "items": [
                ["Version", str(t["version_no"])], ["Due day", str(t.get("due_day_of_month") or "—")],
                ["Total contract value", _money(t["total_value"], t["currency"]) if t.get("total_value") else "not stated"],
                ["Invoice required", "Yes" if t.get("invoice_required") else "No"],
                ["Automatic execution permitted", "Yes" if t.get("auto_execute_permitted") else "No"],
                ["Effective from", t.get("effective_from") or "contract start"]]})
        else:
            out["summary"] = f"“{d.get('title')}” is {d.get('status')} and has no approved terms yet."
            if d.get("extracted_fields"):
                out["blocks"].append({"type": "kv", "title": "Extracted (not yet approved) values",
                                      "items": [[_title(k), str(v)] for k, v in d["extracted_fields"].items()]})
    elif name in ("list_payments", "upcoming_payments"):
        ps = d.get("payments", [])
        out["title"] = "Upcoming payments" if name == "upcoming_payments" else "Payments"
        out["summary"] = (f"{len(ps)} payment{'s' if len(ps) != 1 else ''} found"
                          + (f"; next due {ps[0]['due_date']} ({_money(ps[0]['amount'], ps[0]['currency'])})." if ps else "."))
        if ps:
            out["blocks"].append({"type": "table", "title": out["title"], "columns": ["#", "Due date", "Service period", "Amount", "Status", "Note"],
                                  "rows": [[str(p["sequence_no"]), p["due_date"], p["period"].replace("..", " → "), _money(p["amount"], p["currency"]),
                                            _title(p["status"]), (p.get("hold_reason") or "")[:90] if p["status"] not in ("SCHEDULED", "PAID") else ""]
                                           for p in ps[:25]]})
    elif name == "calculate_contract_totals":
        cur = d.get("currency") or "INR"
        out["title"] = "Contract totals"
        out["summary"] = (f"Paid {_money(d['paid_total'], cur)} of {_money(d['scheduled_total'], cur)} scheduled "
                          f"({d['paid_count']}/{d['occurrences']} payments); remaining {_money(d['remaining'], cur)}.")
        items = [{"label": "Scheduled", "value": _money(d["scheduled_total"], cur)},
                 {"label": "Paid", "value": _money(d["paid_total"], cur), "tone": "good"},
                 {"label": "Remaining", "value": _money(d["remaining"], cur)}]
        if d.get("next_due"):
            items.append({"label": "Next due", "value": d["next_due"]["due_date"], "note": _money(d["next_due"]["amount"], cur)})
        out["blocks"].append({"type": "highlights", "items": items})
        out["sources"].append({"label": "Calculation", "detail": d.get("method", "")})
    elif name == "explain_decision":
        if not d.get("decision"):
            out["summary"] = d.get("note", "No matching decision found.")
        else:
            out["title"] = "Governance decision"
            out["summary"] = f"Decision: {d['decision']}. " + (d.get("reasons") or [""])[0]
            items = [{"label": "Decision", "value": d["decision"]}]
            if d.get("risk_score"):
                items.append({"label": "Risk score", "value": f"{Decimal(d['risk_score']):.1f} / 100"})
            if d.get("required_approver_role"):
                items.append({"label": "Needs", "value": _title(d["required_approver_role"])})
            out["blocks"].append({"type": "highlights", "items": items})
            if d.get("risk_factors"):
                out["blocks"].append({"type": "table", "title": "Risk factors", "columns": ["Factor", "Score (0–5)", "Why"],
                                      "rows": [[f"{k} · {v['name']}", v["value"], "; ".join(v.get("rationale", []))[:140]]
                                               for k, v in d["risk_factors"].items() if v]})
            out["blocks"].append({"type": "list", "title": "Reasons", "items": d.get("reasons", [])})
            if d.get("required_next_actions"):
                out["blocks"].append({"type": "list", "title": "Next steps", "items": d["required_next_actions"]})
    elif name == "compare_amendment":
        if not d.get("diff"):
            out["summary"] = d.get("note", "No amendment found.")
        else:
            out["title"] = "Amendment comparison"
            out["summary"] = f"Amendment {_title(d['status']).lower()}, effective {d['effective_date']}: " + \
                             "; ".join(f"{_title(x['field']).lower()} {x['old']} → {x['new']}" for x in d["diff"]) + "."
            out["blocks"].append({"type": "table", "title": "Proposed changes", "columns": ["Field", "Current", "Proposed"],
                                  "rows": [[_title(x["field"]), x["old"], x["new"]] for x in d["diff"]]})
            out["blocks"].append({"type": "kv", "title": "Details", "items": [["Status", _title(d["status"])], ["Effective", d["effective_date"]],
                                                                              ["Reason", d.get("reason") or "—"], ["Risk score", d.get("risk_score") or "—"]]})
    elif name == "search_contract_evidence":
        ms = d.get("matches", [])
        out["title"] = "Contract evidence"
        out["summary"] = f"{len(ms)} matching passage{'s' if len(ms) != 1 else ''} in your accessible documents." if ms else \
            "No matching text found in your accessible documents."
        if ms:
            out["blocks"].append({"type": "quote", "title": "Matching passages", "items": [
                {"text": m["snippet"][:400], "source": f"{m['contract']} · page {m['page']}"
                 + (" · contains instruction-like text (treated as data)" if m.get("untrusted_instruction_like_content") else "")} for m in ms[:5]]})
    elif name == "prepare_payment_request":
        out["title"] = "Payment request (governance check)"
        out["summary"] = f"Payment {_money(d['amount'], d['currency'])}: {d['decision']} — {d['note']}."
        out["blocks"].append({"type": "highlights", "items": [
            {"label": "Decision", "value": d["decision"]}, {"label": "Amount", "value": _money(d["amount"], d["currency"]), "note": d["amount_source"]},
            {"label": "Risk score", "value": f"{Decimal(d['risk_score']):.1f} / 100"}]})
        out["blocks"].append({"type": "list", "title": "Reasons", "items": d.get("reasons", [])})
    elif name == "invoice_draft":
        items = d.get("items", [])
        out["title"] = f"Invoice for {d.get('month_label')}"
        if not items:
            out["summary"] = f"You have no scheduled payment covering {d.get('month_label')}."
        for it in items:
            ex = it.get("existing_invoice")
            state = (f"already submitted as {ex['number']} ({ex['status'].lower()})" if ex else
                     "ready to submit — open Dashboard → Last month's invoice and click “Submit invoice claim”" if it["claimable"]
                     else f"not claimable (payment {it['payment_status'].lower()})")
            out["summary"] = (f"Invoice {it['invoice_number']} for {it['period_start']} → {it['period_end']}: "
                              f"{_money(it['amount'], it['currency'])} — {state}.")
            out["blocks"].append({"type": "highlights", "items": [
                {"label": "Invoice no.", "value": it["invoice_number"]},
                {"label": "Amount", "value": _money(it["amount"], it["currency"]), "note": it["amount_source"]},
                {"label": "Payment due", "value": it["due_date"], "note": it["payment_status"].replace("_", " ").title()}]})
            out["blocks"].append({"type": "kv", "title": "Invoice details", "items": [
                ["From", it["seller"]["name"] + (f" ({it['seller']['company']})" if it["seller"].get("company") else "")],
                ["Bill to", it["buyer"]["name"]], ["Contract", f"{it['contract']['reference'] or it['contract']['title']} v{it['contract']['version']}"],
                ["Service period", f"{it['period_start']} → {it['period_end']}"], ["Description", it["description"]],
                ["Pay to", it.get("payout_account") or "—"], ["Status", state]]})
        out["assumptions"].append("The agent drafts invoices but cannot submit them; submitting is your action in the app.")
    elif name == "create_incident_report":
        out["title"] = "Incident reported"
        out["summary"] = f"Incident recorded for risk review (audit #{d['incident_audit_seq']})."
    else:
        out["summary"] = d.get("note", "")
    return out


def compose(results: list[tuple[str, dict, Any, str | None]], limitations: list[str], text_answer: str) -> dict:
    sections, errors = [], []
    for name, _args, res, err in results:
        if err:
            errors.append(f"{_title(name)}: {err}")
        elif res is not None:
            sections.append(tool_section(name, res.data))
    blocks: list[dict] = []
    sources: list[dict] = []
    assumptions: list[str] = []
    for i, sec in enumerate(sections):
        if len(sections) > 1:
            blocks.append({"type": "heading", "text": sec["title"], "summary": sec["summary"]})
        blocks.extend(sec.get("blocks", []))
        sources.extend(sec.get("sources", []))
        assumptions.extend(sec.get("assumptions", []))
        if sec.get("formula"):
            sources.insert(0, {"label": "Formula", "detail": sec["formula"]})
    if errors:
        blocks.append({"type": "list", "title": "Not available", "items": errors})
    main = sections[0] if sections else {"title": "Answer", "summary": text_answer}
    status = main.get("status") or ("ANSWERED" if sections else "UNAVAILABLE")
    return {"title": main["title"] if len(sections) <= 1 else "Answer", "summary": main["summary"] or text_answer,
            "status": status, "blocks": blocks, "assumptions": list(dict.fromkeys(assumptions)),
            "limitations": limitations, "sources": sources, "tools": [r[0] for r in results]}
