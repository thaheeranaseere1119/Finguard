"""Evaluation of the company-knowledge RAG path (retrieval + answer), offline and reproducible.

Questions are written in natural language (not copied from the documents). Expected answers are short facts that must
appear in the answer, and the expected source document must be retrieved. Dossier questions are generated from the
corpus so their expectations always match the data. Negative questions (external company, unrelated topic) must be
refused, never answered from loosely related passages.

Metrics: retrieval hit@1 / hit@5 (expected document among the top-k chunks), answer accuracy (expected fact in the
answer text), refusal accuracy (negatives), latency.
"""

from __future__ import annotations

import re
import statistics
import time

# (question, expected substring in the answer, expected doc_key prefix)
FIXED: list[tuple[str, str, str]] = [
    ("Who is the CEO of XYZ?", "Arjun Mehta", "profile"),
    ("Who is the CFO?", "Nandini Shah", "profile"),
    ("Who is the chief technology officer?", "Lakshmi Narayanan", "profile"),
    ("Who leads human resources?", "Devika Pillai", "profile"),
    ("When was the company founded?", "2009", "profile"),
    ("Where is XYZ's head office?", "Bengaluru", "profile"),
    ("Who are the statutory auditors?", "Rao & Associates", "profile"),
    ("Which bank does the company use?", "Coral Bank", "profile"),
    ("Who are the independent directors on the board?", "Priya Sethi", "profile|board-"),
    ("How many employees does XYZ have?", "680", "profile"),
    ("What is the hotel cap in metro cities?", "7,500", "policy-expense"),
    ("How much per diem do employees get?", "1,500", "policy-expense"),
    ("What are the standard vendor payment terms?", "30 to 45 days", "policy-procurement"),
    ("How many days of credit do strategic accounts get?", "60 days", "policy-credit|customer-"),
    ("What is the target DSO?", "45 days", "policy-credit"),
    ("What is the dividend policy?", "20%", "policy-treasury"),
    ("What is the minimum cash balance the company keeps?", "₹20 crore", "policy-treasury"),
    ("What interest do the term loans carry?", "9.5%", "policy-treasury"),
    ("Who must approve capital expenditure above 5 crore?", "board approval", "policy-procurement"),
    ("When is advance tax provided?", "June", "policy-tax"),
    ("What happens to a contractor claim more than 10% above the contract?", "risk officer", "policy-contractors"),
    ("When was the cloud outage?", "18 January 2023", "event-2023-01-18"),
    ("When did XYZ open the Chennai office?", "14 June 2016", "event-2016-06-14"),
    ("When was the ISO 27001 recertification?", "10 November 2023", "event-2023-11-10"),
    ("When did the company move into the Mumbai office?", "1 April 2022", "event-2022-04-01"),
    ("When did XYZ win the Lumen Healthcare programme?", "12 February 2025", "event-2025-02-12"),
    ("What happened on 24 March 2020?", "lockdown", "event-2020-03-24"),
    ("What happened in May 2020?", "Coral Bank", "month-2020-05"),
    ("How was business affected in April 2020?", "lockdown", "month-2020-04"),
    ("Why did XYZ borrow money in 2020?", "liquidity facility", "month-2020-05"),
    ("What did the board ratify in July 2020?", "liquidity facility", "board-FY2021-Q1"),
    ("Which board meeting approved the new data centre?", "12 crore", "board-FY2022-Q4"),
    ("When did the board approve the Bengaluru campus expansion?", "6 crore", "board-FY2019-Q2"),
    ("Who is the account manager for Orion Retail?", "Kiran Rao", "customer-00"),
    ("What are the credit terms for Coral Bank?", "60 days", "customer-06"),
    ("What is Echo Cloud Infra's role?", "cloud capacity provider", "vendor-04"),
    ("What are the payment terms for Alpha Components?", "30–45 days", "vendor-00"),
]
# Held-out set: written AFTER retrieval was tuned on FIXED, run once, never tuned on. This is the honest score.
HELD_OUT: list[tuple[str, str, str]] = [
    ("Who co-founded XYZ along with Arjun Mehta?", "Lakshmi Narayanan", "profile"),
    ("Who looks after treasury and tax?", "Nandini Shah", "profile"),
    ("Which city is XYZ registered in?", "Bengaluru", "profile"),
    ("Which agency handles advertising for XYZ?", "Brightline", "profile"),
    ("Which travel agency books company trips?", "SkyWays", "profile|policy-expense"),
    ("Can employees fly business class?", "CFO approval", "policy-expense"),
    ("How quickly must expense claims be filed?", "30 days", "policy-expense"),
    ("When are cloud capacity bills paid?", "10th", "policy-procurement|vendor-"),
    ("What happens to customers more than 120 days overdue?", "no new hardware orders", "policy-credit"),
    ("Have any bad debts been written off?", "none", "policy-credit"),
    ("How much interest do fixed deposits earn?", "4%", "policy-treasury"),
    ("When is the final dividend paid each year?", "20 September", "policy-treasury|profile"),
    ("What effective tax rate is used for provisions?", "25.17%", "policy-tax"),
    ("Which suppliers are approved for hardware components?", "Alpha Components", "policy-procurement"),
    ("Who was appointed to lead export sales?", "Sanjay Bhatt", "event-2019-07-15|profile"),
    ("When did offices reopen after the lockdown?", "1 September 2020", "event-2020-09-01"),
    ("When did XYZ cross 550 employees?", "5 August 2024", "event-2024-08-05"),
    ("When was the managed data platform launched?", "2 February 2017", "event-2017-02-02"),
    ("What did the board note in the December 2023 quarter?", "27001", "board-FY2024-Q3"),
    ("What did the board approve for FY2027?", "budget", "board-FY2026-Q4"),
    ("What does the operations log say about October 2019?", "Festival-season", "month-2019-10"),
    ("What notes are recorded about Orion Retail?", "Hardware refresh", "customer-00"),
    ("Is Krypton Data Centres a component supplier?", "cloud capacity provider", "vendor-"),
    ("What payment terms apply to FixIt Services?", "on invoice", "vendor-"),
    ("How long has Coral Bank been our customer?", "4 April 2016", "customer-06"),   # label fixed (was a labelling error)
]
NEGATIVE: list[tuple[str, str]] = [
    ("What is XYZ's share price today?", "NO_DATA"),
    ("Who is the CEO of Wipro?", "OUT_OF_SCOPE"),
    ("Who is the CEO of Infosys?", "OUT_OF_SCOPE"),
    ("What is the weather in Bengaluru today?", "NO_DATA"),
    ("Who won the cricket world cup?", "NO_DATA"),
]


def generated(docs: list[dict]) -> list[tuple[str, str, str]]:
    """Dossier questions derived from the corpus (8 customers): account manager and customer-since date."""
    out = []
    for d in [x for x in docs if x["doc_type"] == "customer_dossier"][10:18]:
        name = d["title"].split(": ", 1)[1]
        if m := re.search(r"Account manager: ([^.]+)\.", d["text"]):
            out.append((f"Who manages the {name} account?", m.group(1), d["doc_id"]))
        if m := re.search(r"Customer since ([^;]+);", d["text"]):
            out.append((f"Since when has {name} been a customer?", m.group(1), d["doc_id"]))
    return out


def evaluate(calc, docs: list[dict], split: str = "tuning") -> dict:
    from finguard.xyz.qa import answer, knowledge_base

    kb = knowledge_base(calc)
    rows = []
    for q, expect, doc in (HELD_OUT if split == "held_out" else FIXED + generated(docs)):
        t = time.perf_counter()
        a = answer(calc, q)
        ms = (time.perf_counter() - t) * 1000
        hits = kb.search(q, 5)
        keys = [h.chunk.doc_key for h in hits]
        rows.append({"question": q, "expected": expect, "expected_doc": doc, "status": a.status, "answer": a.text,
                     "route": (a.structured or {}).get("plan", {}).get("operation"),
                     "hit1": bool(keys) and keys[0].startswith(tuple(doc.split("|"))),
                     "hit5": any(k.startswith(tuple(doc.split("|"))) for k in keys),
                     "correct": expect.lower() in (a.text or "").lower(), "ms": ms})
    neg = []
    for q, want in NEGATIVE:
        a = answer(calc, q)
        neg.append({"question": q, "expected_status": want, "status": a.status, "correct": a.status == want, "answer": a.text})
    n = len(rows)
    return {
        "n": n, "hit_at_1": sum(r["hit1"] for r in rows) / n, "hit_at_5": sum(r["hit5"] for r in rows) / n,
        "answer_accuracy": sum(r["correct"] for r in rows) / n,
        "refusal_accuracy": sum(r["correct"] for r in neg) / len(neg),
        "latency_ms_p50": statistics.median(r["ms"] for r in rows), "rows": rows, "negatives": neg,
    }
