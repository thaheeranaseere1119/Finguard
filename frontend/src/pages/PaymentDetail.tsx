import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import InvoiceClaim from "../components/InvoiceClaim";
import RiskPanel from "../components/RiskPanel";
import { Alert, Button, Card, DecisionBadge, Empty, Field, KV, Loading, PageHeader, StatusBadge, Table } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, dateTime, money } from "../lib/format";

export default function PaymentDetail() {
  const { id } = useParams();
  const { can, user } = useAuth();
  const qc = useQueryClient();
  const p = useQuery({ queryKey: ["payment", id], queryFn: () => api.get(`/payments/${id}`) });
  const contract = useQuery({ queryKey: ["contract", p.data?.contract_id], queryFn: () => api.get(`/contracts/${p.data.contract_id}`), enabled: !!p.data });
  const refresh = () => qc.invalidateQueries({ queryKey: ["payment", id] });
  const [inv, setInv] = useState({ invoice_number: "", amount: "", currency: "INR" });
  const submitInv = useMutation({ mutationFn: () => api.post(`/payments/${id}/invoices`, inv), onSuccess: refresh });
  const review = useMutation({ mutationFn: (x: { id: string; approve: boolean }) => api.post(`/invoices/${x.id}/review`, { approve: x.approve, note: x.approve ? "Invoice confirmed genuine by finance" : "Invoice rejected by finance" }), onSuccess: refresh });
  const retry = useMutation({ mutationFn: () => api.post(`/payments/${id}/retry`), onSuccess: refresh });
  const reconcile = useMutation({ mutationFn: () => api.post(`/payments/${id}/reconcile`), onSuccess: refresh });
  const draftQ = useQuery({ queryKey: ["invoice-draft", id], queryFn: () => api.get(`/payments/${id}/invoice-draft`), enabled: !!p.data && !!contract.data && contract.data.contractor?.id === user?.id });

  if (p.isLoading) return <Loading />;
  if (p.isError) return <Alert tone="error">{errorMessage(p.error)}</Alert>;
  const x = p.data;
  const isOwner = contract.data?.contractor?.id === user?.id;
  const error = [submitInv, review, retry, reconcile].find((m) => m.isError)?.error;
  return (
    <>
      <PageHeader title={<span className="flex items-center gap-3">Payment #{x.sequence_no} · {money(x.amount, x.currency)} <StatusBadge status={x.status} /></span>}
        subtitle={<><Link className="text-brand-600 hover:underline" to={`/contracts/${x.contract_id}`}>{x.contract_title}</Link> · due {date(x.due_date)}</>}
        actions={<>
          {can("payments:prepare") && ["FAILED", "HELD"].includes(x.status) && <Button loading={retry.isPending} onClick={() => retry.mutate()}>Re-run governance</Button>}
          {can("payments:reconcile") && ["UNKNOWN", "SUBMITTED"].includes(x.status) && <Button variant="secondary" loading={reconcile.isPending} onClick={() => reconcile.mutate()}>Reconcile with provider</Button>}
          {x.approval_request_id && can("approvals:read") && x.status === "AWAITING_APPROVAL" && <Link to={`/approvals/${x.approval_request_id}`}><Button>Open approval</Button></Link>}
        </>} />
      {error && <div className="mb-4"><Alert tone="error">{errorMessage(error)}</Alert></div>}
      {retry.isSuccess && <div className="mb-4"><Alert tone="info">Governance decision: {retry.data.decision}</Alert></div>}
      {x.hold_reason && !["PAID", "SCHEDULED"].includes(x.status) && <div className="mb-4"><Alert tone={x.status === "BLOCKED" || x.status === "FAILED" ? "error" : "warn"} title="Reason">{x.hold_reason}</Alert></div>}
      {x.status === "UNKNOWN" && <div className="mb-4"><Alert tone="warn" title="Outcome unknown">The provider did not confirm the outcome. No retry will happen until reconciliation establishes whether the payout exists.</Alert></div>}
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card title="Occurrence">
            <KV items={[["Service period", `${date(x.period_start)} – ${date(x.period_end)}`], ["Nominal date", date(x.nominal_date)],
              ["Due date (business-day adjusted)", date(x.due_date)], ["Execution time (UTC)", dateTime(x.due_at)],
              ["Amount (from approved version)", money(x.amount, x.currency)], ["Prorated", x.is_prorated ? "Yes" : "No"],
              ["Idempotency key", <span className="font-mono text-xs">{x.idempotency_key}</span>], ["Paid at", x.paid_at ? dateTime(x.paid_at) : "—"]]} />
          </Card>
          <Card title="Provider attempts" padded={false}>
            {x.attempts.length ? (
              <Table head={["#", "Status", "Provider", "Payout ID", "Failure", "Updated"]}>
                {x.attempts.map((a: any) => (
                  <tr key={a.id}><td className="td">{a.attempt_no}</td><td className="td"><StatusBadge status={a.status} /><div className="text-[11px] text-slate-500">{a.provider_status}</div></td>
                    <td className="td text-xs">{a.provider} · {a.provider_mode}</td><td className="td font-mono text-[11px]">{a.provider_payout_id ?? "—"}</td>
                    <td className="td text-xs">{a.failure_code ? `${a.failure_code}: ` : ""}{a.failure_reason ?? "—"}</td><td className="td text-xs">{dateTime(a.updated_at)}</td></tr>
                ))}
              </Table>
            ) : <div className="p-5"><Empty title="No payout attempts">Payouts are attempted only after governance returns ACT or an authorized approval.</Empty></div>}
          </Card>
          {x.provider_events?.length > 0 && (
            <Card title="Signed provider webhooks" padded={false}>
              <Table head={["Event", "Type", "Outcome", "Received"]}>
                {x.provider_events.map((e: any) => <tr key={e.event_id}><td className="td font-mono text-[11px]">{e.event_id}</td><td className="td text-xs">{e.event_type}</td><td className="td"><StatusBadge status={e.outcome} /></td><td className="td text-xs">{dateTime(e.received_at)}</td></tr>)}
              </Table>
            </Card>
          )}
          <Card title="Invoices">
            {x.invoices.length ? (
              <Table head={["Number", "Amount", "Status", ""]}>
                {x.invoices.map((i: any) => (
                  <tr key={i.id}><td className="td">{i.invoice_number}{i.document_id && <button className="ml-2 text-xs text-brand-600 hover:underline" onClick={() => api.download(`/documents/${i.document_id}/download`, `${i.invoice_number}.pdf`)}>PDF</button>}</td><td className="td">{money(i.amount, i.currency)}</td><td className="td"><StatusBadge status={i.status} /></td>
                    <td className="td text-right">{can("invoices:review") && !isOwner && <>
                      {["SUBMITTED", "FLAGGED"].includes(i.status) && <><Button size="sm" variant="success" onClick={() => review.mutate({ id: i.id, approve: true })}>Confirm genuine</Button>{" "}</>}
                      {(["SUBMITTED", "FLAGGED"].includes(i.status) || (i.status === "ACCEPTED" && !["PAID", "SUBMITTED"].includes(x.status))) &&
                        <Button size="sm" variant="danger" onClick={() => review.mutate({ id: i.id, approve: false })}>Reject</Button>}</>}
                      {i.review_note && <div className="mt-1 max-w-xs text-left text-[11px] text-slate-500">{i.review_note}</div>}</td></tr>
                ))}
              </Table>
            ) : <Empty title="No invoice submitted" />}
            {isOwner && draftQ.data && <div className="mt-4"><InvoiceClaim d={draftQ.data} onDone={refresh} /></div>}
            {isOwner && can("invoices:submit_own") && !["PAID", "SUPERSEDED", "CANCELLED"].includes(x.status) && !draftQ.data?.existing_invoice && (
              <details className="mt-4 text-sm"><summary className="cursor-pointer text-slate-600">Or enter an invoice you issued elsewhere</summary>
              <div className="mt-3 grid gap-3 sm:grid-cols-4">
                <Field label="Invoice number" htmlFor="in"><input id="in" className="input" value={inv.invoice_number} onChange={(e) => setInv({ ...inv, invoice_number: e.target.value })} /></Field>
                <Field label="Amount" htmlFor="ia"><input id="ia" className="input" value={inv.amount} placeholder={x.amount} onChange={(e) => setInv({ ...inv, amount: e.target.value })} /></Field>
                <Field label="Currency" htmlFor="ic"><input id="ic" className="input" value={inv.currency} onChange={(e) => setInv({ ...inv, currency: e.target.value })} /></Field>
                <div className="flex items-end"><Button loading={submitInv.isPending} disabled={!inv.invoice_number || !inv.amount} onClick={() => submitInv.mutate()}>Submit invoice</Button></div>
              </div></details>
            )}
          </Card>
        </div>
        <div className="space-y-6">
          <Card title="Governance decisions">
            {x.decisions.length ? (
              <div className="space-y-6">
                {x.decisions.slice(0, 1).map((d: any) => d.risk_factors ? <RiskPanel key={d.decision_id} d={d} /> : (
                  <div key={d.created_at}><DecisionBadge decision={d.decision} /><ul className="mt-2 list-disc pl-5 text-sm">{d.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul></div>
                ))}
                {x.decisions.length > 1 && (
                  <div><div className="label">Earlier decisions</div>
                    <ul className="space-y-1 text-xs text-slate-600">{x.decisions.slice(1).map((d: any, i: number) => <li key={i} className="flex gap-2"><DecisionBadge decision={d.decision} />{dateTime(d.created_at)}{d.risk_score ? ` · score ${Number(d.risk_score).toFixed(0)}` : ""}</li>)}</ul></div>
                )}
              </div>
            ) : <Empty title="Not evaluated yet">The scheduler evaluates the occurrence on its due date.</Empty>}
          </Card>
        </div>
      </div>
    </>
  );
}
