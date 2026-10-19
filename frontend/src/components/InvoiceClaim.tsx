import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api, errorMessage } from "../lib/api";
import { date, money } from "../lib/format";
import { Alert, Badge, Button, KV, StatusBadge } from "./ui";

// One invoice draft (generated from the approved schedule): preview, download PDF, submit as claim.
export default function InvoiceClaim({ d, onDone }: { d: any; onDone?: () => void }) {
  const qc = useQueryClient();
  const [dlError, setDlError] = useState<string | null>(null);
  const claim = useMutation({
    mutationFn: () => api.post(`/payments/${d.scheduled_payment_id}/claim`),
    onSuccess: () => { qc.invalidateQueries(); onDone?.(); },
  });
  const upload = useMutation({
    mutationFn: (fd: FormData) => api.post(`/payments/${d.scheduled_payment_id}/invoice-upload`, fd),
    onSuccess: () => { qc.invalidateQueries(); onDone?.(); },
  });
  const ex = d.existing_invoice;
  const download = () =>
    (ex?.document_id ? api.download(`/documents/${ex.document_id}/download`, `${d.invoice_number}.pdf`)
      : api.download(`/payments/${d.scheduled_payment_id}/invoice.pdf`, `${d.invoice_number}.pdf`)).catch((e) => setDlError(errorMessage(e)));
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Invoice {d.invoice_number}</div>
          <div className="mt-0.5 text-2xl font-semibold tabular-nums text-slate-900">{money(d.amount, d.currency)}</div>
          <div className="text-xs text-slate-500">{d.amount_source}</div>
        </div>
        <div className="flex flex-col items-end gap-1">
          {ex ? <StatusBadge status={ex.status} /> : <Badge tone={d.claimable ? "amber" : "slate"}>{d.claimable ? "Not yet claimed" : d.billable_from && d.billable_from > new Date().toISOString().slice(0, 10) ? `Can be invoiced from ${date(d.billable_from)}` : "Not claimable"}</Badge>}
          <span className="text-xs text-slate-500">payment <StatusBadge status={d.payment_status} /></span>
        </div>
      </div>
      <div className="mt-3">
        <KV items={[["Service period", `${date(d.period_start)} – ${date(d.period_end)}`], ["Payment due", date(d.due_date)],
          ["Bill to", d.buyer.name], ["Contract", `${d.contract.reference ?? d.contract.title} (v${d.contract.version})`],
          ["Pay to", d.payout_account ?? "—"], ["Description", d.description]]} />
      </div>
      {ex?.review_note && <div className="mt-3"><Alert tone={ex.status === "REJECTED" ? "error" : "info"}>Finance note: {ex.review_note}</Alert></div>}
      {ex?.status === "SUBMITTED" && !claim.isSuccess && <div className="mt-3"><Alert tone="info" title="Awaiting finance review">Automatic validation is switched off for your organization, so finance will review this invoice.</Alert></div>}
      {ex?.status === "FLAGGED" && !claim.isSuccess && !upload.isSuccess && <div className="mt-3"><Alert tone="warn" title="Under risk review">This invoice did not pass automatic validation, so a risk officer is reviewing the claim. You'll be notified of the outcome.</Alert></div>}
      {(claim.isSuccess || upload.isSuccess) && (() => {
        const r: any = ((claim.data ?? upload.data) as any)?.release;
        const scen = r?.tier?.scenario;
        const tone: "success" | "warn" | "info" = !r ? "success" : scen === 3 || ["ESCALATE", "BLOCK", "VERIFY"].includes(r.decision) ? "warn" : scen === 2 ? "info" : "success";
        const title = !r ? "Invoice submitted" : scen === 1 ? "Valid claim within your contract" : scen === 2 ? "Claim needs an approver" : scen === 3 ? "Claim sent to risk review" : "Invoice submitted";
        return <div className="mt-3"><Alert tone={tone} title={title}>{r?.message ?? "Finance has been notified."}</Alert></div>;
      })()}
      {(claim.isError || dlError) && <div className="mt-3"><Alert tone="error">{claim.isError ? errorMessage(claim.error) : dlError}</Alert></div>}
      {upload.isSuccess && <UploadResult r={(upload.data as any)?.release} />}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="secondary" onClick={download}>{ex ? "Download submitted invoice (PDF)" : "Download invoice (PDF)"}</Button>
        {!ex && d.claimable && !upload.isSuccess && <Button loading={claim.isPending} onClick={() => claim.mutate()}>Submit invoice claim</Button>}
      </div>
      {!ex && d.claimable && !claim.isSuccess && <UploadOwnInvoice upload={upload} />}
    </div>
  );
}


// Contractor uploads their OWN invoice (PDF or photo). FinGuard reads it (OCR for scans/photos) and applies the same rules.
function UploadOwnInvoice({ upload }: { upload: any }) {
  const ref = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [num, setNum] = useState("");
  const [amt, setAmt] = useState("");
  const send = () => {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    if (num.trim()) fd.append("invoice_number", num.trim());
    if (amt.trim()) fd.append("amount", amt.trim());
    upload.mutate(fd);
  };
  if (upload.isSuccess) return null;
  return (
    <div className="mt-4 rounded-lg border border-dashed border-slate-300 p-3">
      <div className="text-sm font-medium text-slate-800">Or upload your own invoice</div>
      <p className="mt-0.5 text-xs text-slate-500">PDF, PNG or JPEG. FinGuard reads the invoice number, total and date (scans and photos are OCR'd). Up to your contract amount it is paid automatically; up to 10% more waits for an approver; anything beyond that, an invalid invoice or a changed payout account goes to risk review.</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input ref={ref} type="file" accept="application/pdf,image/png,image/jpeg" className="text-sm" aria-label="Invoice file"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <input className="input w-40" placeholder="Invoice no. (optional)" value={num} onChange={(e) => setNum(e.target.value)} aria-label="Invoice number" />
        <input className="input w-36" placeholder="Total (optional)" value={amt} onChange={(e) => setAmt(e.target.value)} aria-label="Invoice total" />
        <Button disabled={!file} loading={upload.isPending} onClick={send}>Upload invoice</Button>
      </div>
      {upload.isError && <div className="mt-2"><Alert tone="error">{errorMessage(upload.error)}</Alert></div>}
    </div>
  );
}

function UploadResult({ r }: { r: any }) {
  if (!r) return null;
  const e = r.extracted ?? {};
  const failed = (r.checks ?? []).filter((c: any) => !c.passed);
  return (
    <div className="mt-3 space-y-2">
      <KV items={[["Read from your invoice", `${e.invoice_number ?? "—"} · ${e.amount ? money(e.amount, e.currency) : "—"} · ${e.invoice_date ? date(e.invoice_date) : "no date"}`],
        ["How it was read", `${e.text_method === "OCR" ? "OCR (scan/photo)" : e.text_method === "MIXED" ? "text + OCR" : "PDF text"}${e.ocr_confidence ? ` · confidence ${e.ocr_confidence}%` : ""}`]]} />
      {failed.length > 0 && <div className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900"><div className="font-medium">The invoice failed these checks, so the claim goes to risk review:</div>
        <ul className="ml-4 list-disc">{failed.map((c: any) => <li key={c.check}>{c.detail}</li>)}</ul></div>}
    </div>
  );
}
