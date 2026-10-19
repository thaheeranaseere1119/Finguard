import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Badge, Button, Card, Empty, KV, Loading, PageHeader, Table } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dateTime, relative, titleCase } from "../lib/format";

export function Notifications() {
  const qc = useQueryClient();
  const n = useQuery({ queryKey: ["notifications", "all"], queryFn: () => api.get("/notifications") });
  const read = useMutation({ mutationFn: () => api.post("/notifications/read"), onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }) });
  if (n.isLoading) return <Loading />;
  return (
    <>
      <PageHeader title="Notifications" actions={n.data.unread > 0 && <Button variant="secondary" loading={read.isPending} onClick={() => read.mutate()}>Mark all read</Button>} />
      <Card padded={false}>
        {n.data.items.length ? (
          <ul className="divide-y divide-slate-100">
            {n.data.items.map((x: any) => (
              <li key={x.id} className={`flex gap-3 px-5 py-3 ${x.read ? "" : "bg-brand-50/40"}`}>
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${x.read ? "bg-slate-300" : "bg-brand-600"}`} aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">{x.link ? <Link className="hover:underline" to={x.link}>{x.title}</Link> : x.title}</div>
                  {x.body && <div className="text-sm text-slate-600">{x.body}</div>}
                  <div className="text-xs text-slate-400">{titleCase(x.kind.replace(".", " "))} · {relative(x.created_at)}</div>
                </div>
              </li>
            ))}
          </ul>
        ) : <div className="p-5"><Empty title="No notifications" /></div>}
      </Card>
    </>
  );
}

export function Profile() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["sessions"], queryFn: () => api.get("/auth/sessions") });
  const revoke = useMutation({ mutationFn: (id: string) => api.del(`/auth/sessions/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }) });
  return (
    <>
      <PageHeader title="Profile & sessions" />
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Account"><KV items={[["Name", user?.full_name], ["Email", user?.email], ["Organization", user?.organization.name], ["Company", user?.company_name ?? "—"],
          ["Roles", <div className="flex flex-wrap gap-1">{user?.roles.map((r) => <Badge key={r}>{titleCase(r)}</Badge>)}</div>], ["Permissions", `${user?.permissions.length} granted`]]} /></Card>
        <Card title="Active sessions" padded={false}>
          {s.isLoading ? <Loading /> : (
            <Table head={["Started", "Client", ""]}>
              {(s.data ?? []).map((x: any) => (
                <tr key={x.session_id}><td className="td text-xs">{dateTime(x.created_at)}</td><td className="td max-w-xs truncate text-xs">{x.user_agent} · {x.ip_address}</td>
                  <td className="td text-right">{x.current ? <Badge tone="green">this session</Badge> : <Button size="sm" variant="ghost" onClick={() => revoke.mutate(x.session_id)}>Revoke</Button>}</td></tr>
              ))}
            </Table>
          )}
        </Card>
      </div>
    </>
  );
}

export function AccessDenied() {
  return (
    <div className="mx-auto max-w-md py-20 text-center">
      <div className="text-5xl font-semibold text-slate-300">403</div>
      <h1 className="mt-2 text-lg font-semibold">Access denied</h1>
      <p className="mt-1 text-sm text-slate-500">Your role does not include permission for this page. The server enforces the same rule for direct API calls.</p>
      <Link to="/" className="mt-6 inline-block"><Button variant="secondary">Back to dashboard</Button></Link>
    </div>
  );
}

export function NotFound() {
  return (
    <div className="mx-auto max-w-md py-20 text-center">
      <div className="text-5xl font-semibold text-slate-300">404</div>
      <h1 className="mt-2 text-lg font-semibold">Page not found</h1>
      <Link to="/" className="mt-6 inline-block"><Button variant="secondary">Back to dashboard</Button></Link>
    </div>
  );
}
