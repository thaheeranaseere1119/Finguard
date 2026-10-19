import { FACTOR_NAMES, FACTOR_WEIGHTS, dateTime, titleCase } from "../lib/format";
import { Badge, DecisionBadge, cx } from "./ui";

export interface DecisionView {
  decision_id: string;
  decision: string;
  risk_score: string;
  risk_factors?: Record<string, { value: string; rationale: string[]; name: string } | null>;
  contributions?: Record<string, string>;
  policy_version?: string | null;
  hard_rule_results?: { rule_id: string; passed: boolean; on_fail: string; message: string; terminal: boolean }[];
  evidence_refs?: any[];
  reasons: string[];
  required_approver_role?: string | null;
  required_approvals?: number;
  required_next_actions?: string[];
  thresholds?: { act_max: string; escalate_min: string; human_required: boolean };
  action_type?: string;
  created_at?: string | null;
}

function scoreTone(score: number, th?: DecisionView["thresholds"]) {
  const act = th ? Number(th.act_max) : 45;
  const esc = th ? Number(th.escalate_min) : 70;
  if (score >= esc) return "bg-violet-500";
  if (score > act) return "bg-blue-500";
  return "bg-emerald-500";
}

export default function RiskPanel({ d, compact }: { d: DecisionView; compact?: boolean }) {
  const score = Number(d.risk_score);
  const failed = (d.hard_rule_results ?? []).filter((r) => !r.passed);
  const passed = (d.hard_rule_results ?? []).filter((r) => r.passed);
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <DecisionBadge decision={d.decision} />
        {d.action_type && <Badge>{titleCase(d.action_type)}</Badge>}
        {d.policy_version && <span className="text-xs text-slate-500">policy {d.policy_version}</span>}
        {d.created_at && <span className="text-xs text-slate-400">{dateTime(d.created_at)}</span>}
      </div>
      <div>
        <div className="mb-1 flex items-baseline justify-between">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Risk score (3S+5F+3I+3E+3T+3U)</span>
          <span className="text-2xl font-semibold tabular-nums">{score.toFixed(1)}<span className="text-sm text-slate-400"> / 100</span></span>
        </div>
        <div className="relative h-2.5 w-full rounded-full bg-slate-100" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={score} aria-label="Risk score">
          <div className={cx("h-2.5 rounded-full", scoreTone(score, d.thresholds))} style={{ width: `${Math.min(100, score)}%` }} />
          {d.thresholds && !d.thresholds.human_required && (
            <div className="absolute -top-1 h-4.5 w-0.5 bg-slate-500" style={{ left: `${d.thresholds.act_max}%`, height: 18 }} title={`ACT ≤ ${d.thresholds.act_max}`} />
          )}
          {d.thresholds && (
            <div className="absolute -top-1 w-0.5 bg-violet-600" style={{ left: `${d.thresholds.escalate_min}%`, height: 18 }} title={`ESCALATE ≥ ${d.thresholds.escalate_min}`} />
          )}
        </div>
        {d.thresholds && (
          <div className="mt-1 text-[11px] text-slate-500">
            {d.thresholds.human_required ? "Human approval always required for this action" : `ACT ≤ ${d.thresholds.act_max}`} · ESCALATE ≥ {d.thresholds.escalate_min} (example thresholds)
          </div>
        )}
      </div>
      {d.risk_factors && (
        <div className="space-y-2.5">
          {(["S", "F", "I", "E", "T", "U"] as const).map((k) => {
            const f = d.risk_factors?.[k];
            if (!f) return null;
            const v = Number(f.value);
            return (
              <div key={k}>
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium text-slate-700">{k} · {FACTOR_NAMES[k]} <span className="text-slate-400">×{FACTOR_WEIGHTS[k]}</span></span>
                  <span className="tabular-nums text-slate-600">{v.toFixed(1)} / 5{d.contributions?.[k] ? ` → ${Number(d.contributions[k]).toFixed(1)} pts` : ""}</span>
                </div>
                <div className="mt-1 h-1.5 w-full rounded-full bg-slate-100">
                  <div className={cx("h-1.5 rounded-full", v >= 4 ? "bg-rose-500" : v >= 2.5 ? "bg-amber-500" : "bg-emerald-500")} style={{ width: `${(v / 5) * 100}%` }} />
                </div>
                {!compact && f.rationale?.length > 0 && <div className="mt-0.5 text-[11px] text-slate-500">{f.rationale.join(" · ")}</div>}
              </div>
            );
          })}
        </div>
      )}
      {failed.length > 0 && (
        <div>
          <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">Hard rules triggered</div>
          <ul className="space-y-1.5">
            {failed.map((r) => (
              <li key={r.rule_id} className="flex gap-2 rounded-lg border border-rose-100 bg-rose-50/60 px-3 py-2 text-xs">
                <DecisionBadge decision={r.on_fail} />
                <span><span className="font-mono text-[11px] text-slate-500">{r.rule_id}</span> — {r.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div>
        <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">Why</div>
        <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700">{d.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        {d.required_next_actions && d.required_next_actions.length > 0 && (
          <div className="mt-2 text-sm text-slate-600"><span className="font-medium">Next: </span>{d.required_next_actions.join(" ")}</div>
        )}
      </div>
      {!compact && passed.length > 0 && (
        <details className="text-xs text-slate-500">
          <summary className="cursor-pointer select-none">{passed.length} controls passed</summary>
          <ul className="mt-1 space-y-0.5 pl-4">{passed.map((r) => <li key={r.rule_id}><span className="font-mono">{r.rule_id}</span></li>)}</ul>
        </details>
      )}
      {!compact && d.evidence_refs && d.evidence_refs.length > 0 && (
        <details className="text-xs text-slate-500">
          <summary className="cursor-pointer select-none">{d.evidence_refs.length} evidence references</summary>
          <pre className="mt-1 max-h-48 overflow-auto rounded bg-slate-50 p-2 text-[11px]">{JSON.stringify(d.evidence_refs, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}
