export function money(amount: string | number | null | undefined, currency = "INR"): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = typeof amount === "number" ? amount : Number(amount);
  if (Number.isNaN(n)) return String(amount);
  try {
    return new Intl.NumberFormat(currency === "INR" ? "en-IN" : "en-US", { style: "currency", currency, maximumFractionDigits: 2 }).format(n);
  } catch {
    return `${currency} ${n.toFixed(2)}`;
  }
}

export function date(v?: string | null): string {
  if (!v) return "—";
  const d = new Date(v.length === 10 ? `${v}T00:00:00` : v);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

export function dateTime(v?: string | null): string {
  if (!v) return "—";
  return new Date(v).toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return "n/a";
  return `${(v * 100).toFixed(digits)}%`;
}

export function titleCase(s?: string | null): string {
  if (!s) return "—";
  return s.toLowerCase().replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function relative(v?: string | null): string {
  if (!v) return "";
  const diff = (Date.now() - new Date(v).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export const FACTOR_NAMES: Record<string, string> = {
  S: "Data sensitivity", F: "Financial impact", I: "Irreversibility", E: "Evidence risk", T: "Tool risk", U: "Model uncertainty",
};
export const FACTOR_WEIGHTS: Record<string, number> = { S: 3, F: 5, I: 3, E: 3, T: 3, U: 3 };
