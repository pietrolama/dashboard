import { useCallback, useEffect, useState } from "react";
import type { Health, Lifecycle, LinkKind, ObservedKind, Phase, ProjectEvent } from "./api";

const rtf = new Intl.RelativeTimeFormat("it", { numeric: "auto" });
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 86400],
  ["month", 30 * 86400],
  ["week", 7 * 86400],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return "adesso";
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("it-IT", { dateStyle: "medium", timeStyle: "short" });
}

export const LIFECYCLE_LABEL: Record<Lifecycle, string> = {
  active: "Attivo",
  paused: "In pausa",
  archived: "Archiviato",
};

export const PHASE_LABEL: Record<Phase, string> = {
  idea: "Idea",
  prototype: "Prototipo",
  development: "Sviluppo",
  stabilization: "Stabilizzazione",
  release: "Rilascio",
};

export const HEALTH_LABEL: Record<Health, string> = {
  healthy: "Recente",
  stale: "Fermo",
  missing: "Non trovato",
  unknown: "Sconosciuto",
};

export const HEALTH_HINT: Record<Health, string> = {
  healthy: "Attività negli ultimi 30 giorni",
  stale: "Attivo ma senza modifiche da oltre 30 giorni",
  missing: "La directory non è stata trovata all'ultima scansione",
  unknown: "Nessuna attività osservata",
};

/** How a relation reads from its source: "A dipende da B". */
export const LINK_LABEL: Record<LinkKind | ObservedKind, string> = {
  depends_on: "dipende da",
  part_of: "fa parte di",
  evolves_from: "è l'evoluzione di",
  related: "collegato a",
  same_remote: "stesso remote di",
  contains: "contiene",
};

/** The same relation read from its target: "B è usato da A". */
export const LINK_LABEL_REVERSE: Record<LinkKind | ObservedKind, string> = {
  depends_on: "è usato da",
  part_of: "include",
  evolves_from: "si è evoluto in",
  related: "collegato a",
  same_remote: "stesso remote di",
  contains: "sta dentro",
};

export const PRIORITY_LABEL = ["Nessuna", "Bassa", "Media", "Alta"];

export function describeEvent(e: ProjectEvent): string {
  const p = e.payload as Record<string, any>;
  switch (e.event_type) {
    case "project_discovered":
      return p.vcs === "git" ? "Repository scoperto" : "Progetto scoperto";
    case "project_created":
      return "Progetto creato manualmente";
    case "project_updated":
      return "Modificato: " + Object.keys(p).join(", ");
    case "git_head_changed":
      return `Nuovo commit: ${p.subject ?? (p.to as string)?.slice(0, 7)}`;
    case "git_branch_changed":
      return `Branch: ${p.from ?? "—"} → ${p.to ?? "detached"}`;
    case "worktree_changed":
      return p.dirty_files || p.untracked_files
        ? `Modifiche non committate: ${p.dirty_files} file, ${p.untracked_files} nuovi`
        : "Working tree pulita";
    case "readme_changed":
      return `${p.file ?? "README"} aggiornato`;
    case "stack_changed":
      return `Stack: ${(p.to as string[])?.join(", ") || "—"}`;
    case "repository_missing":
      return "Directory non più trovata";
    case "repository_restored":
      return "Directory ritrovata";
    case "vcs_changed":
      return `Versionamento: ${p.from} → ${p.to}`;
    case "decision_recorded":
      return `${p.kind === "constraint" ? "Vincolo" : "Decisione"}: ${p.title}`;
    case "decision_superseded":
      return `Decisione sostituita: ${p.title}`;
    case "decision_status_changed":
      return p.to === "review" ? `Da rivedere: ${p.title}` : `Confermata: ${p.title}`;
    case "decision_edited":
      return `Decisione modificata: ${p.title}`;
    case "session_started":
      return `Sessione iniziata: ${p.goal}`;
    case "session_ended":
      return p.next_step ? `Sessione chiusa → ${p.next_step}` : `Sessione chiusa: ${p.goal}`;
    case "session_edited":
      return "Note di sessione corrette";
    case "team_run_created":
      return `Team: richiesta «${p.request}»`;
    case "team_run_started":
      return `Team run #${p.run} avviato`;
    case "team_run_review":
      return `Team run #${p.run} pronto per la revisione`;
    case "team_run_applied":
      return `Team run #${p.run} applicato`;
    case "team_run_discarded":
      return `Team run #${p.run} scartato`;
    case "team_run_failed":
      return `Team run #${p.run} non riuscito`;
    case "analysis_requested":
      return `Analisi AI #${p.analysis} richiesta`;
    case "analysis_done":
      return `Analisi AI #${p.analysis} completata`;
    case "analysis_failed":
      return `Analisi AI #${p.analysis} non riuscita`;
    case "finding_accepted":
      return `Analisi AI: accettato «${p.text}»`;
    case "finding_dismissed":
      return `Analisi AI: ignorato «${p.text}»`;
    case "link_added":
      return `Relazione: ${p.from} ${LINK_LABEL[p.kind as LinkKind] ?? p.kind} ${p.to}`;
    case "link_removed":
      return `Relazione rimossa: ${p.from} ${LINK_LABEL[p.kind as LinkKind] ?? p.kind} ${p.to}`;
    case "brief_exported":
      return `Briefing esportato (${p.freshness})`;
    default:
      return e.event_type;
  }
}

/** Minimal data-fetching hook: refetches when `deps` change or on reload(). */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fn().then(
      (d) => !cancelled && (setData(d), setError(null), setLoading(false)),
      (e) => !cancelled && (setError(e), setLoading(false)),
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload, setData };
}
