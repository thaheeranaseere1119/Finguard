import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Alert, Button, Field } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function Login() {
  const { user, login } = useAuth();
  const nav = useNavigate();
  const loc = useLocation() as { state?: { from?: string } };
  const [mode, setMode] = useState<"login" | "register">("login");
  const [org, setOrg] = useState("acme");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={loc.state?.from ?? "/"} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "register") {
        await api.post("/auth/register", { organization: org, email, full_name: name, password, company_name: company || null });
        setInfo("Account created. You can sign in now.");
        setMode("login");
      } else {
        await login(org, email, password);
        nav(loc.state?.from ?? "/", { replace: true });
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen">
      <div className="hidden w-1/2 flex-col justify-between bg-slate-900 p-12 text-white lg:flex">
        <div className="flex items-center gap-3"><img src="/favicon.svg" alt="" className="h-9 w-9" /><span className="text-lg font-semibold">FinGuard AI</span></div>
        <div>
          <h1 className="text-3xl font-semibold leading-tight">Risk-tiered delegation for financial agents</h1>
          <p className="mt-4 max-w-md text-slate-300">Contract intake with OCR, deterministic payment schedules and an AI agent that must decide to
            <span className="font-semibold text-white"> act</span>, <span className="font-semibold text-white">ask</span>,
            <span className="font-semibold text-white"> verify</span>, <span className="font-semibold text-white">escalate</span> or
            <span className="font-semibold text-white"> block</span> — with every decision explained and audited.</p>
          <div className="mt-8 grid max-w-md grid-cols-5 gap-2 text-center text-[11px] font-semibold">
            {[["ACT", "bg-emerald-500/20 text-emerald-300"], ["ASK", "bg-blue-500/20 text-blue-300"], ["VERIFY", "bg-amber-500/20 text-amber-300"],
              ["ESCALATE", "bg-violet-500/20 text-violet-300"], ["BLOCK", "bg-rose-500/20 text-rose-300"]].map(([l, c]) => (
              <div key={l} className={`rounded-md px-2 py-2 ${c}`}>{l}</div>))}
          </div>
        </div>
        <p className="text-xs text-slate-500">Research prototype. Payouts run against a sandbox provider; no real money moves.</p>
      </div>
      <div className="flex flex-1 items-center justify-center p-6">
        <form onSubmit={submit} className="w-full max-w-sm space-y-4" aria-label={mode === "login" ? "Sign in" : "Register"}>
          <div>
            <h2 className="text-xl font-semibold">{mode === "login" ? "Sign in" : "Contractor registration"}</h2>
            <p className="mt-1 text-sm text-slate-500">{mode === "login" ? "Use your organization code and work email." : "Register to submit contracts to an organization."}</p>
          </div>
          {info && <Alert tone="success">{info}</Alert>}
          {error && <Alert tone="error">{error}</Alert>}
          <Field label="Organization code" htmlFor="org"><input id="org" className="input" value={org} onChange={(e) => setOrg(e.target.value)} required autoComplete="organization" /></Field>
          {mode === "register" && <>
            <Field label="Full legal name" htmlFor="name"><input id="name" className="input" value={name} onChange={(e) => setName(e.target.value)} required /></Field>
            <Field label="Company (optional)" htmlFor="company"><input id="company" className="input" value={company} onChange={(e) => setCompany(e.target.value)} /></Field>
          </>}
          <Field label="Email" htmlFor="email"><input id="email" type="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="username" /></Field>
          <Field label="Password" htmlFor="pw" hint={mode === "register" ? "At least 12 characters with three of: lowercase, uppercase, digit, symbol." : undefined}>
            <input id="pw" type="password" className="input" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete={mode === "login" ? "current-password" : "new-password"} />
          </Field>
          <Button type="submit" className="w-full" loading={busy}>{mode === "login" ? "Sign in" : "Create account"}</Button>
          <button type="button" className="w-full text-center text-sm text-brand-600 hover:underline" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(null); }}>
            {mode === "login" ? "New contractor? Register" : "Already registered? Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
