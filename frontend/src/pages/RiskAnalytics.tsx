import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Legend, PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from "recharts";
import RiskPanel from "../components/RiskPanel";
import { Card, DecisionBadge, Empty, Loading, Modal, PageHeader, Stat, Table } from "../components/ui";
import { api } from "../lib/api";
import { dateTime, titleCase } from "../lib/format";
import { DECISION_COLORS } from "./Dashboard";

export default function RiskAnalytics() {
  const r = useQuery({ queryKey: ["analytics", "risk"], queryFn: () => api.get("/analytics/risk") });
  const ocr = useQuery({ queryKey: ["analytics", "ocr"], queryFn: () => api.get("/analytics/ocr") });
  const wl = useQuery({ queryKey: ["analytics", "workload"], queryFn: () => api.get("/analytics/workload") });
  const decisions = useQuery({ queryKey: ["decisions", "recent"], queryFn: () => api.get("/decisions?limit=25") });
  const [open, setOpen] = useState<any>(null);
  if (r.isLoading) return <Loading />;
  const d = r.data;
  const decisionsList = ["ACT", "APPROVE", "VERIFY", "ESCALATE", "BLOCK"];
  const byAction: Record<string, any> = {};
  d.by_action_decision.forEach((x: any) => { byAction[x.action_type] ??= { action: titleCase(x.action_type) }; byAction[x.action_type][x.decision] = x.count; });
  const hist: Record<number, any> = {};
  d.score_histogram.forEach((x: any) => { hist[x.bucket] ??= { bucket: `${x.bucket}–${x.bucket + 9}` }; hist[x.bucket][x.decision] = x.count; });
  const radar = ["S", "F", "I", "E", "T", "U"].map((k) => { const row: any = { factor: k }; d.avg_factors.forEach((a: any) => (row[titleCase(a.action_type)] = Number(a[k].toFixed(2)))); return row; });
  const total = d.by_action_decision.reduce((s: number, x: any) => s + x.count, 0);
  const human = d.by_action_decision.filter((x: any) => ["APPROVE", "ESCALATE"].includes(x.decision)).reduce((s: number, x: any) => s + x.count, 0);
  const colors = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#db2777"];
  return (
    <>
      <PageHeader title="Risk analytics" subtitle="Live governance data from this organization's decisions (not simulated)." />
      <div className="mb-6 grid gap-4 sm:grid-cols-4">
        <Stat label="Governance decisions" value={total} />
        <Stat label="Routed to humans" value={total ? `${Math.round((100 * human) / total)}%` : "—"} />
        <Stat label="With evidence references" value={d.evidence_coverage.assessments ? `${Math.round((100 * d.evidence_coverage.with_evidence) / d.evidence_coverage.assessments)}%` : "—"} />
        <Stat label="Hard-rule violations" value={d.hard_rule_violations.reduce((s: number, x: any) => s + x.count, 0)} tone="bad" />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Decisions by action type">
          <div className="h-72"><ResponsiveContainer><BarChart data={Object.values(byAction)}><CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="action" tick={{ fontSize: 11 }} /><YAxis allowDecimals={false} /><Tooltip /><Legend />
            {decisionsList.map((k) => <Bar key={k} dataKey={k} stackId="a" fill={DECISION_COLORS[k]} />)}</BarChart></ResponsiveContainer></div>
        </Card>
        <Card title="Risk score distribution by decision">
          <div className="h-72"><ResponsiveContainer><BarChart data={Object.keys(hist).sort((a, b) => +a - +b).map((k) => hist[+k])}><CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="bucket" tick={{ fontSize: 11 }} /><YAxis allowDecimals={false} /><Tooltip /><Legend />
            {decisionsList.map((k) => <Bar key={k} dataKey={k} stackId="a" fill={DECISION_COLORS[k]} />)}</BarChart></ResponsiveContainer></div>
        </Card>
        <Card title="Average factor profile (0–5)">
          <div className="h-72"><ResponsiveContainer><RadarChart data={radar}><PolarGrid /><PolarAngleAxis dataKey="factor" /><PolarRadiusAxis domain={[0, 5]} tick={{ fontSize: 10 }} />
            {d.avg_factors.map((a: any, i: number) => <Radar key={a.action_type} name={titleCase(a.action_type)} dataKey={titleCase(a.action_type)} stroke={colors[i % 5]} fill={colors[i % 5]} fillOpacity={0.12} />)}
            <Legend /><Tooltip /></RadarChart></ResponsiveContainer></div>
        </Card>
        <Card title="Recent decisions: risk score over time">
          <div className="h-72"><ResponsiveContainer><ScatterChart><CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="t" type="number" domain={["auto", "auto"]} tickFormatter={(v) => new Date(v).toLocaleDateString()} tick={{ fontSize: 10 }} name="time" />
            <YAxis dataKey="score" domain={[0, 100]} name="score" /><ZAxis range={[40, 40]} /><Tooltip formatter={(v: any, n: any) => (n === "time" ? new Date(v).toLocaleString() : v)} /><Legend />
            {decisionsList.map((k) => <Scatter key={k} name={k} fill={DECISION_COLORS[k]} data={d.recent.filter((x: any) => x.decision === k).map((x: any) => ({ t: new Date(x.at).getTime(), score: x.score }))} />)}
          </ScatterChart></ResponsiveContainer></div>
        </Card>
        <Card title="OCR extraction quality (critical fields)" padded={false}>
          {ocr.data ? (
            <Table head={["Field", "Status", "Count"]}>
              {ocr.data.critical_field_status.map((x: any, i: number) => <tr key={i}><td className="td text-sm">{titleCase(x.field)}</td><td className="td text-xs">{x.status}</td><td className="td tabular-nums">{x.count}</td></tr>)}
            </Table>
          ) : <Loading />}
          {ocr.data && <div className="px-5 py-3 text-xs text-slate-500">{ocr.data.extractions} extractions · {ocr.data.jobs.map((j: any) => `${j.method ?? "?"}: ${j.count}${j.avg_ocr_confidence ? ` (mean OCR ${j.avg_ocr_confidence.toFixed(0)})` : ""}`).join(" · ")} · corrections {ocr.data.corrections.reduce((s: number, c: any) => s + c.count, 0)}</div>}
        </Card>
        <Card title="Human workload" padded={false}>
          {wl.data?.approvals?.length ? (
            <Table head={["Kind", "Status", "Count", "Avg. resolution"]}>
              {wl.data.approvals.map((x: any, i: number) => <tr key={i}><td className="td text-sm">{titleCase(x.kind)}</td><td className="td text-xs">{x.status}</td><td className="td">{x.count}</td>
                <td className="td text-xs">{x.avg_resolution_seconds ? `${(x.avg_resolution_seconds / 60).toFixed(1)} min` : "—"}</td></tr>)}
            </Table>
          ) : <div className="p-5"><Empty title="No approvals yet" /></div>}
        </Card>
      </div>
      <div className="mt-6">
        <Card title="Latest governance decisions" padded={false}>
          <Table head={["When", "Action", "Resource", "Score", "Decision", ""]}>
            {(decisions.data?.items ?? []).map((g: any) => (
              <tr key={g.decision_id}><td className="td whitespace-nowrap text-xs">{dateTime(g.created_at)}</td><td className="td text-xs">{titleCase(g.action_type)}</td>
                <td className="td text-xs">{g.resource_type === "scheduled_payment" ? <Link className="text-brand-600 hover:underline" to={`/payments/${g.resource_id}`}>payment</Link>
                  : g.resource_type === "contract" ? <Link className="text-brand-600 hover:underline" to={`/contracts/${g.resource_id}`}>contract</Link> : titleCase(g.resource_type)}</td>
                <td className="td tabular-nums">{Number(g.risk_score).toFixed(1)}</td><td className="td"><DecisionBadge decision={g.decision} /></td>
                <td className="td text-right"><button className="text-sm text-brand-600 hover:underline" onClick={() => setOpen(g)}>Explain</button></td></tr>
            ))}
          </Table>
        </Card>
      </div>
      <Modal open={!!open} onClose={() => setOpen(null)} title="Decision explanation" wide>{open && <RiskPanel d={open} />}</Modal>
    </>
  );
}
