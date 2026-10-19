"""Reproducible evaluation entry point.

    python -m finguard.evaluation.run_all [--record] [--ocr-docs 20] [--skip-ocr]

Steps (all deterministic given the seed):
  1. build the derived task set + split manifest              → evaluation/data/derived/
  2. compare NO_OVERSIGHT / ALWAYS_APPROVE / RISK_TIERED on the held-out TEST split (example thresholds)
  3. calibrate PAYMENT_EXECUTION thresholds on the CALIBRATION split only; evaluate the calibrated policy on TEST
  4. ablations on TEST: without evidence factor, without uncertainty, without hard rules, reviewer-error sensitivity
  5. threshold sweep on TEST for the risk-vs-workload (Pareto) plot (descriptive only — not used for tuning)
  6. compatibility analysis on the existing 35-task calibration set
  7. OCR/extraction accuracy on synthetic contracts (native, scanned, image, DOCX)
Outputs: evaluation/results/*.json, traces/*.jsonl, *.png/*.svg, report.md. Everything is labelled SIMULATED where
human review or documents are simulated.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from finguard.core.money import canonical_hash
from finguard.evaluation import dataset, harness
from finguard.governance.policy import default_policy

RESULTS = dataset.ROOT / "results"


def _dump(name: str, obj) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / name
    p.write_text(json.dumps(obj, indent=2, default=str))
    return p


def _traces(name: str, traces: list[dict]) -> None:
    d = RESULTS / "traces"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(json.dumps(t, default=str) for t in traces) + "\n")


def _plots(comparison: dict, sweep_rows: list[dict], ocr: dict | None) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    files = []
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.scatter([r["human_review_rate"] * 100 for r in sweep_rows], [r["unsafe_execution_rate"] * 100 for r in sweep_rows],
               s=18, c="#9aa5b1", label="Risk-tiered threshold sweep")
    pts = sorted({(r["human_review_rate"], r["unsafe_execution_rate"]) for r in sweep_rows})
    front, best = [], 2.0
    for x, y in pts:
        if y < best:
            front.append((x, y))
            best = y
    if front:
        ax.plot([p[0] * 100 for p in front], [p[1] * 100 for p in front], color="#4b5563", lw=1, label="Pareto front (sweep)")
    marks = {"NO_OVERSIGHT": ("#d64545", "X"), "ALWAYS_APPROVE": ("#2563eb", "s"),
             "RISK_TIERED_EXAMPLE": ("#059669", "o"), "RISK_TIERED_CALIBRATED": ("#7c3aed", "D")}
    for name, m in comparison.items():
        if name in marks:
            c, mk = marks[name]
            ax.scatter([m["workload"]["human_review_rate"] * 100], [m["safety"]["unsafe_execution_rate"] * 100], s=110,
                       c=c, marker=mk, label=name.replace("_", " ").title(), zorder=5, edgecolors="white")
    ax.set_xlabel("Human review rate (% of tasks)")
    ax.set_ylabel("Unsafe execution rate (% of tasks)")
    ax.set_title("Risk vs. workload — held-out test split (simulated reviewer)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper right")
    for ext in ("png", "svg"):
        fig.savefig(RESULTS / f"pareto_risk_vs_workload.{ext}", dpi=150, bbox_inches="tight")
        files.append(f"pareto_risk_vs_workload.{ext}")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    names = [n for n in ("NO_OVERSIGHT", "ALWAYS_APPROVE", "RISK_TIERED_EXAMPLE", "RISK_TIERED_CALIBRATED") if n in comparison]
    metrics = [("Unsafe exec.", lambda m: m["safety"]["unsafe_execution_rate"]),
               ("Human review", lambda m: m["workload"]["human_review_rate"]),
               ("Task success", lambda m: m["utility"]["task_success_rate"]),
               ("Eligible pay processed", lambda m: m["utility"]["eligible_payment_processing_rate"])]
    w = 0.8 / len(names)
    for i, n in enumerate(names):
        ax.bar([j + i * w for j in range(len(metrics))], [f(comparison[n]) * 100 for _, f in metrics], width=w,
               label=n.replace("_", " ").title(), color=marks[n][0])
    ax.set_xticks([j + w * (len(names) - 1) / 2 for j in range(len(metrics))], [m for m, _ in metrics])
    ax.set_ylabel("%")
    ax.set_title("Policy comparison — held-out test split")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    for ext in ("png", "svg"):
        fig.savefig(RESULTS / f"policy_comparison.{ext}", dpi=150, bbox_inches="tight")
        files.append(f"policy_comparison.{ext}")
    plt.close(fig)

    if ocr:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        conds = list(ocr["conditions"])
        keys = [("amount_accuracy", "Amount"), ("currency_accuracy", "Currency"), ("date_accuracy", "Dates"),
                ("frequency_accuracy", "Frequency")]
        w = 0.8 / len(keys)
        for i, (k, label) in enumerate(keys):
            ax.bar([j + i * w for j in range(len(conds))], [(ocr["conditions"][c][k] or 0) * 100 for c in conds], width=w, label=label)
        ax.set_xticks([j + w * 1.5 for j in range(len(conds))], conds)
        ax.set_ylim(0, 105)
        ax.set_ylabel("Field accuracy (%)")
        ax.set_title("Extraction accuracy on synthetic contracts")
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.3)
        for ext in ("png", "svg"):
            fig.savefig(RESULTS / f"ocr_accuracy.{ext}", dpi=150, bbox_inches="tight")
            files.append(f"ocr_accuracy.{ext}")
        plt.close(fig)
    return files


def _score_only_plot(rows: list[dict], comparison: dict) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 5))
    sc = ax.scatter([r["human_review_rate"] * 100 for r in rows], [r["unsafe_execution_rate"] * 100 for r in rows],
                    c=[r["act_max"] for r in rows], cmap="viridis", s=22)
    fig.colorbar(sc, ax=ax, label="act_max threshold")
    for name, (c, mk) in {"NO_OVERSIGHT": ("#d64545", "X"), "ALWAYS_APPROVE": ("#2563eb", "s"),
                          "RISK_TIERED_EXAMPLE": ("#059669", "o")}.items():
        m = comparison[name]
        ax.scatter([m["workload"]["human_review_rate"] * 100], [m["safety"]["unsafe_execution_rate"] * 100], s=110, c=c,
                   marker=mk, label=name.replace("_", " ").title() + (" (with hard rules)" if "RISK" in name else ""),
                   edgecolors="white", zorder=5)
    ax.set_xlabel("Human review rate (% of tasks)")
    ax.set_ylabel("Unsafe execution rate (% of tasks)")
    ax.set_title("Score-only routing (hard rules disabled): threshold trade-off")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    out = []
    for ext in ("png", "svg"):
        fig.savefig(RESULTS / f"pareto_score_only.{ext}", dpi=150, bbox_inches="tight")
        out.append(f"pareto_score_only.{ext}")
    plt.close(fig)
    return out


def _fmt(x) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x * 100:.1f}%" if x <= 1 else f"{x:.2f}"
    return str(x)


def report(manifest: dict, comparison: dict, ablations: dict, calib: dict, cal35: dict, ocr: dict | None,
           sensitivity: dict) -> str:
    lines = ["# FinGuard AI — evaluation report", "",
             f"Generated {datetime.now(timezone.utc).isoformat()} · dataset `{manifest['dataset_version']}` "
             f"(sha256 `{manifest['sha256'][:16]}…`, {manifest['tasks']} tasks; splits {manifest['split_counts']})", "",
             "**Labelling:** Human review is **simulated** (approver miss rate 5%, risk-officer miss rate 2%, false-reject "
             "rate 2%; common random numbers per task). Contract/payment scenarios are **synthetic**. FinanceBench and "
             "TAT-QA supply real questions/evidence but contain no payment-incident labels; all safety labels follow the "
             "rubric in `finguard/evaluation/dataset.py`, never the risk score. Thresholds marked *example* are prototype "
             "values; *calibrated* thresholds were chosen on the calibration split only.", "",
             "## Policy comparison (held-out test split)", "",
             "| Metric | " + " | ".join(n.replace("_", " ").title() for n in comparison) + " |",
             "|---|" + "---|" * len(comparison)]
    rows = [("Unsafe execution rate", lambda m: m["safety"]["unsafe_execution_rate"]),
            ("False-negative safety rate", lambda m: m["safety"]["false_negative_safety_rate"]),
            ("Unauthorized executions", lambda m: m["safety"]["unauthorized_executions"]),
            ("Duplicate executions", lambda m: m["safety"]["duplicate_executions"]),
            ("Unsupported-claim rate (QA, evidence withheld)", lambda m: m["safety"]["unsupported_claim_rate"]),
            ("Policy violation rate", lambda m: m["safety"]["policy_violation_rate"]),
            ("Unsupervised high-impact executions", lambda m: m["safety"]["unsupervised_high_impact_executions"]),
            ("Human review rate", lambda m: m["workload"]["human_review_rate"]),
            ("Escalation rate", lambda m: m["workload"]["escalation_rate"]),
            ("Verify (evidence hold) rate", lambda m: m["workload"]["verify_rate"]),
            ("Reviewer minutes (simulated proxy)", lambda m: round(m["workload"]["reviewer_minutes_simulated"])),
            ("Task success rate", lambda m: m["utility"]["task_success_rate"]),
            ("Eligible payment processing rate", lambda m: m["utility"]["eligible_payment_processing_rate"]),
            ("Calculation correct rate (TAT-QA, deterministic)", lambda m: m["utility"]["calculation_correct_rate"]),
            ("Decision latency p95 (ms)", lambda m: f"{m['utility']['decision_latency_ms_p95']:.3f}"),
            ("Decision trace completeness", lambda m: m["governance"]["decision_trace_completeness"]),
            ("Decisions with evidence references", lambda m: m["governance"]["decisions_with_evidence"])]
    for label, f in rows:
        lines.append(f"| {label} | " + " | ".join(_fmt(f(m)) for m in comparison.values()) + " |")
    lines += ["", "Baselines have no risk engine, so trace completeness/evidence references are 0 by construction.", "",
              "## Calibration (calibration split only)", "",
              f"Objective: {calib['objective']}. Evaluated {calib['candidates_evaluated']} threshold pairs for "
              f"PAYMENT_EXECUTION. Chosen: act_max={calib['chosen']['act_max']}, escalate_min={calib['chosen']['escalate_min']} "
              f"(calibration split: unsafe executions={calib['chosen']['unsafe_executions']}, human review rate="
              f"{_fmt(calib['chosen']['human_review_rate'])}). Example thresholds: act_max=45, escalate_min=70.", "",
              "## Ablations (held-out test split, risk-tiered with example thresholds)", "",
              "| Variant | Unsafe exec. rate | Human review rate | Task success | Unsupervised high-impact |", "|---|---|---|---|---|"]
    for name, m in ablations.items():
        lines.append(f"| {name} | {_fmt(m['safety']['unsafe_execution_rate'])} | {_fmt(m['workload']['human_review_rate'])} | "
                     f"{_fmt(m['utility']['task_success_rate'])} | {m['safety']['unsupervised_high_impact_executions']} |")
    lines += ["", "## Reviewer-error sensitivity (simulated)", "", "| Approver miss rate | Policy | Unsafe exec. rate |", "|---|---|---|"]
    for miss, d in sensitivity.items():
        for pol, v in d.items():
            lines.append(f"| {miss} | {pol} | {_fmt(v)} |")
    lines += ["", "## Existing 35-task calibration set (compatibility analysis)", "",
              f"Labels collapsed to AUTO / CHECK (VERIFY or APPROVE) / ESCALATE; scores recomputed with "
              f"Risk = 3S+5F+3I+3E+3T+3U. Best agreement {_fmt(cal35['best']['agreement'])} at act_max={cal35['best']['act_max']}, "
              f"escalate_min={cal35['best']['escalate_min']}; example thresholds (45/70): "
              f"{_fmt(cal35['example_thresholds_45_70']['agreement']) if cal35.get('example_thresholds_45_70') else 'n/a'}. "
              f"n={cal35['n']} is small; treat as indicative only.", ""]
    if ocr:
        lines += ["## OCR / extraction accuracy (synthetic contracts)", "",
                  "| Condition | Amount | Currency | Dates | Frequency | Critical-field error rate | Wrong critical values flagged | Mean OCR conf. | s/doc |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c, r in ocr["conditions"].items():
            conf = f"{r['mean_ocr_confidence']:.1f}" if r["mean_ocr_confidence"] is not None else "n/a (text layer)"
            lines.append(f"| {c} | {_fmt(r['amount_accuracy'])} | {_fmt(r['currency_accuracy'])} | {_fmt(r['date_accuracy'])} | "
                         f"{_fmt(r['frequency_accuracy'])} | {_fmt(r['critical_field_error_rate'])} | "
                         f"{_fmt(r['wrong_critical_values_flagged_for_review'])} | {conf} | {r['seconds_per_document']:.2f} |")
        lines.append("")
    lines += ["## Development note (transparency)", "",
              "The first full run showed risk-tiered delegation answering all FinanceBench questions whose evidence was "
              "withheld (unsupported claims): for read-only queries the weighted score stays below the ACT threshold even "
              "with E=5. The same failure was reproduced on the dev split (40/40). The fix implements the specification's "
              "\"missing mandatory evidence\" deterministic override for factual agent answers (rule AG_EVIDENCE_PRESENT → "
              "VERIFY). No thresholds were changed using test-split results. The \"without hard rules\" ablation shows the "
              "score-only behaviour.", "",
              "## Limitations", "",
              "* Reviewer behaviour is simulated; real reviewer accuracy, fatigue and latency were not measured.",
              "* Synthetic scenario families encode the rubric's notion of safety; results show consistency of the policy "
              "with that rubric, not real-world loss rates.",
              "* No LLM was called in this run (mock provider); FinanceBench answer correctness is therefore not measured. "
              "TAT-QA correctness measures the deterministic calculator on gold derivations (percent-scale answers accepted in "
              "ratio×100 or percentage-point form).",
              "* OCR metrics use generated documents with one font family; real scans vary more. Some name misreads "
              "(e.g. 'Iyer'→'lyer') occur at high OCR confidence and are not caught by confidence thresholds; the "
              "validation step's party-identity check against the registered contractor surfaces them as warnings.",
              "* Circularity: most synthetic unsafe families correspond to conditions that the platform's hard rules check "
              "by design. The safety advantage over ALWAYS_APPROVE therefore mainly reflects deterministic rule coverage "
              "versus simulated reviewer error; it is not evidence of real-world performance. The ablation without hard "
              "rules isolates the score-based component.",
              "* Baselines are evaluated on the same rubric; ALWAYS_APPROVE's results depend entirely on the reviewer model.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="store EvaluationRun rows in the database")
    ap.add_argument("--skip-ocr", action="store_true")
    ap.add_argument("--ocr-docs", type=int, default=20)
    args = ap.parse_args(argv)

    tasks = dataset.build()
    manifest = dataset.write(tasks)
    test = [t for t in tasks if t.split == "test"]
    cal = [t for t in tasks if t.split == "calibration"]
    base = default_policy()

    comparison, all_traces = {}, {}
    for name, kw in (("NO_OVERSIGHT", {}), ("ALWAYS_APPROVE", {}), ("RISK_TIERED_EXAMPLE", {"policy": base})):
        m, tr = harness.run_policy(test, "RISK_TIERED" if name.startswith("RISK") else name, **kw)
        for t in tr:
            t["policy"] = name
        comparison[name], all_traces[name] = m, tr
    calib = harness.calibrate_payment_thresholds(cal)
    calibrated = harness.with_thresholds(base, "PAYMENT_EXECUTION", calib["chosen"]["act_max"], calib["chosen"]["escalate_min"])
    m, tr = harness.run_policy(test, "RISK_TIERED", policy=calibrated)
    for t in tr:
        t["policy"] = "RISK_TIERED_CALIBRATED"
    comparison["RISK_TIERED_CALIBRATED"], all_traces["RISK_TIERED_CALIBRATED"] = m, tr

    ablations = {}
    for name, ab in (("full (example thresholds)", {}), ("without evidence factor (E=0)", {"no_evidence": True}),
                     ("without uncertainty factor (U=0)", {"no_uncertainty": True}),
                     ("without hard rules (score only)", {"no_hard_rules": True}),
                     ("without evidence & uncertainty", {"no_evidence": True, "no_uncertainty": True})):
        ablations[name], tr = harness.run_policy(test, "RISK_TIERED", policy=base, ablate=ab)
        _traces(f"ablation_{name.split(' (')[0].replace(' ', '_').replace('&', 'and')}.jsonl", tr)
    sensitivity = {}
    for miss in (0.0, 0.05, 0.15, 0.30):
        rv = harness.ReviewerModel(approver_miss_rate=miss, risk_officer_miss_rate=miss / 2)
        sensitivity[str(miss)] = {p: harness.run_policy(test, p if p != "RISK_TIERED" else "RISK_TIERED", policy=base,
                                                        reviewer=rv)[0]["safety"]["unsafe_execution_rate"]
                                  for p in ("ALWAYS_APPROVE", "RISK_TIERED")}
    sweep_rows = harness.sweep(test, actions=("PAYMENT_EXECUTION", "AGENT_QUERY", "AGENT_TOOL"))
    score_only_rows = harness.sweep(test, actions=("PAYMENT_EXECUTION", "AGENT_QUERY", "AGENT_TOOL", "CONTRACT_APPROVAL",
                                                   "AMENDMENT_APPROVAL"), act_values=range(10, 95, 5),
                                    esc_offsets=(5, 10, 20, 30), ablate={"no_hard_rules": True})
    cal35 = harness.calibration_35_analysis(dataset.calibration_35())
    ocr = None
    if not args.skip_ocr:
        from finguard.evaluation.ocr_eval import evaluate

        ocr = evaluate(args.ocr_docs)

    for name, tr in all_traces.items():
        _traces(f"{name.lower()}.jsonl", tr)
    _dump("policy_comparison.json", comparison)
    _dump("ablations.json", ablations)
    _dump("reviewer_sensitivity.json", sensitivity)
    _dump("calibration_payment_thresholds.json", calib)
    _dump("calibration_35_analysis.json", cal35)
    _dump("threshold_sweep_test.json", sweep_rows)
    _dump("threshold_sweep_score_only_test.json", score_only_rows)
    if ocr:
        _dump("ocr_accuracy.json", ocr)
    files = _plots(comparison, sweep_rows, ocr) + _score_only_plot(score_only_rows, comparison)
    (RESULTS / "report.md").write_text(report(manifest, comparison, ablations, calib, cal35, ocr, sensitivity))
    summary = {"manifest": manifest, "comparison": {k: {"safety": v["safety"], "workload": v["workload"],
                                                        "utility": v["utility"], "governance": v["governance"]}
                                                    for k, v in comparison.items()},
               "calibrated_thresholds": calib["chosen"], "plots": files}
    _dump("summary.json", summary)
    if args.record:
        _record(manifest, comparison, ablations, calib, cal35, ocr, files, canonical_hash(base))
    print(json.dumps({k: {"unsafe": round(v["safety"]["unsafe_execution_rate"], 4),
                          "human_review": round(v["workload"]["human_review_rate"], 4),
                          "success": round(v["utility"]["task_success_rate"], 4)} for k, v in comparison.items()}, indent=2))
    return summary


def _record(manifest, comparison, ablations, calib, cal35, ocr, files, policy_hash) -> None:
    from finguard.core.db import session_scope
    from finguard.models import EvaluationRun

    with session_scope() as db:
        for name, m in comparison.items():
            db.add(EvaluationRun(name=name, kind="POLICY_COMPARISON", dataset_version=manifest["dataset_version"],
                                 split="test", policy_config_hash=policy_hash, metrics=json.loads(json.dumps(m, default=str)),
                                 artifacts={"plots": files, "traces": f"traces/{name.lower()}.jsonl"}, is_simulated=True))
        db.add(EvaluationRun(name="Ablations", kind="ABLATION", dataset_version=manifest["dataset_version"], split="test",
                             policy_config_hash=policy_hash, metrics=json.loads(json.dumps(ablations, default=str)),
                             artifacts={}, is_simulated=True))
        db.add(EvaluationRun(name="Payment threshold calibration", kind="CALIBRATION",
                             dataset_version=manifest["dataset_version"], split="calibration", policy_config_hash=policy_hash,
                             metrics={"chosen": calib["chosen"], "objective": calib["objective"],
                                      "calibration_35": {"best": cal35["best"], "example": cal35["example_thresholds_45_70"]}},
                             artifacts={}, is_simulated=True))
        if ocr:
            db.add(EvaluationRun(name="OCR extraction accuracy", kind="OCR", dataset_version="synthetic-contracts-v1",
                                 split="synthetic", metrics=ocr, artifacts={"plots": [f for f in files if "ocr" in f]},
                                 is_simulated=True))


if __name__ == "__main__":
    main()
