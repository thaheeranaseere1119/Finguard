import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from "recharts";
import { Alert, Badge, Card, Empty, Loading, PageHeader, Table } from "../components/ui";
import { api } from "../lib/api";
import { dateTime, pct, titleCase } from "../lib/format";

const COLORS: Record<string, string> = { NO_OVERSIGHT: "#ef4444", ALWAYS_APPROVE: "#2563eb", RISK_TIERED_EXAMPLE: "#10b981", RISK_TIERED_CALIBRATED: "#7c3aed" };

export default function Research() {
  const runs = useQuery({ queryKey: ["evaluation", "runs"], queryFn: () => api.get("/evaluation/runs") });
  if (runs.isLoading) return <Loading />;
  const all: any[] = runs.data ?? [];
  if (!all.length) return (<><PageHeader title="Research evaluation" /><Empty title="No evaluation runs recorded">Run <code className="font-mono">python -m finguard.evaluation.run_all --record</code> in the backend.</Empty></>);
  const comp = all.filter((r) => r.kind === "POLICY_COMPARISON");
  const order = ["NO_OVERSIGHT", "ALWAYS_APPROVE", "RISK_TIERED_EXAMPLE", "RISK_TIERED_CALIBRATED"];
  comp.sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name));
  const abl = all.find((r) => r.kind === "ABLATION");
  const cal = all.find((r) => r.kind === "CALIBRATION");
  const ocr = all.find((r) => r.kind === "OCR");
  const bars = [
    { metric: "Unsafe execution", ...Object.fromEntries(comp.map((r) => [r.name, +(r.metrics.safety.unsafe_execution_rate * 100).toFixed(2)])) },
    { metric: "Human review", ...Object.fromEntries(comp.map((r) => [r.name, +(r.metrics.workload.human_review_rate * 100).toFixed(2)])) },
    { metric: "Task success", ...Object.fromEntries(comp.map((r) => [r.name, +(r.metrics.utility.task_success_rate * 100).toFixed(2)])) },
    { metric: "Policy violations", ...Object.fromEntries(comp.map((r) => [r.name, +(r.metrics.safety.policy_violation_rate * 100).toFixed(2)])) },
  ];
  const rows: [string, (m: any) => string][] = [
    ["Unsafe execution rate", (m) => pct(m.safety.unsafe_execution_rate)], ["False-negative safety rate", (m) => pct(m.safety.false_negative_safety_rate)],
    ["Unauthorized executions", (m) => String(m.safety.unauthorized_executions)], ["Duplicate executions", (m) => String(m.safety.duplicate_executions)],
    ["Unsupported-claim rate", (m) => pct(m.safety.unsupported_claim_rate)], ["Unsupervised high-impact executions", (m) => String(m.safety.unsupervised_high_impact_executions)],
    ["Human review rate", (m) => pct(m.workload.human_review_rate)], ["Escalation rate", (m) => pct(m.workload.escalation_rate)],
    ["Verify (evidence hold) rate", (m) => pct(m.workload.verify_rate)], ["Reviewer minutes (simulated)", (m) => m.workload.reviewer_minutes_simulated.toFixed(0)],
    ["Task success rate", (m) => pct(m.utility.task_success_rate)], ["Eligible payment processing", (m) => pct(m.utility.eligible_payment_processing_rate)],
    ["Calculation correct (TAT-QA)", (m) => pct(m.utility.calculation_correct_rate)], ["Decision trace completeness", (m) => pct(m.governance.decision_trace_completeness)],
  ];
  return (
    <>
      <PageHeader title="Research evaluation" subtitle={`Act vs Ask vs Escalate — dataset ${comp[0]?.dataset_version} · held-out test split · recorded ${dateTime(comp[0]?.created_at)}`} />
      <div className="mb-6"><Alert tone="warn" title="How to read these results">
        Human review is <b>simulated</b> (approver miss 5%, risk-officer miss 2%, false-reject 2%). Contract/payment scenarios are synthetic; FinanceBench & TAT-QA provide real
        questions/evidence but no incident labels — safety labels come from a written rubric, never from the risk score. Thresholds are prototype examples; the calibrated variant was tuned on the calibration split only.
        Full report: <code className="font-mono">evaluation/results/report.md</code>.</Alert></div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Policy comparison (%)">
          <div className="h-72"><ResponsiveContainer><BarChart data={bars}><CartesianGrid strokeDasharray="3 3" vertical={false} /><XAxis dataKey="metric" tick={{ fontSize: 11 }} /><YAxis />
            <Tooltip /><Legend />{comp.map((r) => <Bar key={r.name} dataKey={r.name} name={titleCase(r.name)} fill={COLORS[r.name]} />)}</BarChart></ResponsiveContainer></div>
        </Card>
        <Card title="Risk vs. workload">
          <div className="h-72"><ResponsiveContainer><ScatterChart><CartesianGrid strokeDasharray="3 3" />
            <XAxis type="number" dataKey="x" name="Human review %" domain={[0, 100]} label={{ value: "Human review %", position: "insideBottom", offset: -2, fontSize: 11 }} />
            <YAxis type="number" dataKey="y" name="Unsafe execution %" label={{ value: "Unsafe %", angle: -90, position: "insideLeft", fontSize: 11 }} /><ZAxis range={[160, 160]} /><Tooltip /><Legend />
            {comp.map((r) => <Scatter key={r.name} name={titleCase(r.name)} fill={COLORS[r.name]} data={[{ x: +(r.metrics.workload.human_review_rate * 100).toFixed(1), y: +(r.metrics.safety.unsafe_execution_rate * 100).toFixed(2) }]} />)}
          </ScatterChart></ResponsiveContainer></div>
        </Card>
      </div>
      <div className="mt-6"><Card title="Metrics (held-out test split)" padded={false}>
        <Table head={["Metric", ...comp.map((r) => titleCase(r.name))]}>
          {rows.map(([label, f]) => <tr key={label}><td className="td font-medium">{label}</td>{comp.map((r) => <td key={r.name} className="td tabular-nums">{f(r.metrics)}</td>)}</tr>)}
        </Table>
      </Card></div>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        {abl && <Card title="Ablations (risk-tiered)" padded={false}>
          <Table head={["Variant", "Unsafe", "Human review", "Success"]}>
            {Object.entries(abl.metrics).map(([k, m]: [string, any]) => <tr key={k}><td className="td text-sm">{k}</td><td className="td">{pct(m.safety.unsafe_execution_rate)}</td><td className="td">{pct(m.workload.human_review_rate)}</td><td className="td">{pct(m.utility.task_success_rate)}</td></tr>)}
          </Table>
        </Card>}
        {cal && <Card title="Threshold calibration">
          <p className="text-sm text-slate-600">{cal.metrics.objective}</p>
          <div className="mt-3 flex flex-wrap gap-2 text-sm">
            <Badge tone="violet">chosen act_max {cal.metrics.chosen.act_max}</Badge><Badge tone="violet">escalate_min {cal.metrics.chosen.escalate_min}</Badge>
            <Badge>calibration-split unsafe executions {cal.metrics.chosen.unsafe_executions}</Badge>
          </div>
          {cal.metrics.calibration_35 && <p className="mt-4 text-sm text-slate-600">Existing 35-task set (collapsed AUTO/CHECK/ESCALATE): best agreement {pct(cal.metrics.calibration_35.best.agreement)} at {cal.metrics.calibration_35.best.act_max}/{cal.metrics.calibration_35.best.escalate_min};
            example thresholds 45/70: {pct(cal.metrics.calibration_35.example?.agreement)}. Small sample — indicative only.</p>}
        </Card>}
      </div>
      {ocr && <div className="mt-6"><Card title="OCR / extraction accuracy (synthetic contracts)" padded={false}>
        <Table head={["Condition", "Amount", "Currency", "Dates", "Frequency", "Critical-field errors", "Mean OCR conf.", "s / doc"]}>
          {Object.entries(ocr.metrics.conditions).map(([k, r]: [string, any]) => (
            <tr key={k}><td className="td">{titleCase(k)}</td><td className="td">{pct(r.amount_accuracy)}</td><td className="td">{pct(r.currency_accuracy)}</td><td className="td">{pct(r.date_accuracy)}</td>
              <td className="td">{pct(r.frequency_accuracy)}</td><td className="td">{pct(r.critical_field_error_rate)}</td><td className="td">{r.mean_ocr_confidence?.toFixed(1) ?? "text layer"}</td><td className="td">{r.seconds_per_document.toFixed(2)}</td></tr>))}
        </Table>
      </Card></div>}
    </>
  );
}
