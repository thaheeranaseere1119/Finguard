import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import InvoiceClaim from "../components/InvoiceClaim";
import { Alert, Badge, Button, Card, DecisionBadge, Empty, Loading, PageHeader, Stat, StatusBadge, Table } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, money, relative, titleCase } from "../lib/format";

export const DECISION_COLORS: Record<string, string> = { ACT: "#10b981", APPROVE: "#3b82f6", VERIFY: "#f59e0b", ESCALATE: "#8b5cf6", BLOCK: "#ef4444" };

export default function Dashboard() {
  const { primaryRole, user } = useAuth();
  const greeting = <PageHeader title={`Welcome, ${user?.full_name.split(" ")[0]}`} subtitle={`${titleCase(primaryRole)} · ${user?.organization.name}`} />;
  switch (primaryRole) {
    case "CONTRACTOR": return <>{greeting}<ContractorHome /></>;
    case "FINANCE_ANALYST": return <>{greeting}<FinanceHome /></>;
    case "FINANCE_APPROVER": case "FINANCE_MANAGER": return <>{greeting}<ApproverHome /></>;
    case "RISK_OFFICER": return <>{greeting}<RiskHome /></>;
    case "AUDITOR": return <>{greeting}<AuditorHome /></>;
    case "SYSTEM_ADMIN": return <>{greeting}<AdminHome /></>;
    default: return greeting;
  }
}

function useList(path: string, key: string[]) {
  return useQuery({ queryKey: key, queryFn: () => api.get(path) });
}

function PaymentsTable({ items, showContract = true }: { items: any[]; showContract?: boolean }) {
  if (!items.length) return <Empty title="No payments" />;
  return (
    <Table head={["Due", ...(showContract ? ["Contract"] : []), "Amount", "Status", ""]}>
      {items.map((p) => (
        <tr key={p.id}>
          <td className="td whitespace-nowrap">{date(p.due_date)}</td>
          {showContract && <td className="td">{p.contract_title ?? "—"}<div className="text-xs text-slate-500">#{p.sequence_no} · {date(p.period_start)} – {date(p.period_end)}</div></td>}
          <td className="td whitespace-nowrap font-medium tabular-nums">{money(p.amount, p.currency)}</td>
          <td className="td"><StatusBadge status={p.status} />{p.hold_reason && p.status !== "SCHEDULED" && p.status !== "PAID" && <div className="mt-1 max-w-xs text-xs text-slate-500 line-clamp-2">{p.hold_reason}</div>}</td>
          <td className="td text-right"><Link className="text-sm font-medium text-brand-600 hover:underline" to={`/payments/${p.id}`}>View</Link></td>
        </tr>
      ))}
    </Table>
  );
}

function MonthlyTerms() {
  const t = useList("/my/monthly-terms", ["my", "monthly-terms"]);
  if (!t.data?.length) return null;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {t.data.map((c: any) => (
        <Card key={c.contract_id} title={<span className="flex items-center gap-2">{c.title} <Badge tone="green">{c.status.toLowerCase()}</Badge></span>}>
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-slate-500">You are paid every {c.frequency === "MONTHLY" ? "month" : c.frequency.toLowerCase()}</div>
              <div className="text-3xl font-semibold tabular-nums text-slate-900">{money(c.amount, c.currency)}</div>
              <div className="mt-1 text-xs text-slate-500">
                {c.due_day_of_month ? `due on day ${c.due_day_of_month} of the following month` : ""} · paid in {c.payment_timing.toLowerCase()} · {date(c.start_date)} – {date(c.end_date)} · version {c.version_no}
              </div>
            </div>
            {c.next_payment && (
              <div className="text-right">
                <div className="text-xs text-slate-500">Next payment</div>
                <div className="font-medium">{money(c.next_payment.amount, c.currency)} on {date(c.next_payment.due_date)}</div>
                <StatusBadge status={c.next_payment.status} />
              </div>
            )}
          </div>
          {c.invoice_required && <div className="mt-3 text-xs text-amber-800">This contract requires an invoice for each period before payment is released.</div>}
        </Card>
      ))}
    </div>
  );
}

function LastMonthInvoice() {
  const q = useList("/my/claimable", ["my", "claimable"]);
  if (!q.data) return null;
  return (
    <Card title={`Last month's invoice — ${q.data.month_label}`} actions={<span className="text-xs text-slate-500">Generated from your approved contract terms</span>}>
      {q.data.items.length ? (
        <div className="space-y-3">{q.data.items.map((d: any) => <InvoiceClaim key={d.scheduled_payment_id} d={d} />)}</div>
      ) : <Empty title={`No scheduled payment covers ${q.data.month_label}`} />}
    </Card>
  );
}

function ContractorHome() {
  const contracts = useList("/contracts?mine=true", ["contracts", "mine"]);
  const payments = useList("/payments?limit=200", ["payments", "mine"]);
  if (contracts.isLoading) return <Loading />;
  const cs = contracts.data?.items ?? [];
  const ps: any[] = payments.data?.items ?? [];
  const upcoming = ps.filter((p) => ["SCHEDULED", "AWAITING_APPROVAL", "HELD", "ESCALATED"].includes(p.status)).slice(0, 6);
  const paid = ps.filter((p) => p.status === "PAID");
  const attention = ps.filter((p) => p.status === "HELD" || p.status === "FAILED");
  const needsReview = cs.filter((c: any) => ["NEEDS_REVIEW", "VALIDATION_FAILED", "OCR_FAILED"].includes(c.status));
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Active contracts" value={cs.filter((c: any) => c.status === "ACTIVE").length} />
        <Stat label="Received to date" value={money(paid.reduce((s, p) => s + Number(p.amount), 0))} tone="good" />
        <Stat label="Next payment" value={upcoming[0] ? money(upcoming[0].amount, upcoming[0].currency) : "—"} hint={upcoming[0] ? `due ${date(upcoming[0].due_date)}` : undefined} />
        <Stat label="Needs your action" value={needsReview.length + attention.length} tone={needsReview.length + attention.length ? "warn" : "default"} />
      </div>
      <MonthlyTerms />
      <LastMonthInvoice />
      {needsReview.map((c: any) => (
        <Alert key={c.id} tone="warn" title={`“${c.title}” needs your review`}>
          Extracted fields must be checked {c.status === "VALIDATION_FAILED" ? "and corrected after validation feedback" : ""} before submission. <Link className="font-medium underline" to={`/contracts/${c.id}`}>Open review</Link>
        </Alert>
      ))}
      {attention.filter((p) => p.hold_reason?.toLowerCase().includes("invoice")).map((p) => (
        <Alert key={p.id} tone="warn" title={`Invoice needed for ${money(p.amount, p.currency)} due ${date(p.due_date)}`}>
          <Link className="font-medium underline" to={`/payments/${p.id}`}>Submit invoice</Link>
        </Alert>
      ))}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="My contracts" actions={<Link to="/contracts/new"><Button size="sm">Submit contract</Button></Link>} padded={false}>
          {cs.length ? (
            <Table head={["Contract", "Status", "Terms"]}>
              {cs.map((c: any) => (
                <tr key={c.id}>
                  <td className="td"><Link className="font-medium text-brand-700 hover:underline" to={`/contracts/${c.id}`}>{c.title}</Link><div className="text-xs text-slate-500">{c.external_ref}</div></td>
                  <td className="td"><StatusBadge status={c.status} /></td>
                  <td className="td text-xs">{c.current_version ? `${money(c.current_version.payment_amount, c.current_version.currency)} ${titleCase(c.current_version.frequency)}` : "—"}</td>
                </tr>
              ))}
            </Table>
          ) : <div className="p-5"><Empty title="No contracts yet">Upload a signed contract to get started.</Empty></div>}
        </Card>
        <Card title="Upcoming payments" padded={false}><PaymentsTable items={upcoming} /></Card>
      </div>
    </div>
  );
}

function FinanceHome() {
  const overview = useList("/analytics/overview", ["overview"]);
  const review = useList("/contracts?status=PENDING_VALIDATION", ["contracts", "pv"]);
  const exceptions = useList("/payments?status=HELD,FAILED,UNKNOWN,BLOCKED&limit=20", ["payments", "exceptions"]);
  const ocr = useList("/contracts?status=NEEDS_REVIEW,OCR_FAILED", ["contracts", "ocr"]);
  if (overview.isLoading) return <Loading />;
  const o = overview.data;
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label="Due today" value={o.due_today.count} hint={money(o.due_today.amount)} />
        <Stat label="Next 30 days" value={o.upcoming_30d.count} hint={money(o.upcoming_30d.amount)} />
        <Stat label="Paid to date" value={money(o.paid_total)} tone="good" />
        <Stat label="Awaiting validation" value={review.data?.total ?? "—"} tone={review.data?.total ? "warn" : "default"} />
        <Stat label="Payment exceptions" value={exceptions.data?.total ?? "—"} tone={exceptions.data?.total ? "bad" : "default"} />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Contract validation queue" padded={false}>
          {review.data?.items?.length ? (
            <Table head={["Contract", "Contractor", "Submitted", ""]}>
              {review.data.items.map((c: any) => (
                <tr key={c.id}>
                  <td className="td font-medium">{c.title}</td><td className="td">{c.contractor.name}</td><td className="td text-xs">{relative(c.submitted_at)}</td>
                  <td className="td text-right"><Link to={`/contracts/${c.id}`}><Button size="sm">Validate</Button></Link></td>
                </tr>
              ))}
            </Table>
          ) : <div className="p-5"><Empty title="Nothing to validate" /></div>}
        </Card>
        <Card title="Held, failed and unknown payments" padded={false}><PaymentsTable items={exceptions.data?.items ?? []} /></Card>
      </div>
      <Card title="OCR exceptions & contracts in contractor review" padded={false}>
        {ocr.data?.items?.length ? (
          <Table head={["Contract", "Contractor", "Status"]}>
            {ocr.data.items.map((c: any) => (
              <tr key={c.id}><td className="td"><Link className="text-brand-700 hover:underline" to={`/contracts/${c.id}`}>{c.title}</Link></td><td className="td">{c.contractor.name}</td><td className="td"><StatusBadge status={c.status} /></td></tr>
            ))}
          </Table>
        ) : <div className="p-5"><Empty title="No OCR exceptions" /></div>}
      </Card>
    </div>
  );
}

function QueueTable({ items }: { items: any[] }) {
  if (!items.length) return <div className="p-5"><Empty title="Queue is empty" /></div>;
  return (
    <Table head={["Request", "Amount", "Risk", "Required", ""]}>
      {items.map((a) => (
        <tr key={a.id}>
          <td className="td"><div className="font-medium">{a.title}</div><div className="text-xs text-slate-500">{titleCase(a.kind)} · {relative(a.created_at)}</div></td>
          <td className="td whitespace-nowrap tabular-nums">{a.amount ? money(a.amount, a.currency ?? "INR") : "—"}</td>
          <td className="td"><DecisionBadge decision={a.decision} /><div className="text-xs text-slate-500">{a.risk_score ? `score ${Number(a.risk_score).toFixed(0)}` : ""}</div></td>
          <td className="td text-xs">{titleCase(a.required_role)}{a.required_approvals > 1 ? ` ×${a.required_approvals}` : ""}</td>
          <td className="td text-right"><Link to={`/approvals/${a.id}`}><Button size="sm">Review</Button></Link></td>
        </tr>
      ))}
    </Table>
  );
}

function ApproverHome() {
  const q = useList("/approvals?status=OPEN&mine=true", ["approvals", "mine"]);
  const overview = useList("/analytics/overview", ["overview"]);
  if (q.isLoading) return <Loading />;
  const items: any[] = q.data?.items ?? [];
  const byKind = (k: string) => items.filter((a) => a.kind === k).length;
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Contracts to approve" value={byKind("CONTRACT_APPROVAL")} />
        <Stat label="Amendments" value={byKind("AMENDMENT_APPROVAL")} />
        <Stat label="Payments to approve" value={byKind("PAYMENT_APPROVAL")} tone={byKind("PAYMENT_APPROVAL") ? "warn" : "default"} />
        <Stat label="Upcoming 30 days" value={overview.data ? money(overview.data.upcoming_30d.amount) : "—"} />
      </div>
      <Card title="My approval queue" padded={false}><QueueTable items={items.filter((a) => a.kind !== "ESCALATION")} /></Card>
    </div>
  );
}

function RiskHome() {
  const esc = useList("/approvals?status=OPEN&kind=ESCALATION", ["approvals", "esc"]);
  const risk = useList("/analytics/risk", ["analytics", "risk"]);
  if (risk.isLoading) return <Loading />;
  const r = risk.data;
  const totals: Record<string, number> = {};
  r.by_action_decision.forEach((x: any) => (totals[x.decision] = (totals[x.decision] ?? 0) + x.count));
  const pie = Object.entries(totals).map(([name, value]) => ({ name, value }));
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Open escalations" value={esc.data?.total ?? 0} tone={esc.data?.total ? "warn" : "default"} />
        <Stat label="Decisions recorded" value={pie.reduce((s, x) => s + x.value, 0)} />
        <Stat label="Blocked" value={totals.BLOCK ?? 0} tone={totals.BLOCK ? "bad" : "default"} />
        <Stat label="Decisions with evidence" value={r.evidence_coverage.assessments ? `${Math.round((100 * r.evidence_coverage.with_evidence) / r.evidence_coverage.assessments)}%` : "—"} />
      </div>
      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Escalated cases" className="lg:col-span-2" padded={false}><QueueTable items={esc.data?.items ?? []} /></Card>
        <Card title="Decision distribution">
          <div className="h-56">
            <ResponsiveContainer><PieChart><Pie data={pie} dataKey="value" nameKey="name" innerRadius={45} outerRadius={80}>
              {pie.map((p) => <Cell key={p.name} fill={DECISION_COLORS[p.name] ?? "#94a3b8"} />)}</Pie><Tooltip /><Legend /></PieChart></ResponsiveContainer>
          </div>
        </Card>
      </div>
      <Card title="Hard-rule violations (most frequent)">
        <div className="h-64">
          <ResponsiveContainer><BarChart data={r.hard_rule_violations.slice(0, 10)} layout="vertical" margin={{ left: 140 }}>
            <CartesianGrid strokeDasharray="3 3" horizontal={false} /><XAxis type="number" allowDecimals={false} /><YAxis type="category" dataKey="rule_id" tick={{ fontSize: 11 }} width={140} />
            <Tooltip /><Bar dataKey="count" fill="#ef4444" radius={[0, 4, 4, 0]} /></BarChart></ResponsiveContainer>
        </div>
      </Card>
    </div>
  );
}

function AuditorHome() {
  const audit = useList("/audit?limit=12", ["audit", "recent"]);
  const verify = useList("/audit/verify", ["audit", "verify"]);
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-3">
        <Stat label="Audit events" value={audit.data?.total ?? "—"} />
        <Stat label="Hash chain" value={verify.data ? (verify.data.valid ? "Intact" : "BROKEN") : "…"} tone={verify.data?.valid ? "good" : "bad"} hint={verify.data ? `${verify.data.checked} entries verified` : undefined} />
        <Stat label="Access" value="Read-only" />
      </div>
      <Card title="Recent audit events" actions={<Link to="/audit"><Button size="sm" variant="secondary">Open explorer</Button></Link>} padded={false}>
        <Table head={["#", "Event", "Resource", "Outcome", "When"]}>
          {(audit.data?.items ?? []).map((e: any) => (
            <tr key={e.id}><td className="td font-mono text-xs">{e.seq}</td><td className="td">{e.event_type}</td><td className="td text-xs">{e.resource_type}</td><td className="td"><StatusBadge status={e.outcome} /></td><td className="td text-xs">{relative(e.created_at)}</td></tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}

function AdminHome() {
  const h = useList("/system/health", ["health", "system"]);
  if (h.isLoading) return <Loading />;
  const s = h.data;
  return (
    <div className="space-y-6">
      <Alert tone="info">System administrators manage users, configuration and integrations. They have no financial approval authority.</Alert>
      <div className="grid gap-4 sm:grid-cols-4">
        <Stat label="Database" value={s.database.ok ? "OK" : "Down"} tone={s.database.ok ? "good" : "bad"} hint={s.database.revision ? `migration ${s.database.revision}` : undefined} />
        <Stat label="Redis" value={s.redis.ok ? "OK" : "Down"} tone={s.redis.ok ? "good" : "warn"} />
        <Stat label="OCR" value={s.ocr.ok ? "OK" : "Unavailable"} tone={s.ocr.ok ? "good" : "bad"} hint={s.ocr.engine} />
        <Stat label="Payouts" value={`${s.payouts.provider} · ${s.payouts.mode}`} tone={s.payouts.live_money_enabled ? "bad" : "good"} />
      </div>
      <div className="flex gap-2"><Link to="/admin/users"><Button variant="secondary">Users & roles</Button></Link><Link to="/admin/system"><Button variant="secondary">System health</Button></Link></div>
    </div>
  );
}
