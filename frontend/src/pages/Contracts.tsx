import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Empty, Loading, PageHeader, StatusBadge, Table } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, money, titleCase } from "../lib/format";

const FILTERS = [
  ["", "All"], ["NEEDS_REVIEW,OCR_PROCESSING,UPLOADED,OCR_FAILED", "Intake"], ["PENDING_VALIDATION", "Validation"],
  ["PENDING_APPROVAL", "Approval"], ["ACTIVE", "Active"], ["SUSPENDED,EXPIRED,TERMINATED,REJECTED,VALIDATION_FAILED", "Closed / issues"],
];

export default function Contracts() {
  const { can } = useAuth();
  const [status, setStatus] = useState("");
  const q = useQuery({ queryKey: ["contracts", status], queryFn: () => api.get(`/contracts?limit=200${status ? `&status=${status}` : ""}`) });
  return (
    <>
      <PageHeader title="Contracts" subtitle={can("contracts:read_org") ? "All contracts in your organization" : "Your submitted contracts"}
        actions={can("contracts:create") && <Link to="/contracts/new"><Button>Submit contract</Button></Link>} />
      <div className="mb-4 flex flex-wrap gap-2" role="group" aria-label="Filter by status">
        {FILTERS.map(([v, l]) => (
          <button key={l} onClick={() => setStatus(v)} aria-pressed={status === v}
            className={`rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset ${status === v ? "bg-brand-600 text-white ring-brand-600" : "bg-white text-slate-600 ring-slate-300 hover:bg-slate-50"}`}>{l}</button>
        ))}
      </div>
      <Card padded={false}>
        {q.isLoading ? <div className="p-5"><Loading /></div> : q.data?.items?.length ? (
          <Table head={["Contract", "Contractor", "Status", "Payment terms", "Period", "Updated"]}>
            {q.data.items.map((c: any) => {
              const v = c.current_version;
              return (
                <tr key={c.id} className="hover:bg-slate-50">
                  <td className="td"><Link className="font-medium text-brand-700 hover:underline" to={`/contracts/${c.id}`}>{c.title}</Link><div className="text-xs text-slate-500">{c.external_ref ?? "—"}</div></td>
                  <td className="td">{c.contractor.name}{c.contractor.company && <div className="text-xs text-slate-500">{c.contractor.company}</div>}</td>
                  <td className="td"><StatusBadge status={c.status} /></td>
                  <td className="td whitespace-nowrap">{v ? <>{money(v.payment_amount, v.currency)} <span className="text-xs text-slate-500">{titleCase(v.frequency)}</span></> : <span className="text-xs text-slate-400">not approved</span>}</td>
                  <td className="td whitespace-nowrap text-xs">{v ? `${date(v.start_date)} – ${date(v.end_date)}` : "—"}</td>
                  <td className="td whitespace-nowrap text-xs">{date(c.updated_at)}</td>
                </tr>
              );
            })}
          </Table>
        ) : <div className="p-5"><Empty title="No contracts match this filter" /></div>}
      </Card>
    </>
  );
}
