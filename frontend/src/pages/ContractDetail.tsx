import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import FieldReview from "../components/FieldReview";
import RiskPanel from "../components/RiskPanel";
import { Alert, Badge, Button, Card, Empty, Field, KV, Loading, Modal, PageHeader, StatusBadge, Table, Tabs } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, dateTime, money, titleCase } from "../lib/format";

const LIFECYCLE = ["UPLOADED", "OCR_PROCESSING", "NEEDS_REVIEW", "PENDING_VALIDATION", "PENDING_APPROVAL", "APPROVED", "ACTIVE"];
type Tab = "review" | "terms" | "schedule" | "amendments" | "governance" | "activity";

export default function ContractDetail() {
  const { id } = useParams();
  const { can, user } = useAuth();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("review");
  const [modal, setModal] = useState<null | "validate" | "beneficiary" | "amend" | "hold" | "verifyBen">(null);
  const c = useQuery({ queryKey: ["contract", id], queryFn: () => api.get(`/contracts/${id}`),
    refetchInterval: (q) => (["UPLOADED", "OCR_PROCESSING"].includes(q.state.data?.status) ? 2000 : false) });
  const ex = useQuery({ queryKey: ["extraction", id], queryFn: () => api.get(`/contracts/${id}/extraction`), enabled: !!c.data });
  const refresh = () => {
    ["contract", "extraction", "schedule", "amendments", "versions"].forEach((k) => qc.invalidateQueries({ queryKey: [k, id] }));
    qc.invalidateQueries({ queryKey: ["contracts"] });
  };
  const submit = useMutation({ mutationFn: () => api.post(`/contracts/${id}/submit`), onSuccess: refresh });
  const retry = useMutation({ mutationFn: () => api.post(`/contracts/${id}/retry-ocr`), onSuccess: refresh });

  if (c.isLoading) return <Loading />;
  if (c.isError) return <Alert tone="error">{errorMessage(c.error)}</Alert>;
  const k = c.data;
  const isOwner = k.contractor.id === user?.id;
  const doc = k.documents?.find((d: any) => d.id === k.primary_document_id);
  const editable = (isOwner && ["NEEDS_REVIEW", "VALIDATION_FAILED"].includes(k.status)) || (!isOwner && can("contracts:correct_fields") && k.status === "PENDING_VALIDATION");
  const unresolved: string[] = ex.data?.unresolved_critical ?? [];
  const stepIdx = LIFECYCLE.indexOf(k.status);

  return (
    <>
      <PageHeader
        title={<span className="flex flex-wrap items-center gap-3">{k.title} <StatusBadge status={k.status} /></span>}
        subtitle={<>{k.contractor.name}{k.contractor.company ? ` · ${k.contractor.company}` : ""} · {k.external_ref ?? "no reference"} · created {date(k.created_at)}</>}
        actions={<>
          {isOwner && ["NEEDS_REVIEW", "VALIDATION_FAILED"].includes(k.status) && (
            <Button onClick={() => submit.mutate()} loading={submit.isPending} disabled={unresolved.length > 0}>Submit for validation</Button>)}
          {isOwner && k.status === "OCR_FAILED" && <Button onClick={() => retry.mutate()} loading={retry.isPending}>Retry OCR</Button>}
          {isOwner && !k.beneficiary && !["REJECTED", "TERMINATED", "EXPIRED"].includes(k.status) && <Button variant="secondary" onClick={() => setModal("beneficiary")}>Add payout account</Button>}
          {isOwner && k.status === "ACTIVE" && <Button variant="secondary" onClick={() => setModal("amend")}>Request amendment</Button>}
          {!isOwner && can("contracts:validate") && k.status === "PENDING_VALIDATION" && <Button onClick={() => setModal("validate")}>Validate</Button>}
          {!isOwner && can("beneficiaries:verify") && k.beneficiary && !k.beneficiary.verified && k.beneficiary.is_active && <Button variant="secondary" onClick={() => setModal("verifyBen")}>Verify payout account</Button>}
          {can("contracts:suspend") && ["ACTIVE", "SUSPENDED"].includes(k.status) && <Button variant="danger" onClick={() => setModal("hold")}>{k.status === "ACTIVE" ? "Suspend / terminate" : "Reactivate / terminate"}</Button>}
        </>}
      />
      {submit.isError && <div className="mb-4"><Alert tone="error">{errorMessage(submit.error)}</Alert></div>}
      <div className="mb-5 flex flex-wrap items-center gap-1.5 text-[11px]" aria-label="Lifecycle">
        {LIFECYCLE.map((s, i) => (
          <span key={s} className={`rounded px-2 py-1 font-medium ${stepIdx >= 0 && i <= stepIdx ? "bg-emerald-100 text-emerald-800" : "bg-slate-100 text-slate-500"}`}>{titleCase(s)}</span>
        ))}
        {stepIdx < 0 && <Badge tone="red">{titleCase(k.status)}</Badge>}
      </div>
      {k.status_reason && !["ACTIVE"].includes(k.status) && <div className="mb-4"><Alert tone={["REJECTED", "VALIDATION_FAILED", "OCR_FAILED"].includes(k.status) ? "error" : "info"} title="Latest decision note">{k.status_reason}</Alert></div>}
      {k.open_requests?.length > 0 && (
        <div className="mb-4"><Alert tone="info" title="Open governance requests">
          {k.open_requests.map((r: any) => <div key={r.id}>{titleCase(r.kind)} — requires {titleCase(r.required_role)} {can("approvals:read") && <Link className="ml-1 underline" to={`/approvals/${r.id}`}>open</Link>}</div>)}
        </Alert></div>
      )}
      <Tabs<Tab> value={tab} onChange={setTab} tabs={[
        { id: "review", label: `Extraction review${unresolved.length ? ` (${unresolved.length} unresolved)` : ""}` },
        { id: "terms", label: "Terms & versions" }, { id: "schedule", label: "Payment schedule" },
        { id: "amendments", label: "Amendments" }, { id: "governance", label: "Validation & approvals" },
        ...(can("audit:read") ? [{ id: "activity" as Tab, label: "Audit trail" }] : []),
      ]} />
      {tab === "review" && (
        ex.isLoading ? <Loading /> : !ex.data?.extraction ? <Empty title="No extraction yet">OCR is queued or running.</Empty> : (
          <div className="space-y-4">
            {unresolved.length > 0 && <Alert tone="warn" title="Critical fields need attention">{unresolved.map(titleCase).join(", ")} — correct or confirm them with a reason before submission.</Alert>}
            {ex.data.extraction.injection_report?.detected && (
              <Alert tone="error" title="Instruction-like content detected in the document">
                The document contains text addressed to an AI system. It was treated as data and ignored; compliance review will be required.
                <div className="mt-1 text-xs">{ex.data.extraction.injection_report.snippets?.map((s: any) => `“${s.snippet}”`).join(" · ")}</div>
              </Alert>
            )}
            {ex.data.extraction.quality_flags?.length > 0 && <Alert tone="warn">{ex.data.extraction.quality_flags.join(" · ")}</Alert>}
            <div className="text-xs text-slate-500">Extractor {ex.data.extraction.extractor} · prompt {ex.data.extraction.prompt_version} · {dateTime(ex.data.extraction.created_at)}</div>
            {doc && <FieldReview contractId={k.id} documentId={doc.id} pageCount={doc.page_count} fields={ex.data.fields} editable={editable} onlyCritical />}
          </div>
        )
      )}
      {tab === "terms" && <Terms k={k} />}
      {tab === "schedule" && <Schedule id={k.id} />}
      {tab === "amendments" && <Amendments id={k.id} />}
      {tab === "governance" && <Governance k={k} />}
      {tab === "activity" && <Activity id={k.id} />}

      <ValidateModal open={modal === "validate"} onClose={() => setModal(null)} id={k.id} onDone={refresh} />
      <BeneficiaryModal open={modal === "beneficiary"} onClose={() => setModal(null)} id={k.id} onDone={refresh} />
      <AmendModal open={modal === "amend"} onClose={() => setModal(null)} k={k} onDone={refresh} />
      <HoldModal open={modal === "hold"} onClose={() => setModal(null)} k={k} onDone={refresh} />
      <VerifyBenModal open={modal === "verifyBen"} onClose={() => setModal(null)} k={k} onDone={refresh} />
    </>
  );
}

function VersionKV({ v }: { v: any }) {
  return <KV items={[
    ["Payment amount", money(v.payment_amount, v.currency)], ["Frequency", `${titleCase(v.frequency)}${v.interval_count > 1 ? ` × ${v.interval_count} ${v.custom_unit ?? ""}` : ""}`],
    ["Due rule", v.due_day_of_month ? `Day ${v.due_day_of_month} of the month` : v.due_weekday !== null ? `Weekday ${v.due_weekday}` : "Period-anchored"],
    ["Timing", titleCase(v.payment_timing)], ["Start – end", `${date(v.start_date)} – ${date(v.end_date)}`],
    ["Total contract value", v.total_value ? money(v.total_value, v.currency) : "not stated"], ["Effective from", v.effective_from ? date(v.effective_from) : "contract start"],
    ["Business-day rule", titleCase(v.business_day_rule)], ["Grace period", `${v.grace_period_days} days`], ["Proration", titleCase(v.proration_rule)],
    ["Invoice required", v.invoice_required ? "Yes" : "No"], ["Automatic execution permitted", v.auto_execute_permitted ? "Yes (by contract)" : "No"],
    ["Notice period", v.notice_period_days ? `${v.notice_period_days} days` : "—"], ["Tax / withholding", v.tax_withholding_note ?? "—"],
    ["Terms hash", <span className="font-mono text-xs">{v.terms_hash?.slice(0, 16)}…</span>], ["Approved", v.approved_at ? dateTime(v.approved_at) : "—"],
  ]} />;
}

function Terms({ k }: { k: any }) {
  const versions = useQuery({ queryKey: ["versions", k.id], queryFn: () => api.get(`/contracts/${k.id}/versions`) });
  return (
    <div className="space-y-6">
      {k.current_version ? <Card title={`Approved terms — version ${k.current_version.version_no}`}><VersionKV v={k.current_version} /></Card>
        : k.proposed_version ? <Card title="Proposed terms (not yet approved)"><VersionKV v={k.proposed_version} /></Card>
          : <Empty title="Terms are created when the contract is submitted" />}
      {k.beneficiary && <Card title="Payout account"><KV items={[["Holder", k.beneficiary.account_holder_name], ["Account", k.beneficiary.account_masked],
        ["IFSC / routing", k.beneficiary.ifsc], ["Provider", k.beneficiary.provider], ["Verified", k.beneficiary.verified ? `Yes · ${dateTime(k.beneficiary.verified_at)}` : "Pending verification"]]} /></Card>}
      <Card title="Version history (immutable once approved)" padded={false}>
        <Table head={["Version", "Status", "Amount", "Frequency", "Effective", "Created"]}>
          {(versions.data ?? []).map((v: any) => (
            <tr key={v.id}><td className="td">{v.version_no ?? "—"}</td><td className="td"><StatusBadge status={v.status} /></td><td className="td">{money(v.payment_amount, v.currency)}</td>
              <td className="td">{titleCase(v.frequency)}</td><td className="td">{v.effective_from ? date(v.effective_from) : date(v.start_date)}</td><td className="td text-xs">{dateTime(v.created_at)}</td></tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}

function Schedule({ id }: { id: string }) {
  const [all, setAll] = useState(false);
  const s = useQuery({ queryKey: ["schedule", id, all], queryFn: () => api.get(`/contracts/${id}/schedule?include_superseded=${all}`) });
  if (s.isLoading) return <Loading />;
  const rows: any[] = s.data ?? [];
  return (
    <Card title="Recurring payment schedule" actions={<label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> show superseded</label>} padded={false}>
      {rows.length ? (
        <Table head={["#", "Service period", "Nominal → due", "Amount", "Status", ""]}>
          {rows.map((p) => (
            <tr key={p.id} className={p.status === "SUPERSEDED" ? "opacity-50" : ""}>
              <td className="td tabular-nums">{p.sequence_no}</td>
              <td className="td whitespace-nowrap text-xs">{date(p.period_start)} – {date(p.period_end)}</td>
              <td className="td whitespace-nowrap text-xs">{date(p.nominal_date)}{p.nominal_date !== p.due_date && <> → <span className="font-medium">{date(p.due_date)}</span> <Badge tone="amber">business day</Badge></>}</td>
              <td className="td whitespace-nowrap tabular-nums">{money(p.amount, p.currency)}{p.is_prorated && <Badge tone="blue">prorated</Badge>}</td>
              <td className="td"><StatusBadge status={p.status} /></td>
              <td className="td text-right"><Link className="text-sm text-brand-600 hover:underline" to={`/payments/${p.id}`}>Details</Link></td>
            </tr>
          ))}
        </Table>
      ) : <div className="p-5"><Empty title="No schedule yet">A schedule is generated deterministically when the contract becomes active.</Empty></div>}
    </Card>
  );
}

function Amendments({ id }: { id: string }) {
  const a = useQuery({ queryKey: ["amendments", id], queryFn: () => api.get(`/contracts/${id}/amendments`) });
  if (a.isLoading) return <Loading />;
  if (!a.data?.length) return <Empty title="No amendments" />;
  return (
    <div className="space-y-4">
      {a.data.map((am: any) => (
        <Card key={am.id} title={<span className="flex items-center gap-2">Amendment from {date(am.requested_effective_date)} <StatusBadge status={am.status} /></span>}>
          <div className="text-sm text-slate-600">{am.reason}</div>
          <Table head={["Field", "Current", "Proposed"]}>
            {am.diff.map((d: any) => <tr key={d.field}><td className="td">{titleCase(d.field)}</td><td className="td font-mono">{d.old}</td><td className="td font-mono font-semibold text-brand-700">{d.new}</td></tr>)}
          </Table>
          {am.decision_reason && <div className="mt-3 text-sm"><span className="font-medium">Decision:</span> {am.decision_reason}</div>}
        </Card>
      ))}
    </div>
  );
}

function Governance({ k }: { k: any }) {
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="space-y-6">
        <Card title="Validation">
          {k.validations.length ? k.validations.map((v: any) => (
            <div key={v.id} className="mb-4 border-b border-slate-100 pb-4 last:mb-0 last:border-0 last:pb-0">
              <div className="flex items-center gap-2"><StatusBadge status={v.decision} /><span className="text-xs text-slate-500">{dateTime(v.created_at)}</span></div>
              <div className="mt-1 text-sm">{v.reason}</div>
              <ul className="mt-2 space-y-1">{v.checks.map((ch: any, i: number) => (
                <li key={i} className="flex items-start gap-2 text-xs"><span className={ch.passed ? "text-emerald-600" : ch.severity === "error" ? "text-rose-600" : "text-amber-600"}>{ch.passed ? "✓" : ch.severity === "error" ? "✗" : "!"}</span>{ch.message}</li>
              ))}</ul>
            </div>
          )) : <Empty title="Not validated yet" />}
        </Card>
        <Card title="Approval decisions">
          {k.approvals.length ? k.approvals.map((a: any) => (
            <div key={a.id} className="mb-3 text-sm"><StatusBadge status={a.decision} /> <span className="text-xs text-slate-500">{dateTime(a.created_at)}{a.amendment_id ? " · amendment" : ""}</span><div className="mt-1">{a.reason}</div></div>
          )) : <Empty title="No approval decision yet" />}
        </Card>
      </div>
      <Card title="Latest risk assessment">{k.latest_decision ? <RiskPanel d={k.latest_decision} /> : <Empty title="Not available">Risk details are visible to finance and risk roles.</Empty>}</Card>
    </div>
  );
}

function Activity({ id }: { id: string }) {
  const a = useQuery({ queryKey: ["audit", "contract", id], queryFn: () => api.get(`/audit?contract_id=${id}&limit=200`) });
  if (a.isLoading) return <Loading />;
  return (
    <Card padded={false}>
      <Table head={["#", "When", "Event", "State", "Actor", "Details"]}>
        {(a.data?.items ?? []).map((e: any) => (
          <tr key={e.id}><td className="td font-mono text-xs">{e.seq}</td><td className="td whitespace-nowrap text-xs">{dateTime(e.created_at)}</td><td className="td text-sm">{e.event_type}</td>
            <td className="td text-xs">{e.previous_state ? `${e.previous_state} → ` : ""}{e.new_state ?? ""}</td><td className="td text-xs">{e.actor_roles?.join(", ") || e.actor_kind || "system"}</td>
            <td className="td max-w-sm text-xs text-slate-500"><span className="line-clamp-2">{JSON.stringify(e.payload)}</span></td></tr>
        ))}
      </Table>
    </Card>
  );
}

function ValidateModal({ open, onClose, id, onDone }: { open: boolean; onClose: () => void; id: string; onDone: () => void }) {
  const [decision, setDecision] = useState("PASSED");
  const [reason, setReason] = useState("");
  const checks = useQuery({ queryKey: ["checks", id], queryFn: () => api.get(`/contracts/${id}/validation-checks`), enabled: open });
  const m = useMutation({ mutationFn: () => api.post(`/contracts/${id}/validate`, { decision, reason }), onSuccess: () => { onDone(); onClose(); } });
  return (
    <Modal open={open} onClose={onClose} title="Validate contract" wide footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button loading={m.isPending} disabled={reason.trim().length < 3} onClick={() => m.mutate()}>Record validation</Button></>}>
      <div className="space-y-4">
        <div>
          <div className="label">Automated checks</div>
          {checks.isLoading ? <Loading /> : <ul className="space-y-1">{(checks.data?.checks ?? []).map((ch: any, i: number) => (
            <li key={i} className="flex gap-2 text-sm"><span className={ch.passed ? "text-emerald-600" : ch.severity === "error" ? "text-rose-600" : "text-amber-600"}>{ch.passed ? "✓" : ch.severity === "error" ? "✗" : "!"}</span>{ch.message}</li>))}</ul>}
        </div>
        <Field label="Decision" htmlFor="vd">
          <select id="vd" className="input" value={decision} onChange={(e) => setDecision(e.target.value)}>
            <option value="PASSED">Passed — send for approval</option><option value="CHANGES_REQUESTED">Changes requested — return to contractor</option><option value="FAILED">Failed</option>
          </select>
        </Field>
        <Field label="Reason (mandatory, audited)" htmlFor="vr"><textarea id="vr" className="input" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        <p className="text-xs text-slate-500">Validation and approval are separate decisions. A validator cannot also approve the same contract.</p>
        {m.isError && <Alert tone="error">{errorMessage(m.error)}</Alert>}
      </div>
    </Modal>
  );
}

function BeneficiaryModal({ open, onClose, id, onDone }: { open: boolean; onClose: () => void; id: string; onDone: () => void }) {
  const [f, setF] = useState({ account_holder_name: "", account_number: "", ifsc: "" });
  const m = useMutation({ mutationFn: () => api.post(`/contracts/${id}/beneficiary`, f), onSuccess: () => { onDone(); onClose(); setF({ account_holder_name: "", account_number: "", ifsc: "" }); } });
  return (
    <Modal open={open} onClose={onClose} title="Add payout account" footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button loading={m.isPending} onClick={() => m.mutate()}>Register account</Button></>}>
      <div className="space-y-4">
        <Alert tone="info">The account number is sent once to the payout provider ({"sandbox"}) and is not stored by FinGuard — only the last four digits. A finance analyst must verify it before payouts.</Alert>
        <Field label="Account holder name" htmlFor="bh"><input id="bh" className="input" value={f.account_holder_name} onChange={(e) => setF({ ...f, account_holder_name: e.target.value })} /></Field>
        <Field label="Account number" htmlFor="bn" hint="Sandbox: last 4 digits select a test scenario (0000 success, 2222 insufficient funds, 4444 timeout, 5555 pending…)"><input id="bn" className="input" inputMode="numeric" autoComplete="off" value={f.account_number} onChange={(e) => setF({ ...f, account_number: e.target.value })} /></Field>
        <Field label="IFSC (India) or ABA routing number (US)" htmlFor="bi"><input id="bi" className="input uppercase" value={f.ifsc} onChange={(e) => setF({ ...f, ifsc: e.target.value })} placeholder="HDFC0001234 · or 222222226" /></Field>
        {m.isError && <Alert tone="error">{errorMessage(m.error)}</Alert>}
      </div>
    </Modal>
  );
}

function AmendModal({ open, onClose, k, onDone }: { open: boolean; onClose: () => void; k: any; onDone: () => void }) {
  const v = k.current_version ?? {};
  const [amount, setAmount] = useState("");
  const [frequency, setFrequency] = useState("");
  const [endDate, setEndDate] = useState("");
  const [eff, setEff] = useState("");
  const [reason, setReason] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const m = useMutation({
    mutationFn: () => {
      const changes: Record<string, string> = {};
      if (amount) changes.payment_amount = amount;
      if (frequency) changes.frequency = frequency;
      if (endDate) changes.end_date = endDate;
      const fd = new FormData();
      fd.append("payload", JSON.stringify({ changes, reason, effective_date: eff }));
      if (file) fd.append("file", file);
      return api.post(`/contracts/${k.id}/amendments`, fd);
    },
    onSuccess: () => { onDone(); onClose(); },
  });
  return (
    <Modal open={open} onClose={onClose} title="Request a contract amendment" wide footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button loading={m.isPending} disabled={!eff || reason.length < 5 || (!amount && !frequency && !endDate)} onClick={() => m.mutate()}>Submit amendment</Button></>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="sm:col-span-2"><Alert tone="info">Approved terms never change in place. Your request creates a proposed version; current terms stay in force until an authorized approver accepts it, from the effective date (aligned to the next unpaid period).</Alert></div>
        <Field label={`New amount (current ${money(v.payment_amount, v.currency)})`} htmlFor="aa"><input id="aa" className="input" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="e.g. 25000" /></Field>
        <Field label={`New frequency (current ${titleCase(v.frequency)})`} htmlFor="af"><select id="af" className="input" value={frequency} onChange={(e) => setFrequency(e.target.value)}><option value="">unchanged</option>{["WEEKLY", "MONTHLY", "QUARTERLY", "ANNUALLY"].map((f) => <option key={f}>{f}</option>)}</select></Field>
        <Field label={`New end date (current ${date(v.end_date)})`} htmlFor="ae"><input id="ae" type="date" className="input" value={endDate} onChange={(e) => setEndDate(e.target.value)} /></Field>
        <Field label="Requested effective date" htmlFor="aeff"><input id="aeff" type="date" className="input" value={eff} onChange={(e) => setEff(e.target.value)} /></Field>
        <div className="sm:col-span-2"><Field label="Reason" htmlFor="ar"><textarea id="ar" className="input" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field></div>
        <div className="sm:col-span-2"><Field label="Signed amendment document (recommended; required for increases above 10%)" htmlFor="adoc"><input id="adoc" type="file" accept=".pdf,.png,.jpg,.jpeg,.docx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-sm" /></Field></div>
        {m.isError && <div className="sm:col-span-2"><Alert tone="error">{errorMessage(m.error)}</Alert></div>}
      </div>
    </Modal>
  );
}

function HoldModal({ open, onClose, k, onDone }: { open: boolean; onClose: () => void; k: any; onDone: () => void }) {
  const [action, setAction] = useState(k.status === "ACTIVE" ? "SUSPEND" : "REACTIVATE");
  const [reason, setReason] = useState("");
  const m = useMutation({ mutationFn: () => api.post(`/contracts/${k.id}/hold`, { action, reason }), onSuccess: () => { onDone(); onClose(); } });
  return (
    <Modal open={open} onClose={onClose} title="Compliance hold" footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button variant="danger" loading={m.isPending} disabled={reason.length < 3} onClick={() => m.mutate()}>Apply</Button></>}>
      <div className="space-y-4">
        <Field label="Action" htmlFor="ha"><select id="ha" className="input" value={action} onChange={(e) => setAction(e.target.value)}>
          {k.status === "ACTIVE" ? <option value="SUSPEND">Suspend (payments held for review)</option> : <option value="REACTIVATE">Reactivate</option>}
          <option value="TERMINATE">Terminate (future payments cancelled)</option></select></Field>
        <Field label="Reason" htmlFor="hr"><textarea id="hr" className="input" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        {m.isError && <Alert tone="error">{errorMessage(m.error)}</Alert>}
      </div>
    </Modal>
  );
}

function VerifyBenModal({ open, onClose, k, onDone }: { open: boolean; onClose: () => void; k: any; onDone: () => void }) {
  const [note, setNote] = useState("");
  const m = useMutation({ mutationFn: (approve: boolean) => api.post(`/beneficiaries/${k.beneficiary.id}/verify`, { approve, note }), onSuccess: () => { onDone(); onClose(); } });
  return (
    <Modal open={open} onClose={onClose} title="Verify payout account" footer={<><Button variant="danger" onClick={() => m.mutate(false)} disabled={note.length < 3}>Reject</Button><Button variant="success" onClick={() => m.mutate(true)} disabled={note.length < 3} loading={m.isPending}>Verify</Button></>}>
      {k.beneficiary && <KV items={[["Holder", k.beneficiary.account_holder_name], ["Account", k.beneficiary.account_masked], ["IFSC / routing", k.beneficiary.ifsc], ["Contract party", k.current_version?.contractor_legal_name ?? k.contractor.name]]} />}
      <div className="mt-4"><Field label="Verification note" htmlFor="vn"><textarea id="vn" className="input" rows={2} value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Name matches contract party; cancelled cheque on file" /></Field></div>
      {m.isError && <div className="mt-3"><Alert tone="error">{errorMessage(m.error)}</Alert></div>}
    </Modal>
  );
}
