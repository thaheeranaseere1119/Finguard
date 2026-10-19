import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { relative, titleCase } from "../lib/format";
import { Badge, cx } from "./ui";

interface NavItem { to: string; label: string; perm?: string[]; icon: ReactNode }

const I = (d: string) => (
  <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d={d} /></svg>
);

const NAV: { section: string; items: NavItem[] }[] = [
  { section: "Overview", items: [
    { to: "/", label: "Dashboard", icon: I("M3 12l9-8 9 8M5 10v10h14V10") },
    { to: "/notifications", label: "Notifications", icon: I("M15 17h5l-1.4-1.4A2 2 0 0118 14.2V11a6 6 0 10-12 0v3.2a2 2 0 01-.6 1.4L4 17h5m6 0a3 3 0 11-6 0") },
  ] },
  { section: "Contracts", items: [
    { to: "/contracts/new", label: "Submit contract", perm: ["contracts:create"], icon: I("M12 5v14M5 12h14") },
    { to: "/contracts", label: "Contracts", perm: ["contracts:read_own", "contracts:read_org"], icon: I("M7 3h7l5 5v13H7zM14 3v5h5") },
  ] },
  { section: "Payments", items: [
    { to: "/invoices", label: "Submit invoice", perm: ["invoices:submit_own"], icon: I("M9 14h6M9 10h6M7 3h10a1 1 0 011 1v17l-3-2-3 2-3-2-3 2V4a1 1 0 011-1z") },
    { to: "/payments", label: "Payments", perm: ["payments:read_own", "payments:read_org"], icon: I("M3 7h18v10H3zM3 11h18") },
    { to: "/reconciliation", label: "Reconciliation", perm: ["payments:reconcile"], icon: I("M4 4v6h6M20 20v-6h-6M5 13a7 7 0 0012 4M19 11A7 7 0 007 7") },
    { to: "/financials", label: "Company financials", perm: ["financials:read"], icon: I("M3 3v18h18M7 15l4-4 3 3 5-6") },
  ] },
  { section: "Governance", items: [
    { to: "/approvals", label: "Approval queue", perm: ["approvals:read"], icon: I("M9 12l2 2 4-4M5 4h14v16H5z") },
    { to: "/escalations", label: "Escalations", perm: ["escalations:resolve"], icon: I("M12 9v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z") },
    { to: "/risk", label: "Risk analytics", perm: ["risk:read"], icon: I("M4 19V9m6 10V5m6 14v-7m4 7H2") },
    { to: "/audit", label: "Audit explorer", perm: ["audit:read"], icon: I("M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4") },
    { to: "/agent", label: "AI agent", perm: ["agent:use"], icon: I("M8 10h.01M12 10h.01M16 10h.01M21 12c0 4.4-4 8-9 8a9.8 9.8 0 01-4-.8L3 20l1.4-3.7A7.5 7.5 0 013 12c0-4.4 4-8 9-8s9 3.6 9 8z") },
  ] },
  { section: "Research & admin", items: [
    { to: "/research", label: "Evaluation", perm: ["evaluation:read"], icon: I("M9 3v6L4 19a1 1 0 001 1h14a1 1 0 001-1L15 9V3M8 3h8") },
    { to: "/admin/users", label: "Users & roles", perm: ["users:manage"], icon: I("M17 20h5v-2a4 4 0 00-5-3.9M9 20H2v-2a4 4 0 015-3.9M15 7a3 3 0 11-6 0 3 3 0 016 0z") },
    { to: "/admin/policies", label: "Risk policy", perm: ["policies:read"], icon: I("M12 3l8 4v5c0 5-3.5 8.5-8 9-4.5-.5-8-4-8-9V7z") },
    { to: "/admin/system", label: "System health", perm: ["system:read"], icon: I("M3 12h4l3-8 4 16 3-8h4") },
  ] },
];

export default function Layout() {
  const { user, can, logout, primaryRole } = useAuth();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const notif = useQuery({ queryKey: ["notifications", "count"], queryFn: () => api.get("/notifications?unread=true"), refetchInterval: 30000 });
  const health = useQuery({ queryKey: ["health"], queryFn: () => fetch("/api/health").then((r) => r.json()), staleTime: 300000 });

  const sections = NAV.map((s) => ({ ...s, items: s.items.filter((i) => !i.perm || i.perm.some(can)) })).filter((s) => s.items.length);

  const sidebar = (
    <nav aria-label="Main" className="flex h-full flex-col gap-5 overflow-y-auto px-3 py-5">
      <Link to="/" className="flex items-center gap-2.5 px-2">
        <img src="/favicon.svg" alt="" className="h-8 w-8" />
        <div>
          <div className="text-sm font-semibold text-white">FinGuard AI</div>
          <div className="text-[11px] text-slate-400">Risk-tiered delegation</div>
        </div>
      </Link>
      {sections.map((s) => (
        <div key={s.section}>
          <div className="px-2 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500">{s.section}</div>
          <ul className="space-y-0.5">
            {s.items.map((i) => (
              <li key={i.to}>
                <NavLink to={i.to} end={i.to === "/" || i.to === "/contracts"} onClick={() => setOpen(false)}
                  className={({ isActive }) => cx("flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition",
                    isActive ? "bg-white/10 text-white" : "text-slate-300 hover:bg-white/5 hover:text-white")}>
                  {i.icon}{i.label}
                  {i.to === "/notifications" && notif.data?.unread > 0 && (
                    <span className="ml-auto rounded-full bg-rose-500 px-1.5 text-[11px] font-semibold text-white">{notif.data.unread}</span>
                  )}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );

  return (
    <div className="min-h-screen lg:pl-60">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 bg-slate-900 lg:block">{sidebar}</aside>
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden" onClick={() => setOpen(false)}>
          <div className="absolute inset-0 bg-slate-900/50" />
          <aside className="absolute inset-y-0 left-0 w-64 bg-slate-900" onClick={(e) => e.stopPropagation()}>{sidebar}</aside>
        </div>
      )}
      <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-slate-200 bg-white/90 px-4 backdrop-blur sm:px-6">
        <button className="rounded-md p-1.5 text-slate-600 hover:bg-slate-100 lg:hidden" onClick={() => setOpen(true)} aria-label="Open navigation">
          {I("M4 6h16M4 12h16M4 18h16")}
        </button>
        <div className="min-w-0 truncate text-sm font-medium text-slate-700">{user?.organization.name}</div>
        {health.data?.payout_mode === "sandbox" && <Badge tone="amber">Payouts: {health.data.payout_provider} sandbox · no real money</Badge>}
        <div className="ml-auto flex items-center gap-3">
          <Link to="/notifications" className="relative rounded-md p-1.5 text-slate-600 hover:bg-slate-100" aria-label={`Notifications (${notif.data?.unread ?? 0} unread)`}>
            {I("M15 17h5l-1.4-1.4A2 2 0 0118 14.2V11a6 6 0 10-12 0v3.2a2 2 0 01-.6 1.4L4 17h5m6 0a3 3 0 11-6 0")}
            {notif.data?.unread > 0 && <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-rose-500" />}
          </Link>
          <Link to="/profile" className="hidden text-right sm:block">
            <div className="text-sm font-medium text-slate-800">{user?.full_name}</div>
            <div className="text-[11px] text-slate-500">{titleCase(primaryRole)}</div>
          </Link>
          <button className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
            onClick={async () => { await logout(); nav("/login"); }}>Sign out</button>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">
        <Outlet />
      </main>
      <footer className="px-6 pb-6 text-center text-[11px] text-slate-400">
        FinGuard AI research prototype · decisions are governed server-side · {notif.data?.items?.[0] ? `last notification ${relative(notif.data.items[0].created_at)}` : ""}
      </footer>
    </div>
  );
}
