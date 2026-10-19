import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, Button, Card, Field, Loading, Modal, PageHeader, StatusBadge, Table } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dateTime } from "../lib/format";

export default function Audit() {
  const { can } = useAuth();
  const [f, setF] = useState({ event_type: "", contract_id: "", resource_id: "", outcome: "" });
  const [applied, setApplied] = useState(f);
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<any>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const qs = new URLSearchParams(Object.entries(applied).filter(([, v]) => v) as [string, string][]);
  const q = useQuery({ queryKey: ["audit", applied, page], queryFn: () => api.get(`/audit?limit=50&offset=${page * 50}&${qs}`) });
  const verify = useQuery({ queryKey: ["audit", "verify"], queryFn: () => api.get("/audit/verify") });
  return (
    <>
      <PageHeader title="Audit explorer" subtitle="Append-only, SHA-256 hash-chained events. Reconstruct why any payment was executed, held, blocked or escalated."
        actions={can("audit:export") && <Button variant="secondary" onClick={() => api.download(`/audit/export?${qs}`, "finguard-audit.csv").catch((e) => setExportError(errorMessage(e)))}>Export CSV</Button>} />
      {verify.data && <div className="mb-4"><Alert tone={verify.data.valid ? "success" : "error"} title={verify.data.valid ? "Hash chain verified" : "Hash chain verification FAILED"}>
        {verify.data.checked} entries checked{verify.data.valid ? ` · head ${verify.data.head.slice(0, 16)}…` : ` · broken at sequence ${verify.data.broken_at_seq}`}</Alert></div>}
      {exportError && <div className="mb-4"><Alert tone="error">{exportError}</Alert></div>}
      <Card>
        <form className="grid gap-3 sm:grid-cols-5" onSubmit={(e) => { e.preventDefault(); setPage(0); setApplied(f); }}>
          <Field label="Event type prefix" htmlFor="et"><input id="et" className="input" value={f.event_type} onChange={(e) => setF({ ...f, event_type: e.target.value })} placeholder="payment." /></Field>
          <Field label="Contract ID" htmlFor="cid"><input id="cid" className="input" value={f.contract_id} onChange={(e) => setF({ ...f, contract_id: e.target.value })} /></Field>
          <Field label="Resource ID" htmlFor="rid"><input id="rid" className="input" value={f.resource_id} onChange={(e) => setF({ ...f, resource_id: e.target.value })} /></Field>
          <Field label="Outcome" htmlFor="oc"><select id="oc" className="input" value={f.outcome} onChange={(e) => setF({ ...f, outcome: e.target.value })}><option value="">any</option><option>SUCCESS</option><option>DENIED</option><option>FAILURE</option></select></Field>
          <div className="flex items-end"><Button type="submit">Apply filters</Button></div>
        </form>
      </Card>
      <div className="mt-6">
        <Card padded={false}>
          {q.isLoading ? <div className="p-5"><Loading /></div> : (
            <>
              <Table head={["#", "When", "Event", "Actor", "Resource", "State", "Outcome", ""]}>
                {(q.data?.items ?? []).map((e: any) => (
                  <tr key={e.id} className="hover:bg-slate-50">
                    <td className="td font-mono text-xs">{e.seq}</td>
                    <td className="td whitespace-nowrap text-xs">{dateTime(e.created_at)}</td>
                    <td className="td text-sm">{e.event_type}</td>
                    <td className="td text-xs">{e.actor_kind === "SERVICE" ? <span className="font-medium text-violet-700">{e.actor_roles.join(",")}</span> : e.actor_roles.join(", ") || "anonymous"}</td>
                    <td className="td text-xs">{e.resource_type}<div className="font-mono text-[10px] text-slate-400">{e.resource_id?.slice(0, 8)}</div></td>
                    <td className="td text-xs">{e.previous_state ? `${e.previous_state} → ` : ""}{e.new_state ?? ""}</td>
                    <td className="td"><StatusBadge status={e.outcome} /></td>
                    <td className="td"><Button size="sm" variant="ghost" onClick={() => setOpen(e)}>Inspect</Button></td>
                  </tr>
                ))}
              </Table>
              <div className="flex items-center justify-between border-t border-slate-100 px-5 py-3 text-sm">
                <span className="text-slate-500">{q.data?.total ?? 0} events</span>
                <div className="flex gap-2"><Button size="sm" variant="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Newer</Button>
                  <Button size="sm" variant="secondary" disabled={(page + 1) * 50 >= (q.data?.total ?? 0)} onClick={() => setPage(page + 1)}>Older</Button></div>
              </div>
            </>
          )}
        </Card>
      </div>
      <Modal open={!!open} onClose={() => setOpen(null)} title={`Audit event #${open?.seq}`} wide footer={<Button variant="secondary" onClick={() => setOpen(null)}>Close</Button>}>
        {open && <pre className="max-h-[60vh] overflow-auto rounded-lg bg-slate-900 p-4 text-xs text-slate-100">{JSON.stringify(open, null, 2)}</pre>}
      </Modal>
    </>
  );
}
