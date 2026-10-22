import type { ReactNode } from "react";
import type { Health, Lifecycle } from "./api";
import { HEALTH_HINT, HEALTH_LABEL, LIFECYCLE_LABEL } from "./util";

const HEALTH_STYLE: Record<Health, string> = {
  healthy: "bg-accent",
  stale: "bg-warn",
  missing: "bg-danger",
  unknown: "bg-muted",
};

export function HealthDot({ health }: { health: Health }) {
  return (
    <span
      title={`${HEALTH_LABEL[health]} — ${HEALTH_HINT[health]}`}
      className={`inline-block size-2 shrink-0 rounded-full ${HEALTH_STYLE[health]}`}
    />
  );
}

export function HealthBadge({ health }: { health: Health }) {
  const tone: Record<Health, string> = {
    healthy: "bg-accent-soft text-accent",
    stale: "bg-warn-soft text-warn",
    missing: "bg-danger-soft text-danger",
    unknown: "bg-surface-2 text-muted",
  };
  return (
    <span title={HEALTH_HINT[health]} className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${tone[health]}`}>
      <HealthDot health={health} />
      {HEALTH_LABEL[health]}
    </span>
  );
}

export function LifecycleBadge({ lifecycle }: { lifecycle: Lifecycle }) {
  if (lifecycle === "active") return null;
  return <Chip>{LIFECYCLE_LABEL[lifecycle]}</Chip>;
}

export function Chip({ children, mono }: { children: ReactNode; mono?: boolean }) {
  return (
    <span className={`inline-flex items-center rounded border border-line bg-surface-2 px-1.5 py-px text-[11px] text-muted ${mono ? "font-mono" : ""}`}>
      {children}
    </span>
  );
}

export function Stat({ label, value, tone, hint }: { label: string; value: ReactNode; tone?: "warn" | "danger" | "accent"; hint?: string }) {
  const color = tone ? { warn: "text-warn", danger: "text-danger", accent: "text-accent" }[tone] : "text-fg";
  return (
    <div className="card px-4 py-3" title={hint}>
      <div className="text-xs text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  );
}

export function ErrorBox({ error }: { error: Error | null }) {
  if (!error) return null;
  return <div className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-sm text-danger">{error.message}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-4 py-10 text-center text-sm text-muted">{children}</div>;
}

export function Panel({ title, subtitle, action, children }: { title: string; subtitle?: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="card p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium">{title}</h2>
          {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
        </div>
        {action}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs text-muted">{label}</span>
      {children}
    </label>
  );
}
