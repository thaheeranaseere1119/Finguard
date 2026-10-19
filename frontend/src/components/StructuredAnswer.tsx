import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Badge, cx } from "./ui";

export interface Structured {
  title: string;
  summary: string;
  status?: string;
  blocks: any[];
  assumptions?: string[];
  limitations?: string[];
  sources?: { label: string; detail: string }[];
  formula?: string;
  tools?: string[];
}

const STATUS_TONE: Record<string, string> = { ANSWERED: "green", OUT_OF_SCOPE: "red", NO_DATA: "amber", CLARIFY: "blue", UNAVAILABLE: "amber" };
const STATUS_LABEL: Record<string, string> = { ANSWERED: "Answered", OUT_OF_SCOPE: "Out of scope", NO_DATA: "No data", CLARIFY: "Needs detail", UNAVAILABLE: "Unavailable" };

// Renders the agent's structured output: summary, highlight figures, tables, key-value lists, quotes, assumptions, sources.
export default function StructuredAnswer({ s, compact }: { s: Structured; compact?: boolean }) {
  return (
    <div className="space-y-3">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-slate-900">{s.title}</h3>
          {s.status && <Badge tone={STATUS_TONE[s.status] ?? "slate"}>{STATUS_LABEL[s.status] ?? s.status}</Badge>}
        </div>
        <p className="mt-1 text-sm leading-relaxed text-slate-700">{s.summary}</p>
      </div>
      {s.blocks.map((b, i) => <Block key={i} b={b} compact={compact} />)}
      {!!s.assumptions?.length && (
        <div className="rounded-lg border border-blue-100 bg-blue-50/60 px-3 py-2 text-xs text-blue-900">
          <span className="font-semibold">Assumptions: </span>{s.assumptions.join("; ")}
        </div>
      )}
      {!!s.limitations?.length && (
        <div className="space-y-1">{s.limitations.map((l, i) => <div key={i} className="rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">⚠ {l}</div>)}</div>
      )}
      {!!s.sources?.length && (
        <details className="text-xs text-slate-600">
          <summary className="cursor-pointer select-none">Sources & method ({s.sources.length})</summary>
          <ul className="mt-1 space-y-0.5">{s.sources.map((x, i) => <li key={i}><span className="font-medium">{x.label}:</span> {x.detail}</li>)}</ul>
        </details>
      )}
    </div>
  );
}

function Block({ b, compact }: { b: any; compact?: boolean }) {
  if (b.type === "heading") {
    return <div className="border-t border-slate-100 pt-3"><div className="text-sm font-semibold text-slate-800">{b.text}</div>{b.summary && <p className="text-sm text-slate-600">{b.summary}</p>}</div>;
  }
  if (b.type === "highlights") {
    return (
      <div className={cx("grid gap-2", b.items.length >= 4 ? "grid-cols-2 sm:grid-cols-4" : b.items.length === 3 ? "grid-cols-1 sm:grid-cols-3" : "grid-cols-1 sm:grid-cols-2")}>
        {b.items.map((h: any, i: number) => (
          <div key={i} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
            <div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">{h.label}</div>
            <div className={cx("mt-0.5 text-base font-semibold tabular-nums", h.tone === "good" ? "text-emerald-700" : h.tone === "bad" ? "text-rose-700" : "text-slate-900")}>{h.value}</div>
            {h.note && <div className="text-[11px] text-slate-500">{h.note}</div>}
            {h.detail && <div className="text-[11px] tabular-nums text-slate-400">{h.detail}</div>}
          </div>
        ))}
      </div>
    );
  }
  if (b.type === "table") {
    const data = b.chart?.values?.map((v: number, i: number) => ({ label: b.chart.labels[i], v: v / 1e7 })) ?? [];
    return (
      <div>
        {b.title && <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{b.title}</div>}
        {b.chart && data.length > 1 && !compact && (
          <div className="mb-2 h-40 rounded-lg border border-slate-100 bg-white p-2">
            <ResponsiveContainer><BarChart data={data}><XAxis dataKey="label" tick={{ fontSize: 10 }} interval={0} angle={data.length > 6 ? -30 : 0} textAnchor={data.length > 6 ? "end" : "middle"} height={data.length > 6 ? 50 : 20} />
              <YAxis tick={{ fontSize: 10 }} tickFormatter={(v) => `₹${v.toFixed(0)}cr`} width={52} /><Tooltip formatter={(v: number) => `₹${v.toFixed(2)} crore`} /><Bar dataKey="v" fill="#3b6ff6" radius={[3, 3, 0, 0]} /></BarChart></ResponsiveContainer>
          </div>
        )}
        <div className={"overflow-x-auto rounded-lg border border-slate-200" + (b.rows.length > 25 ? " max-h-[28rem] overflow-y-auto" : "")}>
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="sticky top-0 bg-slate-50"><tr>{b.columns.map((c: string) => <th key={c} scope="col" className="px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-500">{c}</th>)}</tr></thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {b.rows.map((r: string[], i: number) => (
                <tr key={i}>{r.map((c, j) => (
                  <td key={j} className={cx("px-3 py-1.5 tabular-nums", j === 0 ? "font-medium text-slate-800" : "text-slate-700",
                    /^-/.test(c) && /%|pp|crore|₹/.test(c) ? "text-rose-700" : /^\+/.test(c) ? "text-emerald-700" : "")}>{c}</td>))}</tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  }
  if (b.type === "kv") {
    return (
      <div>
        {b.title && <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{b.title}</div>}
        <dl className="grid grid-cols-1 gap-x-6 gap-y-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm sm:grid-cols-2">
          {b.items.map(([k, v]: [string, string], i: number) => (
            <div key={i} className="flex justify-between gap-3 border-b border-slate-50 py-0.5 last:border-0"><dt className="text-slate-500">{k}</dt><dd className="text-right font-medium text-slate-800">{v}</dd></div>
          ))}
        </dl>
      </div>
    );
  }
  if (b.type === "list") {
    return (
      <div>
        {b.title && <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{b.title}</div>}
        <ul className="list-disc space-y-0.5 pl-5 text-sm text-slate-700">{b.items.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul>
      </div>
    );
  }
  if (b.type === "quote") {
    return (
      <div className="space-y-2">
        {b.title && <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">{b.title}</div>}
        {b.items.map((q: any, i: number) => (
          <figure key={i} className="rounded-lg border-l-4 border-brand-500 bg-slate-50 px-3 py-2">
            <blockquote className="text-sm italic text-slate-700">“{q.text}”</blockquote>
            <figcaption className="mt-1 text-[11px] text-slate-500">{q.source}</figcaption>
          </figure>
        ))}
      </div>
    );
  }
  return null;
}
