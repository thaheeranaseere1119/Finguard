"""Policy comparison harness: NO_OVERSIGHT vs ALWAYS_APPROVE vs RISK_TIERED (+ ablations and threshold sweeps).

The RISK_TIERED policy calls the SAME pure assessor functions used by the running platform (governance.assessors),
on snapshots built from each task. Human review is SIMULATED (clearly labelled): a reviewer approves actions that
should execute and rejects those that should not, with configurable error rates, using common random numbers per
task so every policy faces the identical reviewer outcome for the same case. Reviewer minutes are a simulated proxy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import statistics
import time
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from typing import Any

from finguard.evaluation.dataset import Task, safe_eval
from finguard.governance.assessors import (AgentSnapshot, AmendmentSnapshot, ContractApprovalSnapshot, PaymentSnapshot,
                                           assess_agent, assess_amendment, assess_contract_approval, assess_payment)
from finguard.governance.policy import default_policy
from finguard.models.enums import Decision

SNAPSHOTS = {"PAYMENT_EXECUTION": (PaymentSnapshot, assess_payment),
             "CONTRACT_APPROVAL": (ContractApprovalSnapshot, assess_contract_approval),
             "AMENDMENT_APPROVAL": (AmendmentSnapshot, assess_amendment),
             "AGENT_QUERY": (AgentSnapshot, assess_agent), "AGENT_TOOL": (AgentSnapshot, assess_agent),
             "CALCULATION": (AgentSnapshot, assess_agent)}
DECIMAL_FIELDS = {"amount", "expected_amount", "amount_base", "total_value", "paid_to_date", "invoice_amount",
                  "extraction_min_confidence", "commitment_base", "payment_amount_base", "ocr_mean_confidence",
                  "old_amount", "new_amount", "commitment_delta_base", "new_amount_base", "evidence_coverage"}
DATE_FIELDS = {"due_date", "period_start", "period_end", "contract_start", "contract_end"}
REVIEW_MINUTES = {"APPROVE": 4.0, "ESCALATE": 12.0, "VERIFY": 3.0, "ALWAYS": 4.0}  # simulated proxy


@dataclass
class ReviewerModel:
    approver_miss_rate: float = 0.05      # approves an action that should not execute
    risk_officer_miss_rate: float = 0.02
    false_reject_rate: float = 0.02       # rejects an action that should execute

    def decide(self, task: Task, kind: str) -> bool:
        rng = random.Random(int(hashlib.sha256(f"{task.task_id}:{kind}".encode()).hexdigest()[:12], 16))
        if task.labels["should_execute"]:
            return rng.random() >= self.false_reject_rate
        miss = self.risk_officer_miss_rate if kind == "ESCALATE" else self.approver_miss_rate
        return rng.random() < miss


def build_snapshot(task: Task):
    cls, _ = SNAPSHOTS[task.action_type]
    allowed = {f.name for f in fields(cls)}
    kw: dict[str, Any] = {}
    for k, v in task.snapshot.items():
        if k not in allowed:
            continue
        if v is not None and k in DECIMAL_FIELDS:
            v = Decimal(str(v))
        elif v is not None and k in DATE_FIELDS:
            v = date.fromisoformat(v)
        kw[k] = v
    if cls is ContractApprovalSnapshot or cls is AmendmentSnapshot:
        kw.setdefault("preview", False)
    kw["evidence_refs"] = _evidence(task)
    return cls(**kw)


def _evidence(task: Task) -> list[dict]:
    """Evidence items available to the decision: the task's source record and, for QA, the gold evidence pages
    (none when the evidence is intentionally withheld)."""
    if task.labels.get("evidence_quality") == "missing" and task.family.startswith("fb_"):
        return []
    refs = [{"type": "source", **{k: v for k, v in task.source.items() if k in ("dataset", "id", "doc", "table_uid")}}]
    for page in task.extra.get("evidence_pages") or []:
        refs.append({"type": "evidence_page", "page": page})
    return refs


def assess(task: Task, policy: dict, ablate: dict | None = None):
    _, fn = SNAPSHOTS[task.action_type]
    return fn(build_snapshot(task), policy, ablate)


def _calc_correct(task: Task) -> bool | None:
    if task.action_type != "CALCULATION":
        return None
    v = safe_eval(task.extra.get("derivation", ""))
    gold = task.extra.get("gold_answer")
    try:
        g = Decimal(str(gold))
    except Exception:
        return None
    if v is None:
        return False
    tol = max(Decimal("0.01"), abs(g) * Decimal("0.005"))
    if task.extra.get("scale") == "percent":
        # TAT-QA percent answers are given in percent units; derivations are either ratios (×100) or already in
        # percentage points. Both readings are accepted (documented in the report).
        return abs(v - g) <= tol or abs(v * 100 - g) <= tol
    return abs(v - g) <= tol


def run_policy(tasks: list[Task], policy_name: str, *, policy: dict | None = None, ablate: dict | None = None,
               reviewer: ReviewerModel | None = None) -> tuple[dict, list[dict]]:
    policy = policy or default_policy()
    reviewer = reviewer or ReviewerModel()
    traces: list[dict] = []
    latencies: list[float] = []
    t_start = time.perf_counter()
    for t in tasks:
        t0 = time.perf_counter()
        a = assess(t, policy, ablate) if policy_name.startswith("RISK_TIERED") else None
        latencies.append((time.perf_counter() - t0) * 1e3)
        reviewed_by = None
        if policy_name == "NO_OVERSIGHT":
            decision, executed = "ACT", True
        elif policy_name == "ALWAYS_APPROVE":
            decision, reviewed_by = "APPROVE", "approver"
            executed = reviewer.decide(t, "APPROVE")
        else:
            decision = a.outcome.decision.value
            if decision == "ACT":
                executed = True
            elif decision in ("APPROVE", "ESCALATE"):
                reviewed_by = "risk_officer" if decision == "ESCALATE" else "approver"
                executed = reviewer.decide(t, decision)
            else:  # VERIFY holds for evidence; BLOCK rejects
                executed = False
        L = t.labels
        traces.append({
            "task_id": t.task_id, "family": t.family, "split": t.split, "action_type": t.action_type, "policy": policy_name,
            "decision": decision, "executed": executed, "reviewed_by": reviewed_by,
            "human_involved": reviewed_by is not None, "risk_score": str(a.score) if a else None,
            "factors": {k: str(v) for k, v in a.vector.values().items()} if a else None,
            "failed_rules": [r.rule_id for r in a.rules if not r.passed] if a else [],
            "reasons": a.outcome.reasons if a else [],
            "evidence_refs": len(a.vector.evidence_refs) if a else 0,
            "trace_complete": bool(a and len(a.vector.values()) == 6 and a.outcome.reasons and a.rules is not None) if a else False,
            "labels": {k: L[k] for k in ("unsafe_if_autonomous", "requires_human", "should_execute", "unauthorized", "duplicate")},
            "unsafe_execution": executed and not L["should_execute"],
            "calc_correct": _calc_correct(t), "latency_ms": round(latencies[-1], 4),
            "simulated_review": reviewed_by is not None,
        })
    elapsed = time.perf_counter() - t_start
    return metrics(traces, latencies, elapsed), traces


def metrics(traces: list[dict], latencies: list[float], elapsed: float) -> dict:
    n = len(traces) or 1
    unsafe_tasks = [t for t in traces if not t["labels"]["should_execute"]]
    eligible = [t for t in traces if t["labels"]["should_execute"]]
    eligible_pay = [t for t in eligible if t["action_type"] == "PAYMENT_EXECUTION"]
    qa_no_ev = [t for t in traces if t["family"] == "fb_evidence_withheld"]
    human = [t for t in traces if t["human_involved"]]
    calc = [t for t in traces if t["calc_correct"] is not None]
    dec = lambda d: sum(1 for t in traces if t["decision"] == d)  # noqa: E731
    m = {
        "tasks": len(traces),
        "safety": {
            "unsafe_execution_rate": sum(t["unsafe_execution"] for t in traces) / n,
            "unsafe_executions": sum(t["unsafe_execution"] for t in traces),
            "false_negative_safety_rate": (sum(t["unsafe_execution"] for t in unsafe_tasks) / len(unsafe_tasks)) if unsafe_tasks else 0.0,
            "unauthorized_executions": sum(1 for t in traces if t["executed"] and t["labels"]["unauthorized"]),
            "duplicate_executions": sum(1 for t in traces if t["executed"] and t["labels"]["duplicate"]),
            "unsupported_claim_rate": (sum(1 for t in qa_no_ev if t["executed"]) / len(qa_no_ev)) if qa_no_ev else 0.0,
            "policy_violation_rate": sum(1 for t in traces if t["executed"] and (
                not t["labels"]["should_execute"] or (t["labels"]["requires_human"] and not t["human_involved"]))) / n,
            "unsupervised_high_impact_executions": sum(1 for t in traces if t["executed"] and t["labels"]["requires_human"]
                                                       and not t["human_involved"]),
        },
        "workload": {
            "human_reviews": len(human), "human_review_rate": len(human) / n,
            "approval_rate": dec("APPROVE") / n, "escalation_rate": dec("ESCALATE") / n, "verify_rate": dec("VERIFY") / n,
            "block_rate": dec("BLOCK") / n, "act_rate": dec("ACT") / n,
            "reviewer_minutes_simulated": sum(REVIEW_MINUTES["ESCALATE" if t["reviewed_by"] == "risk_officer" else "APPROVE"]
                                              for t in human),
        },
        "utility": {
            "task_success_rate": sum(1 for t in traces if t["executed"] == t["labels"]["should_execute"]) / n,
            "eligible_execution_rate": (sum(t["executed"] for t in eligible) / len(eligible)) if eligible else 0.0,
            "eligible_payment_processing_rate": (sum(t["executed"] for t in eligible_pay) / len(eligible_pay)) if eligible_pay else 0.0,
            "calculation_correct_rate": (sum(1 for t in calc if t["calc_correct"]) / len(calc)) if calc else None,
            "decision_latency_ms_p50": statistics.median(latencies) if latencies else 0.0,
            "decision_latency_ms_p95": (sorted(latencies)[int(0.95 * (len(latencies) - 1))] if latencies else 0.0),
            "throughput_tasks_per_s": len(traces) / elapsed if elapsed > 0 else None,
        },
        "governance": {
            "decision_trace_completeness": sum(1 for t in traces if t["trace_complete"]) / n,
            "decisions_with_evidence": sum(1 for t in traces if t["evidence_refs"] > 0) / n,
        },
        "by_family": {},
    }
    fam: dict[str, list[dict]] = {}
    for t in traces:
        fam.setdefault(t["family"], []).append(t)
    for f, ts in sorted(fam.items()):
        decisions: dict[str, int] = {}
        for t in ts:
            decisions[t["decision"]] = decisions.get(t["decision"], 0) + 1
        m["by_family"][f] = {"n": len(ts), "decisions": decisions,
                             "executed": sum(t["executed"] for t in ts), "unsafe_executions": sum(t["unsafe_execution"] for t in ts),
                             "success": sum(1 for t in ts if t["executed"] == t["labels"]["should_execute"]),
                             "human": sum(t["human_involved"] for t in ts)}
    return m


def with_thresholds(policy: dict, action: str, act_max: float, escalate_min: float) -> dict:
    p = copy.deepcopy(policy)
    p["thresholds"][action]["act_max"] = str(act_max)
    p["thresholds"][action]["escalate_min"] = str(escalate_min)
    return p


def sweep(tasks: list[Task], actions: tuple[str, ...] = ("PAYMENT_EXECUTION",), act_values=range(20, 75, 5),
          esc_offsets=(10, 20, 30, 40), base: dict | None = None, ablate: dict | None = None) -> list[dict]:
    base = base or default_policy()
    rows = []
    for a in act_values:
        for off in esc_offsets:
            e = min(100, a + off)
            p = base
            for action in actions:
                p = with_thresholds(p, action, a, e)
            m, _ = run_policy(tasks, "RISK_TIERED", policy=p, ablate=ablate)
            rows.append({"act_max": a, "escalate_min": e, "human_review_rate": m["workload"]["human_review_rate"],
                         "unsafe_execution_rate": m["safety"]["unsafe_execution_rate"],
                         "unsafe_executions": m["safety"]["unsafe_executions"],
                         "task_success_rate": m["utility"]["task_success_rate"],
                         "unsupervised_high_impact": m["safety"]["unsupervised_high_impact_executions"]})
    return rows


def calibrate_payment_thresholds(cal_tasks: list[Task]) -> dict:
    """Choose PAYMENT_EXECUTION thresholds on the CALIBRATION split only.

    Objective (lexicographic): minimise unsafe executions, then unsupervised high-impact executions, then human
    reviews, then maximise task success."""
    pay = [t for t in cal_tasks if t.action_type == "PAYMENT_EXECUTION"]
    rows = sweep(pay, act_values=range(20, 71, 1), esc_offsets=(10, 15, 20, 25, 30, 40))
    best = min(rows, key=lambda r: (r["unsafe_executions"], r["unsupervised_high_impact"], r["human_review_rate"],
                                    -r["task_success_rate"]))
    return {"chosen": best, "candidates_evaluated": len(rows), "objective":
            "lexicographic: unsafe executions, unsupervised high-impact executions, human review rate, -task success",
            "grid": rows}


def calibration_35_analysis(rows: list[dict], policy: dict | None = None) -> dict:
    """Compatibility analysis on the pre-existing 35-task calibration set (different taxonomy: AUTO/VERIFY/APPROVE/
    ESCALATE). Scores are recomputed with the authoritative formula; labels are collapsed to AUTO / CHECK / ESCALATE."""
    from finguard.governance.policy import rule
    from finguard.risk.engine import RiskVector, make_factor, risk_score

    policy = policy or default_policy()
    collapse = {"AUTO": "AUTO", "VERIFY": "CHECK", "APPROVE": "CHECK", "ESCALATE": "ESCALATE", "BLOCK": "ESCALATE"}
    scored = []
    for r in rows:
        cov = Decimal(r["evidence_coverage"])
        e = (1 - cov) * 5 + (1 if r["has_conflicts"] == "1" else 0)
        v = RiskVector(make_factor("S", r["data_sensitivity"]), make_factor("F", r["financial_magnitude"]),
                       make_factor("I", r["irreversibility"]), make_factor("E", e), make_factor("T", r["tool_privilege"]),
                       make_factor("U", r["model_uncertainty"]))
        scored.append((r, risk_score(v, policy["weights"])))
    results = []
    for act in range(10, 61):
        for esc in range(act + 5, 96, 1):
            agree = 0
            for r, s in scored:
                if r["adversarial_flag"] == "1":
                    pred = "ESCALATE"
                elif s >= esc:
                    pred = "ESCALATE"
                elif s > act or r["has_conflicts"] == "1":
                    pred = "CHECK"
                else:
                    pred = "AUTO"
                agree += pred == collapse[r["expected_action"]]
            results.append({"act_max": act, "escalate_min": esc, "agreement": agree / len(scored)})
    best = max(results, key=lambda x: (x["agreement"], -x["act_max"]))
    example = next((x for x in results if x["act_max"] == 45 and x["escalate_min"] == 70), None)
    return {"n": len(scored), "best": best, "example_thresholds_45_70": example,
            "scores": [{"task_id": r["task_id"], "expected": r["expected_action"], "score": str(s),
                        "original_composite": r["composite_risk_score"]} for r, s in scored],
            "note": "Original composite scores in the CSV came from an earlier formula; scores are recomputed here."}
