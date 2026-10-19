"""XYZ Ltd financial data & Q&A model.

    DEMO_PASSWORD=... python -m finguard.xyz.cli setup      # org 'xyz' + users per role + 10-year ledger into PostgreSQL
    python -m finguard.xyz.cli train                         # build corpus, train intent model, evaluate on held-out tests
    python -m finguard.xyz.cli ask "What was revenue in FY2024?"
"""

from __future__ import annotations

import json
import os
import sys
import time

from sqlalchemy import select


def _org(db):
    from finguard.models import Organization

    return db.execute(select(Organization).where(Organization.slug == "xyz")).scalar_one_or_none()


def setup() -> None:
    from finguard.cli import bootstrap, create_org, create_user
    from finguard.core.db import new_session
    from finguard.models.enums import RoleName as R
    from finguard.xyz.generator import generate, load_into_db

    pw = os.environ.get("DEMO_PASSWORD")
    if not pw:
        sys.exit("set DEMO_PASSWORD (password for the XYZ Ltd demo users)")
    db = new_session()
    try:
        bootstrap(db)
        org = _org(db) or create_org(db, "xyz", "XYZ Ltd", "admin@xyz.test", pw,
                                     {"business_day_rule": "FOLLOWING", "execution_hour": 9, "date_day_first": True, "holidays": []})
        for email, name, roles in [("analyst@xyz.test", "Kavya Analyst", [R.FINANCE_ANALYST]),
                                   ("approver@xyz.test", "Nikhil Approver", [R.FINANCE_APPROVER]),
                                   ("manager@xyz.test", "Divya Manager", [R.FINANCE_MANAGER]),
                                   ("risk@xyz.test", "Arvind Risk", [R.RISK_OFFICER]),
                                   ("auditor@xyz.test", "Leela Auditor", [R.AUDITOR]),
                                   ("contractor@xyz.test", "Sameer Contractor", [R.CONTRACTOR])]:
            create_user(db, org, email, name, pw, roles)
        _ensure_tool_policy(db)
        db.commit()
        t0 = time.time()
        ledger = generate()
        stats = load_into_db(db, org.id, ledger)
        db.commit()
        print(f"XYZ Ltd ledger loaded: {stats} in {time.time() - t0:.1f}s")
    finally:
        db.close()


def _ensure_tool_policy(db) -> None:
    """New tools are allowlisted through a NEW policy version (stored policies are never edited in place)."""
    from finguard.governance.service import activate_policy, active_policy
    from finguard.core.money import canonical_hash
    from finguard.models import Organization, PolicyVersion, User
    from finguard.services.common import principal_for

    for org in db.execute(select(Organization)).scalars():
        try:
            pv = active_policy(db, org.id)
        except Exception:
            continue
        if "read.financials" in pv.config.get("tool_risk", {}):
            continue
        cfg = json.loads(json.dumps(pv.config))
        cfg["tool_risk"]["read.financials"] = 1
        new = PolicyVersion(org_id=org.id, version=f"{pv.version}+financials", status="DRAFT", config=cfg,
                            config_hash=canonical_hash(cfg), change_note="Allowlist read.financials (company ledger Q&A tool)",
                            created_by=pv.created_by)
        db.add(new)
        db.flush()
        creator = db.get(User, pv.created_by)
        activate_policy(db, principal_for(db, creator), new)


def train_and_evaluate() -> dict:
    from finguard.core.db import new_session
    from finguard.xyz import training
    from finguard.xyz.generator import generate
    from finguard.xyz.metrics import Calc, MemorySource, SQLSource

    t0 = time.time()
    ledger = generate()
    mem = Calc(MemorySource(ledger))
    tpl = training.template_examples()
    fb, oos = training.financebench_rewrites()
    tq = training.tatqa_rewrites()
    oor = training.out_of_range_examples()
    fresh = training.fresh_examples()
    allx = tpl + fb + tq + oos + oor + fresh
    training.compute_gold(allx, mem)
    model = training.train(allx)
    from finguard.xyz import qa

    qa._model.cache_clear()
    db = new_session()
    try:
        org = _org(db)
        if org is None:
            sys.exit("run `python -m finguard.xyz.cli setup` first")
        res = training.evaluate(allx, Calc(SQLSource(db, org.id)), model)
    finally:
        db.close()
    out_dir = training.EVAL_ROOT / "results" / "xyz"
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = training.EVAL_ROOT / "data" / "xyz"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict

    (corpus_dir / "questions.jsonl").write_text("\n".join(json.dumps(asdict(e), default=str) for e in allx) + "\n")
    (out_dir / "predictions.jsonl").write_text("\n".join(json.dumps(r, default=str) for r in res["rows"]) + "\n")
    summary = {"model": model.meta, "counts": {"template_train": sum(e.split == "train" for e in tpl),
                                               "template_test": sum(e.split == "test" for e in tpl), "financebench_rewrites": len(fb),
                                               "tatqa_rewrites": len(tq), "out_of_scope": len(oos), "out_of_range": len(oor),
                                               "fresh_frozen": len(fresh)},
               "results": res["by_source"], "seconds": round(time.time() - t0, 1)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    _report(summary, res["rows"], out_dir)
    print(json.dumps(summary["results"], indent=2))
    return summary


def _report(summary: dict, rows: list[dict], out_dir) -> None:
    lines = ["# XYZ Ltd financial Q&A — training & evaluation", "",
             "Imaginary company, synthetic 10-year daily ledger (FY2017–FY2026). Answers are computed exactly from the ledger; "
             "the trained model only interprets the question (metric + operation). Gold answers come from the generator's "
             "in-memory ledger via the gold plan (independent of the parser and SQL).", "",
             f"Training questions: {summary['counts']['template_train']} (template phrasings A). Test sets use unseen phrasings.", "",
             "| Test set | n | Answer accuracy | Status accuracy | Metric (pipeline) | Operation (pipeline) | Metric (classifier only) | Metric (lexicon only) | p95 latency ms |",
             "|---|---|---|---|---|---|---|---|---|"]
    for src, r in summary["results"].items():
        lines.append(f"| {src} | {r['n']} | {r['answer_accuracy']:.1%} | {r['status_accuracy']:.1%} | {r['metric_accuracy_pipeline']:.1%} | "
                     f"{r['op_accuracy_pipeline']:.1%} | {r['metric_accuracy_classifier_only']:.1%} | {r['metric_accuracy_lexicon_only']:.1%} | "
                     f"{r['latency_ms_p95']:.0f} |")
    lines += ["", "TEMPLATE = held-out phrasings of generated questions · FINANCEBENCH = real FinanceBench questions rewritten for XYZ Ltd · "
              "TATQA = real TAT-QA arithmetic frames rewritten · OOS = original FinanceBench questions about real companies (must be refused) · "
              "OUT_OF_RANGE = periods outside the data (must answer 'no data').", "",
              "## How these numbers were produced (read before citing)", "",
              "* Development used error analysis on the TEMPLATE, FINANCEBENCH and OOS test sets (parser fixes and corrections to the "
              "FinanceBench rewrite labels), so those scores are optimistic.",
              f"* FRESH is the clean measure: hand-written questions in finguard/xyz/fresh_test.py, frozen before any error analysis. "
              f"Answer accuracy {summary['results'].get('FRESH', {}).get('answer_accuracy', 0):.1%}; refusals "
              f"{summary['results'].get('FRESH_REFUSAL', {}).get('answer_accuracy', 0):.0%}.",
              "* The model never trains on FRESH, FINANCEBENCH, TATQA or OOS questions. Gold answers are computed on the generator's "
              "in-memory ledger from the labelled plan, independent of the parser and SQL.",
              "* Some OOS FinanceBench questions name no company; answering them for XYZ Ltd still counts as an error here.", "",
              "## Sample errors", ""]
    for r in [r for r in rows if not r["correct"]][:40]:
        lines.append(f"* [{r['source']}] {r['question']} — expected {r['expected_status']} {r['metric']}/{r['op']} gold={r['gold']}; "
                     f"got {r['status']} {r['pred_metric']}/{r['pred_op']} {r['got']}")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")


def contractor_demo() -> None:
    """Onboard an invoice-based monthly contract for contractor@xyz.test (Apr 2026 – Mar 2027, ₹85,000/month).
    April–August are invoiced, accepted and paid (sandbox); September is left for the contractor to claim."""
    from datetime import date as _d

    from finguard.core.config import get_settings
    from finguard.core.db import new_session
    from finguard.models import ApprovalRequest, Contract, OcrJob, ContractDocument, ScheduledPayment, User
    from finguard.samples import ContractSpec, contract_lines, searchable_pdf
    from finguard.services import approvals, beneficiaries, contracts, invoicing
    from finguard.services.common import principal_for, service_principal
    from finguard.services.payments import process_due
    from finguard.models.enums import RoleName

    get_settings().use_job_queue = False
    db = new_session()
    try:
        org = _org(db)
        P = lambda email: principal_for(db, db.execute(select(User).where(User.org_id == org.id, User.email == email)).scalar_one())  # noqa: E731
        cp = P("contractor@xyz.test")
        existing = db.execute(select(Contract).where(Contract.contractor_id == cp.user_id)).scalars().first()
        if existing:
            print(f"contractor already has contract {existing.id} ({existing.status})")
            return
        spec = ContractSpec("XYZ-CSA-2026-014", "XYZ Ltd", "Sameer Contractor", 85000, due_day=5, start=_d(2026, 4, 1),
                            end=_d(2027, 3, 31), total_value=1020000, invoice_required=True, auto_pay=True)
        c = contracts.create_with_document(db, cp, "Consulting services — Sameer Contractor", "XYZ-CSA-2026-014.pdf",
                                           searchable_pdf(contract_lines(spec)))
        db.commit()
        db.expire_all()
        for job in db.execute(select(OcrJob).join(ContractDocument, ContractDocument.id == OcrJob.document_id)
                              .where(ContractDocument.contract_id == c.id, OcrJob.status == "QUEUED")).scalars().all():
            contracts.process_ocr_job(db, job.id, service_principal(db, org.id, RoleName.SCHEDULER))
        db.expire_all()
        beneficiaries.register_beneficiary(db, P("contractor@xyz.test"), c.id, "Sameer Contractor", "50100000140000", "ICIC0004321")
        db.commit()
        contracts.submit(db, P("contractor@xyz.test"), c.id)
        db.commit()
        contracts.validate(db, P("analyst@xyz.test"), c.id, "PASSED", "Terms checked against the signed agreement")
        db.commit()
        c = db.get(Contract, c.id)
        beneficiaries.verify_beneficiary(db, P("analyst@xyz.test"), c.beneficiary_id, True, "Bank details verified (sandbox)")
        db.commit()
        ar = db.execute(select(ApprovalRequest).where(ApprovalRequest.contract_id == c.id, ApprovalRequest.status == "OPEN")).scalars().first()
        approvals.decide(db, P("approver@xyz.test"), ar.id, "APPROVE", "Within budget; standard consulting rate")
        db.commit()
        db.expire_all()
        sps = db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id)
                         .order_by(ScheduledPayment.period_start)).scalars().all()
        for sp in sps:
            if sp.period_end < _d(2026, 9, 1):   # April–August: claim + accept
                inv = invoicing.claim(db, P("contractor@xyz.test"), sp.id)
                db.commit()
                beneficiaries.review_invoice(db, P("analyst@xyz.test"), inv.id, True, "Matches approved schedule")
                db.commit()
        from datetime import datetime, timezone

        for _ in range(10):
            if not process_due(db, datetime.now(timezone.utc), "xyz-demo").get("claimed"):
                break
        db.expire_all()
        # an approver signs off past payments that governance routed for human approval (e.g. the first payment)
        for ar in db.execute(select(ApprovalRequest).where(ApprovalRequest.contract_id == c.id, ApprovalRequest.status == "OPEN",
                                                           ApprovalRequest.kind == "PAYMENT_APPROVAL")
                             .order_by(ApprovalRequest.created_at)).scalars().all():
            approvals.decide(db, P("approver@xyz.test"), ar.id, "APPROVE", "Invoice accepted and matches the approved schedule")
            db.commit()
        db.expire_all()
        print("contract", c.id, "status", db.get(Contract, c.id).status)
        for sp in db.execute(select(ScheduledPayment).where(ScheduledPayment.contract_id == c.id)
                             .order_by(ScheduledPayment.period_start)).scalars():
            print(f"  #{sp.sequence_no} {sp.period_start}..{sp.period_end} due {sp.due_date} {sp.amount} {sp.status}")
    finally:
        db.close()


def ask(q: str) -> None:
    from finguard.core.db import new_session
    from finguard.xyz.metrics import Calc, SQLSource
    from finguard.xyz.qa import answer

    db = new_session()
    try:
        a = answer(Calc(SQLSource(db, _org(db).id)), q)
        print(a.status, "|", a.text)
        if a.plan:
            print("plan:", a.plan.metric, a.plan.op, [p.label for p in a.plan.periods], a.plan.filters, a.plan.group, a.plan.k)
    finally:
        db.close()


def kb_build() -> None:
    """Generate the company log dataset (JSONL + browsable markdown) and load it into the knowledge base for org 'xyz'."""
    from finguard.core.db import new_session
    from finguard.xyz import kb
    from finguard.xyz.generator import generate
    from finguard.xyz.kb_corpus import build_corpus
    from finguard.xyz.training import EVAL_ROOT

    t0 = time.time()
    docs = build_corpus(generate())
    out = EVAL_ROOT / "data" / "xyz" / "company_logs"
    out.mkdir(parents=True, exist_ok=True)
    (out / "documents.jsonl").write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in docs) + "\n")
    by_type: dict[str, list[dict]] = {}
    for d in docs:
        by_type.setdefault(d["doc_type"], []).append(d)
    for t, ds in by_type.items():                     # human-readable copies (daily logs: one file per fiscal year)
        if t == "daily_transactions":
            fys: dict[str, list[str]] = {}
            for d in ds:
                fys.setdefault(d["title"][-4:], []).append(d["text"])
            (out / "daily_transactions").mkdir(exist_ok=True)
            for y, texts in fys.items():
                (out / "daily_transactions" / f"transactions_{y}.md").write_text("\n\n".join(texts) + "\n")
        else:
            (out / f"{t}.md").write_text("\n\n---\n\n".join(d["text"] for d in ds) + "\n")
    db = new_session()
    try:
        org = _org(db)
        if org is None:
            sys.exit("run `python -m finguard.xyz.cli setup` first")
        stats = kb.ingest(db, org.id, docs)
        db.commit()
    finally:
        db.close()
    print(f"company log dataset: {len(docs)} documents → {out}")
    print(f"knowledge base loaded for XYZ Ltd: {stats} in {time.time() - t0:.1f}s")


def kb_eval() -> None:
    """Evaluate RAG over the PostgreSQL knowledge base (the production path) on the tuning and held-out splits."""
    from finguard.core.db import new_session
    from finguard.xyz import rag_eval
    from finguard.xyz.generator import generate
    from finguard.xyz.kb_corpus import build_corpus
    from finguard.xyz.metrics import Calc, SQLSource
    from finguard.xyz.training import EVAL_ROOT

    docs = build_corpus(generate())
    db = new_session()
    try:
        calc = Calc(SQLSource(db, _org(db).id))
        res = {s: rag_eval.evaluate(calc, docs, split=s) for s in ("tuning", "held_out")}
    finally:
        db.close()
    out = EVAL_ROOT / "results" / "xyz"
    (out / "rag_results.json").write_text(json.dumps(res, indent=2, default=str))
    lines = ["# XYZ Ltd company-knowledge RAG — evaluation", "",
             "Retriever: PostgreSQL full-text search + local TF-IDF/LSA embeddings, reciprocal-rank fusion with metadata "
             "boosts. Generator: " + ("configured LLM" if not __import__("finguard.llm.providers", fromlist=["x"]).get_provider().is_mock
                                      else "offline extractive (no LLM key configured)") + ".", "",
             "| Split | n | hit@1 | hit@5 | answer accuracy | refusal accuracy | p50 latency |", "|---|---|---|---|---|---|---|"]
    for s, r in res.items():
        lines.append(f"| {s} | {r['n']} | {r['hit_at_1']:.1%} | {r['hit_at_5']:.1%} | {r['answer_accuracy']:.1%} | "
                     f"{r['refusal_accuracy']:.1%} | {r['latency_ms_p50']:.0f} ms |")
    lines += ["", "**Honesty notes.** Retrieval and routing were tuned on the *tuning* split, so its score is optimistic. "
              "The *held-out* split was written after tuning; its first (blind) run scored 40.0% answer accuracy and "
              "80.0% hit@5 on the in-memory index. General (not question-specific) fixes were then made, so later held-out "
              "runs are no longer blind. All facts are synthetic.", "", "## Misses", ""]
    for s, r in res.items():
        for x in r["rows"]:
            if not x["correct"]:
                lines.append(f"- [{s}] {x['question']} — expected “{x['expected']}”; got: {x['answer'][:160]}")
    (out / "rag_report.md").write_text("\n".join(lines) + "\n")
    for s, r in res.items():
        print(s, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items() if k not in ("rows", "negatives")})


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "train"
    if cmd == "setup":
        setup()
    elif cmd == "contractor-demo":
        contractor_demo()
    elif cmd == "kb-build":
        kb_build()
    elif cmd == "kb-eval":
        kb_eval()
    elif cmd == "ask":
        ask(" ".join(sys.argv[2:]))
    else:
        train_and_evaluate()
