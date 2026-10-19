// API client. The access token lives only in memory; the refresh token is an HttpOnly cookie. On a 401 the client
// tries one silent refresh (CSRF double-submit header) and retries the request.

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  requestId?: string;
  constructor(status: number, code: string, message: string, details?: unknown, requestId?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

let accessToken: string | null = null;
let onAuthLost: (() => void) | null = null;
let refreshing: Promise<boolean> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}
export function setOnAuthLost(fn: () => void) {
  onAuthLost = fn;
}

function csrf(): string {
  const m = document.cookie.match(/(?:^|; )fg_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

let inflight: Promise<any | null> | null = null;

// Single-flight: refresh tokens rotate, so concurrent refreshes (StrictMode, parallel 401s) must share one request.
export function refreshSession(): Promise<any | null> {
  if (!inflight) {
    inflight = fetch("/api/v1/auth/refresh", { method: "POST", credentials: "include", headers: { "X-CSRF-Token": csrf() } })
      .then(async (r) => {
        if (!r.ok) return null;
        const data = await r.json();
        accessToken = data.access_token;
        return data;
      })
      .catch(() => null)
      .finally(() => setTimeout(() => (inflight = null), 0));
  }
  return inflight;
}

async function tryRefresh(): Promise<boolean> {
  if (!refreshing) refreshing = refreshSession().then((d) => !!d).finally(() => (refreshing = null));
  return refreshing;
}

export async function request<T = any>(path: string, opts: RequestInit & { raw?: boolean } = {}, retry = true): Promise<T> {
  const headers = new Headers(opts.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (opts.body && !(opts.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(`/api/v1${path}`, { ...opts, headers, credentials: "include" });
  if (res.status === 401 && retry && !path.startsWith("/auth/login")) {
    if (await tryRefresh()) return request<T>(path, opts, false);
    onAuthLost?.();
  }
  if (!res.ok) {
    let body: any = null;
    try {
      body = await res.json();
    } catch {
      /* non-JSON error */
    }
    const e = body?.error ?? {};
    throw new ApiError(res.status, e.code ?? "error", e.message ?? res.statusText, e.details, e.request_id);
  }
  if (opts.raw) return res as unknown as T;
  const ct = res.headers.get("content-type") ?? "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  get: <T = any>(p: string) => request<T>(p),
  post: <T = any>(p: string, body?: unknown) =>
    request<T>(p, { method: "POST", body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body) }),
  put: <T = any>(p: string, body?: unknown) => request<T>(p, { method: "PUT", body: JSON.stringify(body) }),
  del: <T = any>(p: string) => request<T>(p, { method: "DELETE" }),
  blobUrl: async (p: string) => {
    const res = await request<Response>(p, { raw: true });
    return URL.createObjectURL(await res.blob());
  },
  download: async (p: string, filename: string) => {
    const url = await api.blobUrl(p);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  },
};

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.details as any;
    if (d?.unresolved) return `${e.message}: ${d.unresolved.join(", ")}`;
    if (Array.isArray(d)) return `${e.message}: ${d.map((x: any) => x.msg ?? x.message ?? JSON.stringify(x)).join("; ")}`;
    if (d?.failed) return `${e.message}: ${d.failed.map((x: any) => x.message).join("; ")}`;
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}
