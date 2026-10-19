import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, Badge, Button, Card, Field, Loading, Modal, PageHeader, Stat, StatusBadge, Table } from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dateTime, relative, titleCase } from "../lib/format";

const HUMAN_ROLES = ["CONTRACTOR", "FINANCE_ANALYST", "FINANCE_APPROVER", "RISK_OFFICER", "FINANCE_MANAGER", "AUDITOR", "SYSTEM_ADMIN"];

export function Users() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api.get("/users") });
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => api.get("/roles") });
  const [edit, setEdit] = useState<any>(null);
  const [sel, setSel] = useState<string[]>([]);
  const [create, setCreate] = useState(false);
  const [nu, setNu] = useState({ email: "", full_name: "", password: "", roles: ["FINANCE_ANALYST"] as string[] });
  const save = useMutation({ mutationFn: () => api.put(`/users/${edit.id}/roles`, { roles: sel }), onSuccess: () => { setEdit(null); qc.invalidateQueries({ queryKey: ["users"] }); } });
  const toggle = useMutation({ mutationFn: (u: any) => api.put(`/users/${u.id}/active`, { active: !u.is_active }), onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }) });
  const add = useMutation({ mutationFn: () => api.post("/users", nu), onSuccess: () => { setCreate(false); qc.invalidateQueries({ queryKey: ["users"] }); } });
  if (users.isLoading) return <Loading />;
  return (
    <>
      <PageHeader title="Users & roles" subtitle="Role changes are audited. Administrators cannot grant themselves financial authority; contractors cannot hold internal roles." actions={<Button onClick={() => setCreate(true)}>Add user</Button>} />
      {toggle.isError && <div className="mb-4"><Alert tone="error">{errorMessage(toggle.error)}</Alert></div>}
      <Card padded={false}>
        <Table head={["Name", "Email", "Roles", "Type", "Last login", ""]}>
          {users.data.map((u: any) => (
            <tr key={u.id}>
              <td className="td font-medium">{u.full_name}{!u.is_active && <Badge tone="red">inactive</Badge>}</td><td className="td text-sm">{u.email}</td>
              <td className="td"><div className="flex flex-wrap gap-1">{u.roles.map((r: string) => <Badge key={r} tone={u.kind === "SERVICE" ? "violet" : "slate"}>{titleCase(r)}</Badge>)}</div></td>
              <td className="td text-xs">{u.kind === "SERVICE" ? "machine identity" : "human"}</td><td className="td text-xs">{u.last_login_at ? relative(u.last_login_at) : "—"}</td>
              <td className="td text-right whitespace-nowrap">{u.kind === "HUMAN" && <>
                <Button size="sm" variant="secondary" onClick={() => { setEdit(u); setSel(u.roles); save.reset(); }}>Roles</Button>{" "}
                <Button size="sm" variant="ghost" onClick={() => toggle.mutate(u)}>{u.is_active ? "Deactivate" : "Activate"}</Button></>}</td>
            </tr>
          ))}
        </Table>
      </Card>
      {roles.data && <div className="mt-6"><Card title="Permission matrix (server-enforced RBAC; resource-level ABAC applies in addition)" padded={false}>
        <div className="overflow-x-auto"><table className="min-w-full text-xs">
          <thead className="bg-slate-50"><tr><th className="th sticky left-0 bg-slate-50">Permission</th>{roles.data.roles.map((r: any) => <th key={r.name} className="th text-center">{titleCase(r.name)}</th>)}</tr></thead>
          <tbody className="divide-y divide-slate-100">{Object.entries(roles.data.permissions).map(([p, desc]: [string, any]) => (
            <tr key={p}><td className="sticky left-0 bg-white px-3 py-1.5" title={desc}><span className="font-mono">{p}</span></td>
              {roles.data.roles.map((r: any) => <td key={r.name} className="px-3 py-1.5 text-center">{r.permissions.includes(p) ? <span className="text-emerald-600" aria-label="granted">●</span> : <span className="text-slate-200" aria-label="denied">·</span>}</td>)}</tr>))}
          </tbody></table></div>
      </Card></div>}
      <Modal open={!!edit} onClose={() => setEdit(null)} title={`Roles for ${edit?.full_name}`} footer={<><Button variant="secondary" onClick={() => setEdit(null)}>Cancel</Button><Button loading={save.isPending} onClick={() => save.mutate()}>Save</Button></>}>
        <div className="space-y-2">{HUMAN_ROLES.map((r) => (
          <label key={r} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={sel.includes(r)} onChange={(e) => setSel(e.target.checked ? [...sel, r] : sel.filter((x) => x !== r))} />{titleCase(r)}</label>))}
          {save.isError && <Alert tone="error">{errorMessage(save.error)}</Alert>}</div>
      </Modal>
      <Modal open={create} onClose={() => setCreate(false)} title="Add user" footer={<><Button variant="secondary" onClick={() => setCreate(false)}>Cancel</Button><Button loading={add.isPending} onClick={() => add.mutate()}>Create</Button></>}>
        <div className="space-y-3">
          <Field label="Full name" htmlFor="nn"><input id="nn" className="input" value={nu.full_name} onChange={(e) => setNu({ ...nu, full_name: e.target.value })} /></Field>
          <Field label="Email" htmlFor="ne"><input id="ne" className="input" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} /></Field>
          <Field label="Initial password" htmlFor="np" hint="≥12 chars, three character classes"><input id="np" type="password" className="input" autoComplete="new-password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} /></Field>
          <div className="space-y-1">{HUMAN_ROLES.map((r) => <label key={r} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={nu.roles.includes(r)} onChange={(e) => setNu({ ...nu, roles: e.target.checked ? [...nu.roles, r] : nu.roles.filter((x) => x !== r) })} />{titleCase(r)}</label>)}</div>
          {add.isError && <Alert tone="error">{errorMessage(add.error)}</Alert>}
        </div>
      </Modal>
    </>
  );
}

export function Policies() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const p = useQuery({ queryKey: ["policies"], queryFn: () => api.get("/policies") });
  const [draft, setDraft] = useState<string | null>(null);
  const [version, setVersion] = useState("");
  const [note, setNote] = useState("");
  const create = useMutation({ mutationFn: () => api.post("/policies", { version, config: JSON.parse(draft!), note, activate: true }), onSuccess: () => { setDraft(null); qc.invalidateQueries({ queryKey: ["policies"] }); } });
  if (p.isLoading) return <Loading />;
  const active = p.data.find((x: any) => x.status === "ACTIVE");
  const th = active?.config.thresholds ?? {};
  return (
    <>
      <PageHeader title="Risk & governance policy" subtitle="Versioned, hashed and audited. Every decision records the policy version it used."
        actions={can("policies:manage") && active && <Button onClick={() => { setDraft(JSON.stringify(active.config, null, 2)); setVersion(""); setNote(""); }}>New version</Button>} />
      <Alert tone="warn">Thresholds and limits are EXAMPLE research values for a prototype. They must be calibrated on held-out data before any operational use.</Alert>
      {active && <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card title={`Active: ${active.version}`}>
          <div className="mb-3 text-xs text-slate-500">hash <span className="font-mono">{active.config_hash.slice(0, 16)}…</span> · {dateTime(active.created_at)} · {active.change_note}</div>
          <div className="mb-2 text-sm font-medium">Risk = {Object.entries(active.config.weights).map(([k, w]) => `${w}${k}`).join(" + ")}</div>
          <Table head={["Action", "ACT ≤", "ESCALATE ≥", "Human always"]}>
            {Object.entries(th).map(([k, v]: [string, any]) => <tr key={k}><td className="td text-xs">{titleCase(k)}</td><td className="td">{v.human_required ? "—" : v.act_max}</td><td className="td">{v.escalate_min}</td><td className="td">{v.human_required ? "yes" : "no"}</td></tr>)}
          </Table>
        </Card>
        <Card title="Approval limits & controls">
          <Table head={["Setting", "Value"]}>
            {Object.entries(active.config.approval_limits).map(([r, v]: [string, any]) => <tr key={r}><td className="td text-sm">{titleCase(r)} limit</td><td className="td">₹{Number(v).toLocaleString("en-IN")}</td></tr>)}
            <tr><td className="td text-sm">Dual approval above</td><td className="td">₹{Number(active.config.dual_approval_threshold).toLocaleString("en-IN")}</td></tr>
            <tr><td className="td text-sm">Contract approval by manager above</td><td className="td">₹{Number(active.config.contract_manager_threshold).toLocaleString("en-IN")}</td></tr>
            <tr><td className="td text-sm">Automatic execution enabled</td><td className="td">{String(active.config.auto_execute_enabled)}</td></tr>
            <tr><td className="td text-sm">Validator may approve</td><td className="td">{String(active.config.allow_validator_as_approver)}</td></tr>
          </Table>
        </Card>
      </div>}
      <div className="mt-6"><Card title="Version history" padded={false}>
        <Table head={["Version", "Status", "Hash", "Note", "Created"]}>
          {p.data.map((x: any) => <tr key={x.id}><td className="td">{x.version}</td><td className="td"><StatusBadge status={x.status === "ACTIVE" ? "ACTIVE" : x.status} /></td><td className="td font-mono text-xs">{x.config_hash.slice(0, 12)}</td><td className="td text-sm">{x.change_note}</td><td className="td text-xs">{dateTime(x.created_at)}</td></tr>)}
        </Table>
      </Card></div>
      <Modal open={draft !== null} onClose={() => setDraft(null)} title="New policy version" wide footer={<><Button variant="secondary" onClick={() => setDraft(null)}>Cancel</Button><Button loading={create.isPending} disabled={!version || note.length < 3} onClick={() => create.mutate()}>Create & activate</Button></>}>
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2"><Field label="Version" htmlFor="pv"><input id="pv" className="input" value={version} onChange={(e) => setVersion(e.target.value)} placeholder="v1.1.0-calibrated" /></Field>
            <Field label="Change note" htmlFor="pn"><input id="pn" className="input" value={note} onChange={(e) => setNote(e.target.value)} /></Field></div>
          <Field label="Configuration (JSON)" htmlFor="pc"><textarea id="pc" className="input font-mono text-xs" rows={18} value={draft ?? ""} onChange={(e) => setDraft(e.target.value)} /></Field>
          {create.isError && <Alert tone="error">{errorMessage(create.error)}</Alert>}
        </div>
      </Modal>
    </>
  );
}

export function SystemHealth() {
  const qc = useQueryClient();
  const h = useQuery({ queryKey: ["health", "system"], queryFn: () => api.get("/system/health"), refetchInterval: 15000 });
  const tick = useMutation({ mutationFn: () => api.post("/system/scheduler/tick"), onSuccess: () => qc.invalidateQueries() });
  if (h.isLoading) return <Loading />;
  const s = h.data;
  return (
    <>
      <PageHeader title="System health" subtitle="Integration status without secrets." actions={s.environment !== "production" && <Button variant="secondary" loading={tick.isPending} onClick={() => tick.mutate()}>Run scheduler cycle</Button>} />
      {tick.isSuccess && <div className="mb-4"><Alert tone="success">{JSON.stringify(tick.data)}</Alert></div>}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Environment" value={s.environment} />
        <Stat label="Database" value={s.database.ok ? "OK" : "Down"} tone={s.database.ok ? "good" : "bad"} hint={s.database.revision ? `alembic ${s.database.revision}` : s.database.error} />
        <Stat label="Redis / queue" value={s.redis.ok ? "OK" : "Unavailable"} tone={s.redis.ok ? "good" : "warn"} />
        <Stat label="OCR engine" value={s.ocr.ok ? "OK" : "Unavailable"} tone={s.ocr.ok ? "good" : "bad"} hint={s.ocr.engine ?? s.ocr.error} />
      </div>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card title="Integrations">
          <Table head={["Integration", "Status"]}>
            <tr><td className="td">LLM provider</td><td className="td">{s.llm.provider} · {s.llm.model} {s.llm.configured ? <Badge tone="green">configured</Badge> : <Badge tone="red">missing key</Badge>}</td></tr>
            <tr><td className="td">Payout provider</td><td className="td">{s.payouts.provider} · {s.payouts.mode} {s.payouts.live_money_enabled ? <Badge tone="red">LIVE MONEY</Badge> : <Badge tone="green">no real money</Badge>}</td></tr>
            <tr><td className="td">RazorpayX sandbox credentials</td><td className="td">{s.payouts.razorpayx_configured ? <Badge tone="green">present</Badge> : <Badge>not configured</Badge>}</td></tr>
          </Table>
        </Card>
        <Card title="Background jobs">
          {s.jobs.length ? <Table head={["Job", "Last seen", "Healthy", "Last result"]}>
            {s.jobs.map((j: any) => <tr key={j.name}><td className="td text-sm">{j.name}</td><td className="td text-xs">{relative(j.last_seen_at)}</td><td className="td">{j.healthy ? <Badge tone="green">yes</Badge> : <Badge tone="amber">stale</Badge>}</td>
              <td className="td max-w-xs text-[11px] text-slate-500"><span className="line-clamp-2">{JSON.stringify(j.last_result)}</span></td></tr>)}
          </Table> : <Alert tone="info">No heartbeat yet — start the scheduler (<code>python -m finguard.worker.main scheduler</code>).</Alert>}
          <div className="mt-3 text-xs text-slate-500">OCR queue: {Object.entries(s.ocr_queue).map(([k, v]) => `${k} ${v}`).join(" · ") || "empty"}</div>
        </Card>
      </div>
    </>
  );
}
