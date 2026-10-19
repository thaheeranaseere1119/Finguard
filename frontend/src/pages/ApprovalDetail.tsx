import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import FieldReview from "../components/FieldReview";
import RiskPanel from "../components/RiskPanel";
import { Alert, Badge, Button, Card, Empty, Field, KV, Loading, PageHeader, StatusBadge, Table } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, dateTime, money, titleCase } from "../lib/format";

export default function ApprovalDetail() {
  const { id } = useParams();
  const { can, user } = useAuth();
  const qc = useQueryClient();
  const a = useQuery({ queryKey: ["approval", id], queryFn: () => api.get(`/approvals/${id}`) });
  const [reason, setReason] = useState("");
  const decide = useMutation({
    mutationFn: (decision: string) => api.post(`/approvals/${id}/decision`, { decision, reason }),
    onSuccess: () => { qc.invalidateQueries(); setReason(""); },
  });
  if (a.isLoading) return <Loading />;
  if (a.isError) return <Alert tone="error">{errorMessage(a.error)}</Alert>;
  const r = a.data;
  const isEsc = r.kind === "ESCALATION";
  const canAct = r.status === "OPEN" && (isEsc ? can("escalations:resolve") : can({ CONTRACT_APPROVAL: "contracts:approve", AMENDMENT_APPROVAL: "amendments:approve", PAYMENT_APPROVAL: "payments:approve" }[r.kind as string] ?? "none"));
  const alreadyDecided = r.decisions.some((d: any) => d.decided_by === user?.id);

  return (
    <>
      <PageHeader title={<span className="flex flex-wrap items-center gap-3">{r.title} <StatusBadge status={r.status} /></span>}
        subtitle={<>{titleCase(r.kind)} · requires {titleCase(r.required_role)}{r.required_approvals > 1 ? ` (${r.required_approvals} distinct approvers)` : ""} · opened {dateTime(r.created_at)}</>} />
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          {r.resource_type === "contract" && <ContractContext id={r.resource_id} />}
          {r.resource_type === "contract_amendment" && <AmendmentContext id={r.resource_id} />}
          {r.resource_type === "scheduled_payment" && <PaymentContext id={r.resource_id} />}
        </div>
        <div className="space-y-6">
          <Card title="Decision">
            {r.decisions.length > 0 && (
              <ul className="mb-4 space-y-2">{r.decisions.map((d: any, i: number) => (
                <li key={i} className="rounded-lg bg-slate-50 px-3 py-2 text-sm"><StatusBadge status={d.decision === "APPROVE" ? "APPROVED" : d.decision === "REJECT" ? "REJECTED" : d.decision} />
                  <span className="ml-2 text-xs text-slate-500">{titleCase(d.role)} · {dateTime(d.at)}</span><div className="mt-1">{d.reason}</div></li>))}</ul>
            )}
            {r.status !== "OPEN" ? <Alert tone="info">Resolved{r.resolution_reason ? `: ${r.resolution_reason}` : ""}</Alert>
              : !canAct ? <Alert tone="warn">You can view this request but do not hold the authority to decide it.</Alert>
                : alreadyDecided ? <Alert tone="info">You already recorded your decision. A different approver must complete it.</Alert> : (
                  <div className="space-y-3">
                    {isEsc && <Alert tone="info">Clearing an escalation does not approve the action: it opens the normal approval step, which a different person must decide.</Alert>}
                    <Field label="Reason (mandatory, permanently audited)" htmlFor="reason">
                      <textarea id="reason" className="input" rows={4} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Explain what you checked and why" />
                    </Field>
                    {decide.isError && <Alert tone="error">{errorMessage(decide.error)}</Alert>}
                    <div className="flex flex-wrap gap-2">
                      {isEsc ? <>
                        <Button variant="success" disabled={reason.trim().length < 5} loading={decide.isPending && decide.variables === "CLEAR"} onClick={() => decide.mutate("CLEAR")}>Clear for approval</Button>
                        <Button variant="secondary" disabled={reason.trim().length < 5} onClick={() => decide.mutate("HOLD")}>Keep on hold</Button>
                        <Button variant="danger" disabled={reason.trim().length < 5} onClick={() => decide.mutate("REJECT")}>Reject</Button>
                      </> : <>
                        <Button variant="success" disabled={reason.trim().length < 5} loading={decide.isPending && decide.variables === "APPROVE"} onClick={() => decide.mutate("APPROVE")}>Approve</Button>
                        <Button variant="danger" disabled={reason.trim().length < 5} loading={decide.isPending && decide.variables === "REJECT"} onClick={() => decide.mutate("REJECT")}>Reject</Button>
                      </>}
                    </div>
                    <p className="text-xs text-slate-500">The server re-checks your authority, approval limit, separation of duties and all hard rules before anything changes.</p>
                  </div>
                )}
          </Card>
          <Card title="Risk assessment">{r.governance ? <RiskPanel d={r.governance} /> : <Empty title="Risk details not available for your role" />}</Card>
        </div>
      </div>
    </>
  );
}

function ContractContext({ id }: { id: string }) {
  const c = useQuery({ queryKey: ["contract", id], queryFn: () => api.get(`/contracts/${id}`) });
  const ex = useQuery({ queryKey: ["extraction", id], queryFn: () => api.get(`/contracts/${id}/extraction`) });
  if (c.isLoading) return <Loading />;
  const k = c.data;
  const v = k.proposed_version ?? k.current_version;
  const doc = k.documents?.find((d: any) => d.id === k.primary_document_id);
  return (
    <>
      <Card title={<>Proposed terms · <Link className="text-brand-600 hover:underline" to={`/contracts/${k.id}`}>{k.title}</Link></>}>
        {v ? <KV items={[["Contractor (contract party)", v.contractor_legal_name], ["Registered contractor", `${k.contractor.name}${k.contractor.company ? ` (${k.contractor.company})` : ""}`],
          ["Payment", `${money(v.payment_amount, v.currency)} ${titleCase(v.frequency)}`], ["Due rule", v.due_day_of_month ? `Day ${v.due_day_of_month}` : "period-anchored"],
          ["Term", `${date(v.start_date)} – ${date(v.end_date)}`], ["Total value", v.total_value ? money(v.total_value, v.currency) : "not stated"],
          ["Invoice required", v.invoice_required ? "Yes" : "No"], ["Auto-execution requested", v.auto_execute_permitted ? "Yes" : "No"],
          ["Payout account", k.beneficiary ? `${k.beneficiary.account_masked} · ${k.beneficiary.verified ? "verified" : "UNVERIFIED"}` : "none"]]} /> : <Empty title="No proposed terms" />}
      </Card>
      {k.validations?.length > 0 && (
        <Card title="Validation result">
          {k.validations.slice(-1).map((val: any) => (
            <div key={val.id}><StatusBadge status={val.decision} /> <span className="text-sm">{val.reason}</span>
              <ul className="mt-2 space-y-1">{val.checks.filter((ch: any) => !ch.passed).map((ch: any, i: number) => <li key={i} className="text-xs text-amber-700">! {ch.message}</li>)}</ul></div>
          ))}
        </Card>
      )}
      {ex.data?.extraction?.injection_report?.detected && <Alert tone="error" title="Instruction-like content in the document">{ex.data.extraction.injection_report.snippets?.map((s: any) => `“${s.snippet}”`).join(" · ")}</Alert>}
      {doc && ex.data?.fields && <Card title="Original document and extracted evidence"><FieldReview contractId={k.id} documentId={doc.id} pageCount={doc.page_count} fields={ex.data.fields} editable={false} onlyCritical /></Card>}
    </>
  );
}

function AmendmentContext({ id }: { id: string }) {
  const a = useQuery({ queryKey: ["amendment", id], queryFn: () => api.get(`/amendments/${id}`) });
  if (a.isLoading) return <Loading />;
  const am = a.data;
  return (
    <>
      <Card title="Amendment comparison">
        <div className="mb-3 text-sm text-slate-600">{am.reason}</div>
        <Table head={["Field", `Current (v${am.base_version?.version_no})`, "Proposed", "Amendment document says"]}>
          {am.diff.map((d: any) => (
            <tr key={d.field}><td className="td">{titleCase(d.field)}</td><td className="td font-mono">{d.old}</td><td className="td font-mono font-semibold text-brand-700">{d.new}</td>
              <td className="td font-mono">{am.document_fields?.[d.field] ?? <span className="text-slate-400">—</span>}
                {am.document_fields?.[d.field] && (String(am.document_fields[d.field]) === String(d.new) || Number(am.document_fields[d.field]) === Number(d.new) ? <Badge tone="green">matches</Badge> : <Badge tone="red">differs</Badge>)}</td></tr>
          ))}
        </Table>
        <div className="mt-3 text-xs text-slate-500">Effective from {date(am.requested_effective_date)} (aligned to the next unpaid period). {am.document_id ? "Signed document attached and OCR-checked." : "No supporting document."}</div>
      </Card>
      <Card title="Affected schedule occurrences" padded={false}>
        <Table head={["Period start", "Current amount", "Status"]}>
          {am.affected_occurrences.map((o: any) => <tr key={o.id}><td className="td">{date(o.period_start)}</td><td className="td">{money(o.amount)}</td><td className="td"><StatusBadge status={o.status} /></td></tr>)}
        </Table>
        <div className="px-5 py-3 text-xs text-slate-500">Only not-yet-executed occurrences from the effective date are regenerated; paid history is never modified.</div>
      </Card>
    </>
  );
}

function PaymentContext({ id }: { id: string }) {
  const p = useQuery({ queryKey: ["payment", id], queryFn: () => api.get(`/payments/${id}`) });
  if (p.isLoading) return <Loading />;
  const x = p.data;
  return (
    <Card title={<>Payment · <Link className="text-brand-600 hover:underline" to={`/payments/${x.id}`}>#{x.sequence_no} {x.contract_title}</Link></>}>
      <KV items={[["Amount (derived from approved terms)", money(x.amount, x.currency)], ["Service period", `${date(x.period_start)} – ${date(x.period_end)}`],
        ["Due", date(x.due_date)], ["Contractor", x.contractor_name], ["Status", <StatusBadge status={x.status} />],
        ["Prior attempts", x.attempts.map((a: any) => a.status).join(", ") || "none"], ["Invoices", x.invoices.map((i: any) => `${i.invoice_number} (${i.status})`).join(", ") || "none"]]} />
    </Card>
  );
}
