import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, refreshSession, setAccessToken, setOnAuthLost } from "./api";

export type Role =
  | "CONTRACTOR" | "FINANCE_ANALYST" | "FINANCE_APPROVER" | "RISK_OFFICER" | "FINANCE_MANAGER" | "AUDITOR" | "SYSTEM_ADMIN";

export interface User {
  id: string;
  email: string;
  full_name: string;
  company_name?: string | null;
  roles: Role[];
  permissions: string[];
  organization: { name: string; slug: string; base_currency: string };
}

interface AuthCtx {
  user: User | null;
  loading: boolean;
  login: (organization: string, email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (perm: string) => boolean;
  is: (...roles: Role[]) => boolean;
  primaryRole: Role | null;
}

const Ctx = createContext<AuthCtx | null>(null);
const ROLE_PRIORITY: Role[] = ["SYSTEM_ADMIN", "AUDITOR", "RISK_OFFICER", "FINANCE_MANAGER", "FINANCE_APPROVER", "FINANCE_ANALYST", "CONTRACTOR"];

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setOnAuthLost(() => {
      setAccessToken(null);
      setUser(null);
    });
    refreshSession()
      .then((d) => d && setUser(d.user))
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (organization: string, email: string, password: string) => {
    const d = await api.post("/auth/login", { organization, email, password });
    setAccessToken(d.access_token);
    setUser(d.user);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      setAccessToken(null);
      setUser(null);
    }
  }, []);

  const value = useMemo<AuthCtx>(() => {
    const perms = new Set(user?.permissions ?? []);
    const roles = new Set(user?.roles ?? []);
    return {
      user, loading, login, logout,
      can: (p) => perms.has(p),
      is: (...r) => r.some((x) => roles.has(x)),
      primaryRole: ROLE_PRIORITY.find((r) => roles.has(r)) ?? null,
    };
  }, [user, loading, login, logout]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useAuth outside AuthProvider");
  return c;
}
