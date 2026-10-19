import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Button, Spinner } from "./ui";

// Renders a document page (fetched with the bearer token, never from a public URL) and highlights a bounding box.
export default function DocumentViewer({ documentId, pageCount, page, onPage, bbox }:
  { documentId: string; pageCount: number; page: number; onPage: (p: number) => void; bbox?: number[] | null }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let revoked: string | null = null;
    setUrl(null);
    setError(null);
    api.blobUrl(`/documents/${documentId}/pages/${page}`)
      .then((u) => { revoked = u; setUrl(u); })
      .catch(() => setError("Page preview unavailable for this document type."));
    return () => { if (revoked) URL.revokeObjectURL(revoked); };
  }, [documentId, page]);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-xs text-slate-500">
        <span>Original document · page {page} of {pageCount}</span>
        <div className="flex gap-1">
          <Button size="sm" variant="secondary" disabled={page <= 1} onClick={() => onPage(page - 1)}>Prev</Button>
          <Button size="sm" variant="secondary" disabled={page >= pageCount} onClick={() => onPage(page + 1)}>Next</Button>
        </div>
      </div>
      <div className="relative overflow-hidden rounded-lg border border-slate-200 bg-slate-100">
        {!url && !error && <div className="flex h-96 items-center justify-center"><Spinner /></div>}
        {error && <div className="p-6 text-sm text-slate-500">{error}</div>}
        {url && (
          <>
            <img src={url} alt={`Document page ${page}`} className="block w-full" />
            {bbox && bbox.length === 4 && (
              <div className="pointer-events-none absolute rounded-sm border-2 border-amber-500 bg-amber-300/25 shadow-[0_0_0_2000px_rgba(15,23,42,0.08)]"
                style={{ left: `${bbox[0] * 100 - 0.5}%`, top: `${bbox[1] * 100 - 0.3}%`, width: `${(bbox[2] - bbox[0]) * 100 + 1}%`,
                         height: `${(bbox[3] - bbox[1]) * 100 + 0.6}%` }} />
            )}
          </>
        )}
      </div>
    </div>
  );
}
