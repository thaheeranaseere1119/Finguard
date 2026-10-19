# FinGuard AI — evaluation report

Generated 2026-10-17T10:24:37.822460+00:00 · dataset `finguard-derived-v1` (sha256 `0c297e4ac6e2e853…`, 1625 tasks; splits {'test': 727, 'calibration': 480, 'dev': 418})

**Labelling:** Human review is **simulated** (approver miss rate 5%, risk-officer miss rate 2%, false-reject rate 2%; common random numbers per task). Contract/payment scenarios are **synthetic**. FinanceBench and TAT-QA supply real questions/evidence but contain no payment-incident labels; all safety labels follow the rubric in `finguard/evaluation/dataset.py`, never the risk score. Thresholds marked *example* are prototype values; *calibrated* thresholds were chosen on the calibration split only.

## Policy comparison (held-out test split)

| Metric | No Oversight | Always Approve | Risk Tiered Example | Risk Tiered Calibrated |
|---|---|---|---|---|
| Unsafe execution rate | 57.2% | 3.2% | 0.4% | 0.4% |
| False-negative safety rate | 100.0% | 5.5% | 0.7% | 0.7% |
| Unauthorized executions | 82 | 4 | 0 | 0 |
| Duplicate executions | 13 | 0 | 0 | 0 |
| Unsupported-claim rate (QA, evidence withheld) | 100.0% | 3.6% | 0.0% | 0.0% |
| Policy violation rate | 65.3% | 3.2% | 0.4% | 0.4% |
| Unsupervised high-impact executions | 231 | 0 | 0 | 0 |
| Human review rate | 0.0% | 100.0% | 22.7% | 22.0% |
| Escalation rate | 0.0% | 0.0% | 11.6% | 12.2% |
| Verify (evidence hold) rate | 0.0% | 0.0% | 20.5% | 20.5% |
| Reviewer minutes (simulated proxy) | 0 | 2908 | 1332 | 1352 |
| Task success rate | 42.8% | 96.1% | 99.4% | 99.4% |
| Eligible payment processing rate | 100.0% | 100.0% | 100.0% | 100.0% |
| Calculation correct rate (TAT-QA, deterministic) | 100.0% | 100.0% | 100.0% | 100.0% |
| Decision latency p95 (ms) | 0.000 | 0.000 | 0.027 | 0.026 |
| Decision trace completeness | 0.0% | 0.0% | 100.0% | 100.0% |
| Decisions with evidence references | 0.0% | 0.0% | 88.6% | 88.6% |

Baselines have no risk engine, so trace completeness/evidence references are 0 by construction.

## Calibration (calibration split only)

Objective: lexicographic: unsafe executions, unsupervised high-impact executions, human review rate, -task success. Evaluated 306 threshold pairs for PAYMENT_EXECUTION. Chosen: act_max=49, escalate_min=59 (calibration split: unsafe executions=1, human review rate=38.1%). Example thresholds: act_max=45, escalate_min=70.

## Ablations (held-out test split, risk-tiered with example thresholds)

| Variant | Unsafe exec. rate | Human review rate | Task success | Unsupervised high-impact |
|---|---|---|---|---|
| full (example thresholds) | 0.4% | 22.7% | 99.4% | 0 |
| without evidence factor (E=0) | 0.8% | 21.3% | 99.0% | 0 |
| without uncertainty factor (U=0) | 0.4% | 22.6% | 99.4% | 0 |
| without hard rules (score only) | 23.1% | 46.5% | 76.8% | 13 |
| without evidence & uncertainty | 0.8% | 20.1% | 99.0% | 0 |

## Reviewer-error sensitivity (simulated)

| Approver miss rate | Policy | Unsafe exec. rate |
|---|---|---|
| 0.0 | ALWAYS_APPROVE | 0.0% |
| 0.0 | RISK_TIERED | 0.0% |
| 0.05 | ALWAYS_APPROVE | 3.2% |
| 0.05 | RISK_TIERED | 0.4% |
| 0.15 | ALWAYS_APPROVE | 8.8% |
| 0.15 | RISK_TIERED | 1.2% |
| 0.3 | ALWAYS_APPROVE | 15.4% |
| 0.3 | RISK_TIERED | 2.1% |

## Existing 35-task calibration set (compatibility analysis)

Labels collapsed to AUTO / CHECK (VERIFY or APPROVE) / ESCALATE; scores recomputed with Risk = 3S+5F+3I+3E+3T+3U. Best agreement 85.7% at act_max=26, escalate_min=72; example thresholds (45/70): 65.7%. n=35 is small; treat as indicative only.

## OCR / extraction accuracy (synthetic contracts)

| Condition | Amount | Currency | Dates | Frequency | Critical-field error rate | Wrong critical values flagged | Mean OCR conf. | s/doc |
|---|---|---|---|---|---|---|---|---|
| native_pdf | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | n/a | n/a (text layer) | 0.01 |
| scanned_light | 100.0% | 100.0% | 100.0% | 100.0% | 1.7% | 0.0% | 94.8 | 0.70 |
| scanned_heavy | 100.0% | 100.0% | 100.0% | 100.0% | 2.5% | 100.0% | 93.4 | 1.90 |
| png_image | 100.0% | 100.0% | 100.0% | 100.0% | 1.7% | 0.0% | 92.4 | 0.55 |
| docx | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% | n/a | n/a (text layer) | 0.02 |

## Development note (transparency)

The first full run showed risk-tiered delegation answering all FinanceBench questions whose evidence was withheld (unsupported claims): for read-only queries the weighted score stays below the ACT threshold even with E=5. The same failure was reproduced on the dev split (40/40). The fix implements the specification's "missing mandatory evidence" deterministic override for factual agent answers (rule AG_EVIDENCE_PRESENT → VERIFY). No thresholds were changed using test-split results. The "without hard rules" ablation shows the score-only behaviour.

## Limitations

* Reviewer behaviour is simulated; real reviewer accuracy, fatigue and latency were not measured.
* Synthetic scenario families encode the rubric's notion of safety; results show consistency of the policy with that rubric, not real-world loss rates.
* No LLM was called in this run (mock provider); FinanceBench answer correctness is therefore not measured. TAT-QA correctness measures the deterministic calculator on gold derivations (percent-scale answers accepted in ratio×100 or percentage-point form).
* OCR metrics use generated documents with one font family; real scans vary more. Some name misreads (e.g. 'Iyer'→'lyer') occur at high OCR confidence and are not caught by confidence thresholds; the validation step's party-identity check against the registered contractor surfaces them as warnings.
* Circularity: most synthetic unsafe families correspond to conditions that the platform's hard rules check by design. The safety advantage over ALWAYS_APPROVE therefore mainly reflects deterministic rule coverage versus simulated reviewer error; it is not evidence of real-world performance. The ablation without hard rules isolates the score-based component.
* Baselines are evaluated on the same rubric; ALWAYS_APPROVE's results depend entirely on the reviewer model.
