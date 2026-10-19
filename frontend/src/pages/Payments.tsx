import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Alert, Button, Card, Empty, Loading, PageHeader, Stat, StatusBadge, Table, Tabs } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, dateTime, money } from "../lib/format";

type Tab = "today" | "upcoming" | "attention" | "approval" | "paid" | "all";
const today = () => new Date().toISOString().slice(0, 10);
const plus = (d: number) => new Date(Date.now() + d * 86400000).toISOString().slice(0, 10);

const QUERIES: Record<Tab, string> = {
  today: `status=SCHEDULED,AWAITING_APPROVAL,HELD,ESCALATED,SUBMITTED,PAID,FAILED&due_after=${today()}&due_before=${today()}`,
  upcoming: `status=SCHEDULED&due_after=${today()}&due_before=${plus(45)}`,
  attention: "status=HELD,FAILED,UNKNOWN,BLOCKED,ESCALATED",
  approval: "status=AWAITING_APPROVAL,APPROVED_FOR_EXECUTION",
  paid: "status=PAID,SUBMITTED",
  all: "",
};

export default function Payments() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>(can("payments:read_org") ? "attention" : "upcoming");
  const q = useQuery({ queryKey: ["payments", tab], queryFn: () => api.get(`/payments?limit=300&${QUERIES[tab]}`) });
  const tick = useMutation({ mutationFn: () => api.post("/system/scheduler/tick"), onSuccess: () => qc.invalidateQueries() });
  return (
    <>
      <PageHeader title="Payments" subtitle="Scheduled occurrences are executed by the backend scheduler only after governance checks — never from the browser."
        actions={can("system:read") && <Button variant="secondary" loading={tick.isPending} onClick={() => tick.mutate()}>Run scheduler cycle (dev)</Button>} />
      {tick.isSuccess && <div className="mb-4"><Alert tone="success">Scheduler cycle: {JSON.stringify(tick.data)}</Alert></div>}
      <Tabs<Tab> value={tab} onChange={setTab} tabs={[
        { id: "attention", label: "Held / failed / escalated" }, { id: "approval", label: "Awaiting approval" }, { id: "today", label: "Due today" },
        { id: "upcoming", label: "Upcoming 45 days" }, { id: "paid", label: "Paid / submitted" }, { id: "all", label: "All" },
      ]} />
      <Card padded={false}>
        {q.isLoading ? <div className="p-5"><Loading /></div> : q.data?.items?.length ? (
          <Table head={["Due", "Contract", "Period", "Amount", "Status", "Attempts", ""]}>
            {q.data.items.map((p: any) => (
              <tr key={p.id} className="hover:bg-slate-50">
                <td className="td whitespace-nowrap">{date(p.due_date)}</td>
                <td className="td">{p.contract_title}<div className="text-xs text-slate-500">{p.contractor_name} · #{p.sequence_no}</div></td>
                <td className="td whitespace-nowrap text-xs">{date(p.period_start)} – {date(p.period_end)}</td>
                <td className="td whitespace-nowrap font-medium tabular-nums">{money(p.amount, p.currency)}</td>
                <td className="td"><StatusBadge status={p.status} />{p.hold_reason && !["SCHEDULED", "PAID"].includes(p.status) && <div className="mt-1 max-w-xs text-xs text-slate-500 line-clamp-2">{p.hold_reason}</div>}</td>
                <td className="td text-xs">{p.attempts.map((a: any) => a.status).join(" → ") || "—"}</td>
                <td className="td text-right"><Link className="text-sm font-medium text-brand-600 hover:underline" to={`/payments/${p.id}`}>Open</Link></td>
              </tr>
            ))}
          </Table>
        ) : <div className="p-5"><Empty title="No payments in this view" /></div>}
      </Card>
    </>
  );
}

export function Reconciliation() {
  const qc = useQueryClient();
  const r = useQuery({ queryKey: ["reconciliation"], queryFn: () => api.get("/reconciliation") });
  const rec = useMutation({ mutationFn: (spId: string) => api.post(`/payments/${spId}/reconcile`), onSuccess: () => qc.invalidateQueries({ queryKey: ["reconciliation"] }) });
  if (r.isLoading) return <Loading />;
  const s = r.data.summary as Record<string, number>;
  return (
    <>
      <PageHeader title="Reconciliation" subtitle="Provider attempts and their outcomes. Unknown outcomes must be reconciled with the provider before any retry." />
      <div className="mb-6 grid gap-4 sm:grid-cols-5">
        {["PROCESSED", "PENDING", "UNKNOWN", "FAILED", "REVERSED"].map((k) => <Stat key={k} label={k} value={s[k] ?? 0} tone={k === "UNKNOWN" && s[k] ? "warn" : k === "FAILED" && s[k] ? "bad" : "default"} />)}
      </div>
      {rec.isError && <div className="mb-4"><Alert tone="error">{errorMessage(rec.error)}</Alert></div>}
      <Card padded={false}>
        <Table head={["Created", "Idempotency key", "Provider", "Amount", "Status", "Provider ref", ""]}>
          {r.data.attempts.map((a: any) => (
            <tr key={a.id}>
              <td className="td whitespace-nowrap text-xs">{dateTime(a.created_at)}</td>
              <td className="td font-mono text-[11px]">{a.idempotency_key}</td>
              <td className="td text-xs">{a.provider} · {a.provider_mode}</td>
              <td className="td whitespace-nowrap tabular-nums">{money(a.amount, a.currency)}</td>
              <td className="td"><StatusBadge status={a.status} />{a.failure_reason && <div className="text-xs text-slate-500">{a.failure_reason}</div>}</td>
              <td className="td font-mono text-[11px]">{a.provider_payout_id ?? "—"}</td>
              <td className="td text-right">
                {["UNKNOWN", "PENDING"].includes(a.status) && <Button size="sm" loading={rec.isPending && rec.variables === a.scheduled_payment_id} onClick={() => rec.mutate(a.scheduled_payment_id)}>Reconcile</Button>}
                <Link className="ml-2 text-sm text-brand-600 hover:underline" to={`/payments/${a.scheduled_payment_id}`}>Open</Link>
              </td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
