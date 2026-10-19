import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import StructuredAnswer from "../components/StructuredAnswer";
import { Alert, Badge, Button, Card, Empty, Field, Loading, PageHeader, Stat, Table, Tabs } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { money, pct } from "../lib/format";

type Tab = "ask" | "statements" | "trends" | "transactions";
const cr = (v: string | number | null | undefined) => (v === null || v === undefined ? 0 : Number(v) / 1e7);
const crLabel = (v: number) => `₹${v.toFixed(0)} cr`;

function fmtVal(v: string | null, unit: string) {
  if (v === null || v === undefined) return "—";
  if (unit === "INR") return money(v);
  if (unit === "PCT") return `${Number(v).toFixed(2)}%`;
  if (unit === "X") return `${Number(v).toFixed(2)}x`;
  if (unit === "DAYS") return `${Number(v).toFixed(1)} days`;
  return Number(v).toLocaleString("en-IN");
}

export default function Financials() {
  const [tab, setTab] = useState<Tab>("ask");
  const ov = useQuery({ queryKey: ["fin", "overview"], queryFn: () => api.get("/financials/overview") });
  const [fy, setFy] = useState(2026);
  if (ov.isLoading) return <Loading />;
  if (ov.isError) return <><PageHeader title="Company financials" /><Alert tone="warn">{errorMessage(ov.error)}</Alert></>;
  const o = ov.data;
  const last = o.years[o.years.length - 1];
  const first = o.years[0];
  const cagr = (Math.pow(Number(last.revenue) / Number(first.revenue), 1 / (o.years.length - 1)) - 1);
  return (
    <>
      <PageHeader title={`${o.company} — financials`} subtitle={`Ledger FY${o.first_fy}–FY${o.last_fy} (1 Apr 2016 – 31 Mar 2026) · ${o.entries.toLocaleString("en-IN")} journal entries · synthetic imaginary company · amounts in INR`}
        actions={<label className="flex items-center gap-2 text-sm">Fiscal year
          <select className="input w-32" value={fy} onChange={(e) => setFy(Number(e.target.value))}>
            {o.years.map((y: any) => <option key={y.fy} value={y.fy}>FY{y.fy}</option>)}</select></label>} />
      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label={`Revenue FY${last.fy}`} value={`₹${cr(last.revenue).toFixed(1)} cr`} hint={`10-yr CAGR ${pct(cagr)}`} />
        <Stat label={`Net profit FY${last.fy}`} value={`₹${cr(last.net_income).toFixed(1)} cr`} hint={`margin ${Number(last.net_margin).toFixed(1)}%`} tone="good" />
        <Stat label="Operating margin" value={`${Number(last.operating_margin).toFixed(1)}%`} />
        <Stat label="Cash (year end)" value={`₹${cr(last.cash).toFixed(1)} cr`} />
        <Stat label="Invoices FY" value={Number(last.invoice_count).toLocaleString("en-IN")} />
      </div>
      <Tabs<Tab> value={tab} onChange={setTab} tabs={[{ id: "ask", label: "Ask a question" }, { id: "statements", label: "Statements" },
        { id: "trends", label: "Trends" }, { id: "transactions", label: "Daily transactions" }]} />
      {tab === "ask" && <Ask />}
      {tab === "statements" && <Statements fy={fy} />}
      {tab === "trends" && <Trends years={o.years} fy={fy} />}
      {tab === "transactions" && <Transactions fy={fy} />}
    </>
  );
}

const EXAMPLES = [
  "Who is the CFO of XYZ?", "What happened in May 2020?", "What are the standard vendor payment terms?",
  "Who is the account manager for Orion Retail?", "Why did XYZ borrow money in 2020?",
  "What was the most recent transaction?", "All hardware and products transactions in FY2025", "Sales to Orion Retail above ₹10 lakh", "Tell me about XYZ Ltd", "Which customers owe us the most?",
  "Show the 5 largest vendor payments in FY2025", "Tell me about Orion Retail", "What data do you have?",
  "What was XYZ Ltd's revenue in FY2024?", "What is the percentage change in net profit from FY2022 to FY2023?",
  "Top 5 customers by revenue in FY2025", "Which month had the highest revenue in FY2021?", "What were total sales on 15 August 2019?",
  "What percentage of FY2024 revenue came from Cloud Services?", "What drove the change in operating margin in FY2022?",
  "What is the FY2021 days payable outstanding (DPO)?", "What is the revenue CAGR from FY2017 to FY2026?",
  "What was the average monthly marketing spend in FY2023?", "What was the current ratio at the end of FY2025?",
  "What was Apple's revenue in FY2023?",
];

function Ask() {
  const [q, setQ] = useState("");
  const [history, setHistory] = useState<any[]>([]);
  const m = useMutation({ mutationFn: (question: string) => api.post("/financials/ask", { question }),
    onSuccess: (d, question) => { setHistory((h) => [{ question, ...d }, ...h]); setQ(""); } });
  const submit = (e?: FormEvent, text?: string) => { e?.preventDefault(); const t = text ?? q; if (t.trim()) m.mutate(t); };
  const tone: Record<string, any> = { ANSWERED: "green", OUT_OF_SCOPE: "red", NO_DATA: "amber", CLARIFY: "blue" };
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_300px]">
      <div className="space-y-4">
        <Card>
          <form onSubmit={submit} className="flex gap-2">
            <label htmlFor="finq" className="sr-only">Question</label>
            <input id="finq" className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask anything about XYZ Ltd — figures, transactions, people, policies, events, customers…" />
            <Button type="submit" loading={m.isPending}>Ask</Button>
          </form>
          {m.isError && <div className="mt-3"><Alert tone="error">{errorMessage(m.error)}</Alert></div>}
        </Card>
        {!history.length && <Empty title="Ask anything about XYZ Ltd's books">Answers are computed exactly from the 10-year ledger and cite the accounts and journal lines used.</Empty>}
        {history.map((h, i) => (
          <Card key={i}>
            <div className="text-sm font-medium text-slate-900">{h.question}</div>
            <div className="mt-3">{h.structured ? <StructuredAnswer s={h.structured} /> : (
              <div className="flex items-start gap-2"><Badge tone={tone[h.status]}>{h.status.replace("_", " ")}</Badge><p className="text-sm leading-relaxed">{h.answer}</p></div>)}</div>
            {h.plan && h.status === "ANSWERED" && (
              <div className="mt-3 flex flex-wrap gap-1.5 border-t border-slate-100 pt-2 text-xs">
                <Badge>metric: {h.plan.metric}</Badge><Badge>operation: {h.plan.operation}</Badge>
                <Badge>understanding confidence {h.plan.confidence}</Badge>
              </div>
            )}
          </Card>
        ))}
      </div>
      <Card title="Try">
        <div className="flex flex-col gap-2">{EXAMPLES.map((e) => (
          <button key={e} className="rounded-lg border border-slate-200 px-3 py-2 text-left text-sm hover:bg-slate-50" onClick={() => submit(undefined, e)}>{e}</button>))}</div>
        <p className="mt-3 text-xs text-slate-500">Questions about other companies are refused; periods outside FY2017–FY2026 return “no data”.</p>
      </Card>
    </div>
  );
}

function Statements({ fy }: { fy: number }) {
  const s = useQuery({ queryKey: ["fin", "statements", fy], queryFn: () => api.get(`/financials/statements?fy=${fy}`) });
  if (s.isLoading) return <Loading />;
  if (s.isError) return <Alert tone="error">{errorMessage(s.error)}</Alert>;
  const block = (title: string, rows: any[]) => (
    <Card title={title} padded={false}>
      <Table head={["Line item", `FY${fy}`, `FY${fy - 1}`, "Change"]}>
        {rows.map((r) => {
          const ch = r.previous !== null && r.unit === "INR" && Number(r.previous) !== 0 ? (Number(r.current) - Number(r.previous)) / Math.abs(Number(r.previous)) : null;
          const bold = ["revenue", "gross_profit", "operating_income", "net_income", "total_assets", "total_liabilities", "equity", "cfo", "fcf"].includes(r.key);
          return (
            <tr key={r.key} className={bold ? "bg-slate-50 font-semibold" : ""}>
              <td className="td">{r.label}</td><td className="td tabular-nums">{fmtVal(r.current, r.unit)}</td>
              <td className="td tabular-nums text-slate-500">{fmtVal(r.previous, r.unit)}</td>
              <td className={`td tabular-nums ${ch !== null && ch < 0 ? "text-rose-600" : "text-emerald-700"}`}>{ch !== null ? pct(ch) : ""}</td>
            </tr>
          );
        })}
      </Table>
    </Card>
  );
  return (
    <div className="grid gap-6 xl:grid-cols-2">
      {block("Statement of profit and loss", s.data.income_statement)}
      <div className="space-y-6">
        {block("Balance sheet (as at 31 March)", s.data.balance_sheet)}
        {block("Cash flow", s.data.cash_flow)}
        {block("Key ratios", s.data.ratios)}
      </div>
    </div>
  );
}

function Trends({ years, fy }: { years: any[]; fy: number }) {
  const mth = useQuery({ queryKey: ["fin", "monthly", fy], queryFn: () => api.get(`/financials/monthly?fy=${fy}`) });
  const yr = years.map((y) => ({ fy: `FY${String(y.fy).slice(2)}`, revenue: cr(y.revenue), net: cr(y.net_income), cfo: cr(y.cfo), capex: cr(y.capex) }));
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="10-year revenue, net profit and operating cash flow (₹ crore)">
        <div className="h-72"><ResponsiveContainer><BarChart data={yr}><CartesianGrid strokeDasharray="3 3" vertical={false} /><XAxis dataKey="fy" /><YAxis tickFormatter={crLabel} width={70} />
          <Tooltip formatter={(v: number) => `₹${v.toFixed(2)} cr`} /><Legend /><Bar dataKey="revenue" name="Revenue" fill="#2563eb" /><Bar dataKey="net" name="Net profit" fill="#10b981" /><Bar dataKey="cfo" name="Cash from operations" fill="#f59e0b" /></BarChart></ResponsiveContainer></div>
      </Card>
      <Card title={`Monthly revenue vs costs — FY${fy} (₹ crore)`}>
        {mth.data ? <div className="h-72"><ResponsiveContainer><LineChart data={mth.data.months.map((m: any) => ({ m: m.month.slice(0, 7), revenue: cr(m.revenue), cogs: cr(m.cogs), opex: cr(m.opex) }))}>
          <CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="m" tick={{ fontSize: 11 }} /><YAxis tickFormatter={crLabel} width={60} /><Tooltip formatter={(v: number) => `₹${v.toFixed(2)} cr`} /><Legend />
          <Line dataKey="revenue" name="Revenue" stroke="#2563eb" strokeWidth={2} /><Line dataKey="cogs" name="Cost of sales" stroke="#ef4444" /><Line dataKey="opex" name="Operating expenses" stroke="#8b5cf6" /></LineChart></ResponsiveContainer></div> : <Loading />}
      </Card>
      {mth.data && <>
        <Card title={`Revenue by segment and region — FY${fy}`} padded={false}>
          <Table head={["Segment / region", "Revenue"]}>
            {[...mth.data.by_segment, ...mth.data.by_region].map((r: any) => <tr key={r.name}><td className="td">{r.name}</td><td className="td tabular-nums">{money(r.revenue)}</td></tr>)}
          </Table>
        </Card>
        <Card title={`Top 10 customers — FY${fy}`} padded={false}>
          <Table head={["Customer", "Revenue"]}>{mth.data.top_customers.map((r: any) => <tr key={r.name}><td className="td">{r.name}</td><td className="td tabular-nums">{money(r.revenue)}</td></tr>)}</Table>
        </Card>
      </>}
    </div>
  );
}

function Transactions({ fy }: { fy: number }) {
  const [f, setF] = useState({ date_from: `${fy - 1}-04-01`, date_to: `${fy - 1}-04-07`, kind: "", q: "" });
  const [applied, setApplied] = useState(f);
  const [page, setPage] = useState(0);
  const qs = new URLSearchParams(Object.entries(applied).filter(([, v]) => v) as [string, string][]);
  const t = useQuery({ queryKey: ["fin", "tx", applied, page], queryFn: () => api.get(`/financials/transactions?limit=50&offset=${page * 50}&${qs}`) });
  return (
    <>
      <Card>
        <form className="grid gap-3 sm:grid-cols-5" onSubmit={(e) => { e.preventDefault(); setPage(0); setApplied(f); }}>
          <Field label="From" htmlFor="tf"><input id="tf" type="date" className="input" value={f.date_from} onChange={(e) => setF({ ...f, date_from: e.target.value })} /></Field>
          <Field label="To" htmlFor="tt"><input id="tt" type="date" className="input" value={f.date_to} onChange={(e) => setF({ ...f, date_to: e.target.value })} /></Field>
          <Field label="Type" htmlFor="tk"><select id="tk" className="input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
            <option value="">all</option>{["SALE", "RECEIPT", "PURCHASE", "VENDOR_PAYMENT", "PAYROLL", "EXPENSE", "CAPEX", "DEPRECIATION", "INTEREST", "TAX_PROVISION", "TAX_PAYMENT", "DIVIDEND", "LOAN_DRAWDOWN", "LOAN_REPAYMENT"].map((k) => <option key={k}>{k}</option>)}</select></Field>
          <Field label="Search" htmlFor="tq"><input id="tq" className="input" value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} placeholder="customer, vendor, doc ref" /></Field>
          <div className="flex items-end"><Button type="submit">Filter</Button></div>
        </form>
      </Card>
      <div className="mt-6"><Card padded={false}>
        {t.isLoading ? <div className="p-5"><Loading /></div> : (
          <>
            <Table head={["Date", "Doc", "Type", "Description", "Lines (Dr / Cr)"]}>
              {(t.data?.items ?? []).map((x: any) => (
                <tr key={x.id}>
                  <td className="td whitespace-nowrap text-xs">{x.date}</td><td className="td font-mono text-[11px]">{x.doc_ref}</td><td className="td text-xs">{x.kind}</td>
                  <td className="td text-sm">{x.description}<div className="text-xs text-slate-500">{[x.segment, x.region, x.cf_category].filter(Boolean).join(" · ")}</div></td>
                  <td className="td text-xs">{x.lines.map((l: any, i: number) => <div key={i} className="tabular-nums">{l.account} {l.account_name}: {Number(l.debit) ? `Dr ${money(l.debit)}` : `Cr ${money(l.credit)}`}</div>)}</td>
                </tr>
              ))}
            </Table>
            <div className="flex items-center justify-between border-t border-slate-100 px-5 py-3 text-sm">
              <span className="text-slate-500">{t.data?.total?.toLocaleString("en-IN")} entries</span>
              <div className="flex gap-2"><Button size="sm" variant="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Prev</Button>
                <Button size="sm" variant="secondary" disabled={(page + 1) * 50 >= (t.data?.total ?? 0)} onClick={() => setPage(page + 1)}>Next</Button></div>
            </div>
          </>
        )}
      </Card></div>
    </>
  );
}
