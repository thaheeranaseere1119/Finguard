import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import InvoiceClaim from "../components/InvoiceClaim";
import { Alert, Card, Empty, Loading, PageHeader, StatusBadge } from "../components/ui";
import { api } from "../lib/api";
import { date, money } from "../lib/format";

// Contractor: submit invoices (generated from the approved schedule, or upload your own PDF/photo) and track them.
export default function Invoices() {
  const q = useQuery({ queryKey: ["my", "invoices"], queryFn: () => api.get("/my/invoices") });
  const [pick, setPick] = useState<string | null>(null);
  if (q.isLoading) return <Loading />;
  if (q.isError) return <Alert tone="error">Could not load your invoices.</Alert>;
  const { ready, submitted, upcoming } = q.data;
  const selected = ready.find((d: any) => d.scheduled_payment_id === pick) ?? ready[0];
  return (
    <>
      <PageHeader title="Submit invoice"
        subtitle="Invoice a finished service period: submit the invoice generated from your approved contract, or upload your own (PDF, PNG or JPEG). A matching invoice is validated and paid automatically; a price increase or anything unclear goes to finance." />
      <div className="space-y-6">
        <Card title="Ready to invoice">
          {!ready.length ? (
            <Empty title="Nothing to invoice right now">
              {upcoming[0] ? <>Your next period ({date(upcoming[0].period_start)} – {date(upcoming[0].period_end)}, {money(upcoming[0].amount, upcoming[0].currency)}) can be invoiced from {date(upcoming[0].billable_from)}.</>
                : "There are no open service periods on your active contracts."}
            </Empty>
          ) : (
            <div className="space-y-4">
              {ready.length > 1 && (
                <div>
                  <label htmlFor="period" className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500">Service period</label>
                  <select id="period" className="input max-w-md" value={selected.scheduled_payment_id} onChange={(e) => setPick(e.target.value)}>
                    {ready.map((d: any) => (
                      <option key={d.scheduled_payment_id} value={d.scheduled_payment_id}>
                        {date(d.period_start)} – {date(d.period_end)} · {money(d.amount, d.currency)} · {d.contract.reference ?? d.contract.title}
                      </option>))}
                  </select>
                  <p className="mt-1 text-xs text-slate-500">{ready.length} periods are waiting for an invoice — oldest first.</p>
                </div>
              )}
              <InvoiceClaim key={selected.scheduled_payment_id} d={selected} onDone={() => q.refetch()} />
            </div>
          )}
        </Card>

        <Card title="My invoices" padded={false}>
          {!submitted.length ? <div className="p-4"><Empty title="No invoices submitted yet" /></div> : (
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead className="bg-slate-50"><tr>
                  {["Invoice", "Service period", "Amount", "Invoice status", "Payment", "Finance note"].map((h) => <th key={h} className="th">{h}</th>)}
                </tr></thead>
                <tbody>
                  {submitted.map((d: any) => (
                    <tr key={d.scheduled_payment_id} className="border-t border-slate-100">
                      <td className="td font-medium"><Link className="text-indigo-700 hover:underline" to={`/payments/${d.scheduled_payment_id}`}>{d.existing_invoice.number}</Link></td>
                      <td className="td">{date(d.period_start)} – {date(d.period_end)}</td>
                      <td className="td tabular-nums">{money(d.amount, d.currency)}</td>
                      <td className="td"><StatusBadge status={d.existing_invoice.status} /></td>
                      <td className="td"><StatusBadge status={d.payment_status} /></td>
                      <td className="td max-w-xs text-xs text-slate-600">{d.existing_invoice.review_note ?? "—"}</td>
                    </tr>))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
