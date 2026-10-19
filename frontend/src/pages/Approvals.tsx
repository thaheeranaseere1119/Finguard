import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, DecisionBadge, Empty, Loading, PageHeader, StatusBadge, Table, Tabs } from "../components/ui";
import { api } from "../lib/api";
import { money, relative, titleCase } from "../lib/format";

type Tab = "mine" | "open" | "resolved";

export default function Approvals({ escalationsOnly }: { escalationsOnly?: boolean }) {
  const [tab, setTab] = useState<Tab>("mine");
  const kind = escalationsOnly ? "&kind=ESCALATION" : "";
  const path = tab === "resolved" ? `/approvals?status=APPROVED,REJECTED,CANCELLED${kind}` : `/approvals?status=OPEN${kind}${tab === "mine" ? "&mine=true" : ""}`;
  const q = useQuery({ queryKey: ["approvals", tab, escalationsOnly], queryFn: () => api.get(path) });
  return (
    <>
      <PageHeader title={escalationsOnly ? "Escalated cases" : "Approval queue"}
        subtitle={escalationsOnly ? "High-risk, anomalous or policy-sensitive cases routed to risk & compliance." : "Contracts, amendments and payments that require an authorized human decision."} />
      <Tabs<Tab> value={tab} onChange={setTab} tabs={[{ id: "mine", label: "Actionable by me" }, { id: "open", label: "All open" }, { id: "resolved", label: "Resolved" }]} />
      <Card padded={false}>
        {q.isLoading ? <div className="p-5"><Loading /></div> : q.data?.items?.length ? (
          <Table head={["Request", "Kind", "Amount", "Policy decision", "Requires", "Status", ""]}>
            {q.data.items.map((a: any) => (
              <tr key={a.id} className="hover:bg-slate-50">
                <td className="td"><div className="font-medium">{a.title}</div><div className="max-w-md text-xs text-slate-500 line-clamp-2">{a.summary}</div></td>
                <td className="td text-xs">{titleCase(a.kind)}</td>
                <td className="td whitespace-nowrap tabular-nums">{a.amount ? money(a.amount, a.currency ?? "INR") : "—"}</td>
                <td className="td"><DecisionBadge decision={a.decision} />{a.risk_score && <div className="text-xs text-slate-500">score {Number(a.risk_score).toFixed(1)}</div>}</td>
                <td className="td text-xs">{titleCase(a.required_role)}{a.required_approvals > 1 && <> · {a.decisions.filter((d: any) => d.decision === "APPROVE").length}/{a.required_approvals}</>}</td>
                <td className="td"><StatusBadge status={a.status} /><div className="text-xs text-slate-400">{relative(a.created_at)}</div></td>
                <td className="td text-right"><Link to={`/approvals/${a.id}`}><Button size="sm" variant={a.status === "OPEN" ? "primary" : "secondary"}>{a.status === "OPEN" ? "Review" : "View"}</Button></Link></td>
              </tr>
            ))}
          </Table>
        ) : <div className="p-5"><Empty title="Nothing here" /></div>}
      </Card>
    </>
  );
}
