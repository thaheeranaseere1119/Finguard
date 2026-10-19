import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import Layout from "./components/Layout";
import { Loading } from "./components/ui";
import { useAuth } from "./lib/auth";
import { Policies, SystemHealth, Users } from "./pages/Admin";
import Agent from "./pages/Agent";
import ApprovalDetail from "./pages/ApprovalDetail";
import Approvals from "./pages/Approvals";
import Audit from "./pages/Audit";
import ContractDetail from "./pages/ContractDetail";
import Contracts from "./pages/Contracts";
import Dashboard from "./pages/Dashboard";
import Financials from "./pages/Financials";
import Invoices from "./pages/Invoices";
import Login from "./pages/Login";
import { AccessDenied, NotFound, Notifications, Profile } from "./pages/Misc";
import NewContract from "./pages/NewContract";
import PaymentDetail from "./pages/PaymentDetail";
import Payments, { Reconciliation } from "./pages/Payments";
import Research from "./pages/Research";
import RiskAnalytics from "./pages/RiskAnalytics";

// UI guards improve usability only; every permission is enforced again by the API.
function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="p-10"><Loading label="Restoring session…" /></div>;
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  return <>{children}</>;
}

function Perm({ any, children }: { any: string[]; children: ReactNode }) {
  const { can } = useAuth();
  return any.some(can) ? <>{children}</> : <AccessDenied />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<Dashboard />} />
        <Route path="contracts" element={<Perm any={["contracts:read_own", "contracts:read_org"]}><Contracts /></Perm>} />
        <Route path="contracts/new" element={<Perm any={["contracts:create"]}><NewContract /></Perm>} />
        <Route path="contracts/:id" element={<Perm any={["contracts:read_own", "contracts:read_org"]}><ContractDetail /></Perm>} />
        <Route path="invoices" element={<Perm any={["invoices:submit_own"]}><Invoices /></Perm>} />
        <Route path="payments" element={<Perm any={["payments:read_own", "payments:read_org"]}><Payments /></Perm>} />
        <Route path="payments/:id" element={<Perm any={["payments:read_own", "payments:read_org"]}><PaymentDetail /></Perm>} />
        <Route path="reconciliation" element={<Perm any={["payments:reconcile"]}><Reconciliation /></Perm>} />
        <Route path="approvals" element={<Perm any={["approvals:read"]}><Approvals /></Perm>} />
        <Route path="escalations" element={<Perm any={["escalations:resolve"]}><Approvals escalationsOnly /></Perm>} />
        <Route path="approvals/:id" element={<Perm any={["approvals:read"]}><ApprovalDetail /></Perm>} />
        <Route path="financials" element={<Perm any={["financials:read"]}><Financials /></Perm>} />
        <Route path="agent" element={<Perm any={["agent:use"]}><Agent /></Perm>} />
        <Route path="audit" element={<Perm any={["audit:read"]}><Audit /></Perm>} />
        <Route path="risk" element={<Perm any={["risk:read"]}><RiskAnalytics /></Perm>} />
        <Route path="research" element={<Perm any={["evaluation:read"]}><Research /></Perm>} />
        <Route path="admin/users" element={<Perm any={["users:manage"]}><Users /></Perm>} />
        <Route path="admin/policies" element={<Perm any={["policies:read"]}><Policies /></Perm>} />
        <Route path="admin/system" element={<Perm any={["system:read"]}><SystemHealth /></Perm>} />
        <Route path="notifications" element={<Notifications />} />
        <Route path="profile" element={<Profile />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
