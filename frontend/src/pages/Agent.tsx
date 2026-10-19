import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import StructuredAnswer from "../components/StructuredAnswer";
import { Alert, Badge, Button, Card, PageHeader, StatusBadge } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dateTime } from "../lib/format";

const SUGGESTIONS: Record<string, string[]> = {
  CONTRACTOR: ["What are the payment terms of my contract?", "When is my next payment due?", "How much have I been paid so far?",
    "Why is my payment held?", "What does my contract say about termination notice?", "Compare my pending amendment with the current terms"],
  FINANCIALS: ["Who is the CFO of XYZ?", "What happened in May 2020?", "What was the most recent transaction?", "All hardware and products transactions in FY2025", "Travel transactions above ₹50,000 in 2025", "Tell me about XYZ Ltd", "Give me the last 10 months revenue",
    "Which customers owe us the most?", "Top 5 customers by revenue in FY2025", "Show the 5 largest vendor payments in FY2025",
    "What drove the change in operating margin in FY2022?", "Tell me about Orion Retail"],
  default: ["Show upcoming payments", "Why was the last payment escalated?", "What does the contract say about invoices?",
    "Calculate totals paid for the latest contract", "List failed payments"],
};

export default function Agent() {
  const { primaryRole, can } = useAuth();
  const qc = useQueryClient();
  const [msg, setMsg] = useState("");
  const runs = useQuery({ queryKey: ["agent", "runs"], queryFn: () => api.get("/agent/runs") });
  const chat = useMutation({ mutationFn: (m: string) => api.post("/agent/chat", { message: m }), onSuccess: () => { setMsg(""); qc.invalidateQueries({ queryKey: ["agent", "runs"] }); } });
  const send = (e?: FormEvent, text?: string) => { e?.preventDefault(); const m = text ?? msg; if (m.trim()) chat.mutate(m); };
  const items = [...(runs.data ?? [])].reverse();
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [items.length, chat.isPending]);
  return (
    <>
      <PageHeader title="AI financial agent" subtitle="Answers from your authorized records only, with evidence. It can prepare governed requests but cannot pay, approve, change contracts or permissions." />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_280px]">
        <Card padded={false}>
          <div className="flex h-[72vh] min-h-[420px] flex-col">
            <div ref={scroller} className="flex-1 space-y-5 overflow-y-auto p-5" aria-live="polite">
              {!items.length && <div className="text-sm text-slate-500">Ask a question about your contracts, schedules, payments or decisions.</div>}
              {items.map((r: any) => (
                <div key={r.id} className="space-y-2">
                  <div className="ml-auto max-w-[80%] rounded-2xl rounded-br-sm bg-brand-600 px-4 py-2 text-sm text-white">{r.prompt}</div>
                  <div className="w-full max-w-[95%] rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-4 py-3 text-sm shadow-sm">
                    {r.structured ? <StructuredAnswer s={r.structured} /> : <>
                      <div className="whitespace-pre-wrap">{r.answer}</div>
                      {r.limitations?.length > 0 && <div className="mt-2 space-y-1">{r.limitations.map((l: string, i: number) => <div key={i} className="rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">⚠ {l}</div>)}</div>}
                    </>}
                    <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-slate-100 pt-2">
                      {r.tool_calls.map((t: any) => <span key={t.id} title={t.error ?? JSON.stringify(t.arguments)}><Badge tone={t.status === "OK" ? "slate" : "red"}>{t.tool}{t.status !== "OK" ? ` · ${t.status}` : ""}</Badge></span>)}
                      <span className="ml-auto text-[11px] text-slate-400">{r.provider} · {r.latency_ms} ms · {dateTime(r.created_at)}</span>
                    </div>
                    {!r.structured && r.evidence?.length > 0 && (
                      <details className="mt-1 text-xs text-slate-500"><summary className="cursor-pointer">Evidence ({r.evidence.length})</summary>
                        <ul className="mt-1 space-y-0.5">{r.evidence.map((e: any, i: number) => <li key={i}><span className="font-mono">{e.id}</span>{e.page ? ` · p.${e.page}` : ""}{e.text ? ` — “${e.text}”` : ""}</li>)}</ul></details>
                    )}
                  </div>
                </div>
              ))}
              {chat.isPending && <div className="text-sm text-slate-500">Working…</div>}
            </div>
            <form onSubmit={send} className="flex gap-2 border-t border-slate-200 p-3">
              <label htmlFor="agent-msg" className="sr-only">Message</label>
              <input id="agent-msg" className="input" value={msg} onChange={(e) => setMsg(e.target.value)} placeholder="Ask about financials, contracts, payments or decisions…" maxLength={2000} />
              <Button type="submit" loading={chat.isPending}>Send</Button>
            </form>
          </div>
        </Card>
        <div className="space-y-4">
          {chat.isError && <Alert tone="error">{errorMessage(chat.error)}</Alert>}
          <Card title="Try asking">
            <div className="flex flex-col gap-2">{(can("financials:read") ? [...SUGGESTIONS.FINANCIALS, ...SUGGESTIONS.default.slice(0, 2)] : SUGGESTIONS[primaryRole ?? ""] ?? SUGGESTIONS.default).map((s) => (
              <button key={s} className="rounded-lg border border-slate-200 px-3 py-2 text-left text-sm hover:bg-slate-50" onClick={() => send(undefined, s)}>{s}</button>))}</div>
          </Card>
          <Card title="Guardrails">
            <ul className="space-y-1.5 text-xs text-slate-600">
              <li>• Separate machine identity; acts on your behalf with your permissions.</li>
              <li>• Allowlisted tools only; every call re-checks authorization and is audited.</li>
              <li>• Amounts always come from approved contract versions — never from chat.</li>
              <li>• Document text is data: embedded instructions are ignored and flagged.</li>
              <li>• Missing evidence ⇒ <StatusBadge status="VERIFY" /> instead of a guess.</li>
            </ul>
          </Card>
        </div>
      </div>
    </>
  );
}
