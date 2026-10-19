import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, errorMessage } from "../lib/api";
import DocumentViewer from "./DocumentViewer";
import { Alert, Badge, Button, Field, Modal, StatusBadge, cx } from "./ui";

export interface ExtractedField {
  field_name: string; label: string; kind: string; value: string | null; extracted_value: string | null;
  display_value: string | null; null_reason: string | null; source_page: number | null; source_text: string | null;
  source_bbox: number[] | null; extraction_confidence: string; model_confidence: string | null; ocr_confidence: string | null;
  is_critical: boolean; status: string; extracted_status: string; issues: string[]; candidates: any[]; corrected: boolean;
  correction: { reason: string; previous: string | null; at: string } | null; choices: string[];
}

// Side-by-side review: each field with its source page/text; clicking highlights the evidence on the original.
export default function FieldReview({ contractId, documentId, pageCount, fields, editable, onlyCritical }:
  { contractId: string; documentId: string; pageCount: number; fields: ExtractedField[]; editable: boolean; onlyCritical?: boolean }) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<ExtractedField | null>(fields.find((f) => f.field_name === "payment_amount") ?? null);
  const [page, setPage] = useState(selected?.source_page ?? 1);
  const [editing, setEditing] = useState<ExtractedField | null>(null);
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [showAll, setShowAll] = useState(!onlyCritical);

  const save = useMutation({
    mutationFn: () => api.post(`/contracts/${contractId}/fields`, { field_name: editing!.field_name, value: value || null, reason }),
    onSuccess: () => {
      setEditing(null);
      qc.invalidateQueries({ queryKey: ["extraction", contractId] });
      qc.invalidateQueries({ queryKey: ["contract", contractId] });
    },
  });

  const list = fields.filter((f) => showAll || f.is_critical || f.status !== "OK" || ["total_value", "due_day_of_month", "first_payment_date", "invoice_required", "auto_execute_permitted"].includes(f.field_name))
    .sort((a, b) => Number(b.is_critical) - Number(a.is_critical));

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div>
        <div className="mb-2 flex items-center justify-between">
          <span className="text-xs text-slate-500">Click a field to see its source evidence. Values are proposals until validated.</span>
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> show all fields
          </label>
        </div>
        <div className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {list.map((f) => (
            <button key={f.field_name} type="button"
              onClick={() => { setSelected(f); if (f.source_page) setPage(f.source_page); }}
              className={cx("block w-full px-3 py-2.5 text-left transition hover:bg-slate-50", selected?.field_name === f.field_name && "bg-brand-50/60")}>
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-slate-800">{f.label}</span>
                {f.is_critical && <Badge tone="violet">critical</Badge>}
                {f.corrected && <Badge tone="blue">corrected</Badge>}
                <span className="ml-auto"><StatusBadge status={f.status} /></span>
              </div>
              <div className="mt-0.5 flex items-center gap-3 text-sm">
                <span className={cx("font-mono", f.value === null && "italic text-slate-400")}>{f.value ?? f.null_reason ?? "not found"}</span>
                <span className="text-[11px] text-slate-400">conf {Number(f.extraction_confidence).toFixed(2)}{f.ocr_confidence ? ` · OCR ${Number(f.ocr_confidence).toFixed(0)}` : ""}{f.model_confidence ? ` · model ${f.model_confidence} (untrusted)` : ""}</span>
                {editable && (
                  <span role="button" tabIndex={0} className="ml-auto text-xs font-medium text-brand-600 hover:underline"
                    onClick={(e) => { e.stopPropagation(); setEditing(f); setValue(f.value ?? ""); setReason(""); save.reset(); }}
                    onKeyDown={(e) => { if (e.key === "Enter") { e.stopPropagation(); setEditing(f); setValue(f.value ?? ""); setReason(""); } }}>
                    correct
                  </span>
                )}
              </div>
              {f.issues.length > 0 && <div className="mt-1 text-xs text-amber-700">{f.issues.join(" · ")}</div>}
            </button>
          ))}
        </div>
      </div>
      <div className="xl:sticky xl:top-20 xl:self-start">
        {selected && (
          <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            <div className="font-semibold">{selected.label}: <span className="font-mono">{selected.value ?? "—"}</span></div>
            <div className="mt-0.5">{selected.source_text ? <>Page {selected.source_page}: “{selected.source_text}”</> : "No source text — value missing or provided by a reviewer."}</div>
            {selected.correction && <div className="mt-1 text-blue-800">Corrected from “{selected.correction.previous ?? "∅"}”: {selected.correction.reason}</div>}
          </div>
        )}
        <DocumentViewer documentId={documentId} pageCount={pageCount} page={page} onPage={setPage}
          bbox={selected && selected.source_page === page ? selected.source_bbox : null} />
      </div>

      <Modal open={!!editing} onClose={() => setEditing(null)} title={`Correct “${editing?.label}”`}
        footer={<>
          <Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>
          <Button loading={save.isPending} disabled={reason.trim().length < 3} onClick={() => save.mutate()}>Save correction</Button>
        </>}>
        <div className="space-y-4">
          {editing?.source_text && <Alert tone="info" title={`Source (page ${editing.source_page})`}>“{editing.source_text}”</Alert>}
          <Field label="Corrected value" htmlFor="fv" hint={editing?.kind === "date" ? "YYYY-MM-DD" : editing?.kind === "amount" ? "Plain number, e.g. 20000" : undefined}>
            {editing?.choices?.length ? (
              <select id="fv" className="input" value={value} onChange={(e) => setValue(e.target.value)}>
                <option value="">— none —</option>
                {editing.choices.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            ) : (
              <input id="fv" className="input" value={value} onChange={(e) => setValue(e.target.value)} />
            )}
          </Field>
          <Field label="Reason (recorded in the audit trail)" htmlFor="fr">
            <textarea id="fr" className="input" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Clause 3 states the monthly fee; the second amount is a one-off fee" />
          </Field>
          {save.isError && <Alert tone="error">{errorMessage(save.error)}</Alert>}
        </div>
      </Modal>
    </div>
  );
}
