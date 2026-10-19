import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Alert, Button, Card, Field, PageHeader, Spinner, StatusBadge } from "../components/ui";
import { api, errorMessage } from "../lib/api";

const STEPS = ["Upload", "OCR & extraction", "Review fields", "Submit for validation"];

export default function NewContract() {
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [contractId, setContractId] = useState<string | null>(null);

  const upload = useMutation({
    mutationFn: () => {
      const fd = new FormData();
      fd.append("title", title);
      fd.append("file", file!);
      return api.post("/contracts", fd);
    },
    onSuccess: (c) => setContractId(c.id),
  });

  const status = useQuery({
    queryKey: ["contract", contractId, "poll"], enabled: !!contractId,
    queryFn: () => api.get(`/contracts/${contractId}`),
    refetchInterval: (q) => (["UPLOADED", "OCR_PROCESSING"].includes(q.state.data?.status) ? 1500 : false),
  });

  const step = !contractId ? 0 : ["UPLOADED", "OCR_PROCESSING"].includes(status.data?.status) ? 1 : 2;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (file) upload.mutate();
  }

  const job = status.data?.ocr_jobs?.[0];
  return (
    <>
      <PageHeader title="Submit a contract" subtitle="Upload the signed contract. Text is extracted (OCR for scans), payment terms are proposed with source evidence, and you confirm them before validation." />
      <div className="mb-4"><Alert tone="info">Submitting a monthly invoice instead? Use <Link to="/invoices" className="font-medium underline">Submit invoice</Link> — this page is only for new contracts.</Alert></div>
      <ol className="mb-6 grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label="Progress">
        {STEPS.map((s, i) => (
          <li key={s} className={`rounded-lg border px-3 py-2 text-xs font-medium ${i < step ? "border-emerald-200 bg-emerald-50 text-emerald-800" : i === step ? "border-brand-200 bg-brand-50 text-brand-800" : "border-slate-200 bg-white text-slate-500"}`}>
            <span className="mr-1.5 tabular-nums">{i + 1}.</span>{s}
          </li>
        ))}
      </ol>
      {!contractId && (
        <Card>
          <form onSubmit={submit} className="max-w-xl space-y-4">
            <Field label="Contract title" htmlFor="title"><input id="title" className="input" required minLength={2} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Data engineering services 2026" /></Field>
            <Field label="Document" htmlFor="file" hint="PDF (searchable or scanned), PNG, JPEG or DOCX · max 15 MB. Stored privately; never publicly accessible.">
              <input id="file" type="file" required accept=".pdf,.png,.jpg,.jpeg,.docx" className="block w-full text-sm file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-3 file:py-2 file:text-sm file:font-medium file:text-brand-700 hover:file:bg-brand-100"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            </Field>
            {upload.isError && <Alert tone="error">{errorMessage(upload.error)}</Alert>}
            <Button type="submit" loading={upload.isPending} disabled={!file}>Upload and extract</Button>
          </form>
        </Card>
      )}
      {contractId && step === 1 && (
        <Card>
          <div className="flex items-center gap-4">
            <Spinner />
            <div>
              <div className="font-medium">Extracting text and payment terms…</div>
              <div className="text-sm text-slate-500">Status: <StatusBadge status={status.data?.status} /> {job && <>· job <StatusBadge status={job.status} /></>}</div>
            </div>
          </div>
        </Card>
      )}
      {contractId && step === 2 && (
        <Card>
          {status.data?.status === "OCR_FAILED" ? (
            <Alert tone="error" title="Processing failed">{status.data?.status_reason ?? job?.error} — open the contract to retry.</Alert>
          ) : (
            <Alert tone="success" title="Extraction complete">
              Method: {job?.method}{job?.mean_ocr_confidence ? ` · mean OCR confidence ${Number(job.mean_ocr_confidence).toFixed(0)}` : ""}. Review every critical field against the original before submitting.
            </Alert>
          )}
          <div className="mt-4"><Link to={`/contracts/${contractId}`}><Button>Open field review</Button></Link></div>
        </Card>
      )}
    </>
  );
}
