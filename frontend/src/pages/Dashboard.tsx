import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Health, type Project } from "../api";
import { Chip, Empty, ErrorBox, HealthDot, LifecycleBadge, Stat } from "../components";
import { describeEvent, HEALTH_LABEL, PHASE_LABEL, relativeTime, useAsync } from "../util";

type LifecycleFilter = "open" | "active" | "paused" | "archived" | "all";

const LIFECYCLE_FILTERS: [LifecycleFilter, string][] = [
  ["open", "Aperti"],
  ["active", "Attivi"],
  ["paused", "In pausa"],
  ["archived", "Archiviati"],
  ["all", "Tutti"],
];

export default function Dashboard() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const lifecycle = (params.get("lifecycle") as LifecycleFilter) ?? "open";
  const health = params.get("health") as Health | null;
  const [query, setQuery] = useState(q);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<Error | null>(null);

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  // Debounce typing into the URL (and therefore into the server query).
  useEffect(() => {
    const t = setTimeout(() => query !== q && setParam("q", query.trim() || null), 200);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const overview = useAsync(api.overview, []);
  const projects = useAsync(() => api.projects({ q: q || undefined }), [q]);
  const events = useAsync(() => api.events(25), []);

  const visible = useMemo(() => {
    return (projects.data ?? []).filter((p) => {
      if (lifecycle === "open" && p.lifecycle === "archived") return false;
      if (lifecycle !== "open" && lifecycle !== "all" && p.lifecycle !== lifecycle) return false;
      if (health && p.health !== health) return false;
      return true;
    });
  }, [projects.data, lifecycle, health]);

  async function scan() {
    setScanning(true);
    setScanError(null);
    try {
      await api.scan();
      overview.reload();
      projects.reload();
      events.reload();
    } catch (e) {
      setScanError(e as Error);
    } finally {
      setScanning(false);
    }
  }

  const o = overview.data;
  const lastScan = o?.last_scan;
  const noRoots = o && o.total === 0 && !lastScan;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">I tuoi progetti</h1>
          <p className="mt-1 text-sm text-muted">
            {lastScan ? (
              <>
                Ultima scansione {relativeTime(lastScan.finished_at ?? lastScan.started_at)}
                {lastScan.status !== "ok" && (
                  <Link to="/settings" className={lastScan.status === "failed" ? "text-danger" : "text-warn"}>
                    {" "}
                    · {lastScan.errors.length} errori
                  </Link>
                )}
              </>
            ) : (
              "Nessuna scansione eseguita"
            )}
          </p>
        </div>
        <button className="btn btn-primary" onClick={scan} disabled={scanning}>
          <svg viewBox="0 0 16 16" className={`size-3.5 ${scanning ? "animate-spin" : ""}`} fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v2.6h-2.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          {scanning ? "Scansione…" : "Scansiona ora"}
        </button>
      </div>

      <ErrorBox error={scanError ?? overview.error} />

      {noRoots ? (
        <div className="card p-8 text-center">
          <h2 className="font-medium">Nessuna sorgente configurata</h2>
          <p className="mt-1 text-sm text-muted">Aggiungi una directory da scansionare, ad esempio la cartella che contiene tutti i tuoi progetti.</p>
          <Link to="/settings" className="btn btn-primary mt-4">
            Configura le sorgenti
          </Link>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Stat label="Progetti" value={o?.total ?? "—"} />
            <Stat label="Attivi questa settimana" value={o?.active_this_week ?? "—"} tone="accent" hint="Commit o modifiche negli ultimi 7 giorni" />
            <Stat label="Modifiche non committate" value={o?.dirty ?? "—"} hint="Repository Git con working tree non pulita" />
            <Stat label="Fermi da 30+ giorni" value={o?.counts.health.stale ?? 0} tone={o?.counts.health.stale ? "warn" : undefined} />
            <Stat label="Non trovati" value={o?.counts.health.missing ?? 0} tone={o?.counts.health.missing ? "danger" : undefined} />
          </div>

          <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
            <section className="min-w-0 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <input
                  className="field max-w-xs"
                  placeholder="Cerca per nome, descrizione, README…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  aria-label="Cerca progetti"
                />
                <div className="flex rounded-md border border-line bg-surface p-0.5">
                  {LIFECYCLE_FILTERS.map(([value, label]) => (
                    <button
                      key={value}
                      onClick={() => setParam("lifecycle", value === "open" ? null : value)}
                      className={`rounded px-2.5 py-1 text-xs ${lifecycle === value ? "bg-surface-2 text-fg" : "text-muted hover:text-fg"}`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <select
                  className="field w-auto py-1 text-xs"
                  value={health ?? ""}
                  onChange={(e) => setParam("health", e.target.value || null)}
                  aria-label="Filtra per stato"
                >
                  <option value="">Ogni stato</option>
                  {(Object.keys(HEALTH_LABEL) as Health[]).map((h) => (
                    <option key={h} value={h}>
                      {HEALTH_LABEL[h]}
                    </option>
                  ))}
                </select>
                <span className="ml-auto text-xs text-muted tabular-nums">{visible.length} progetti</span>
              </div>

              <ErrorBox error={projects.error} />
              <div className="card divide-y divide-line overflow-hidden">
                {visible.map((p) => (
                  <ProjectRow key={p.id} project={p} />
                ))}
                {!projects.loading && visible.length === 0 && <Empty>Nessun progetto corrisponde ai filtri.</Empty>}
                {projects.loading && !projects.data && <Empty>Caricamento…</Empty>}
              </div>
            </section>

            <aside className="space-y-3">
              <h2 className="text-sm font-medium text-muted">Attività recente</h2>
              <div className="card divide-y divide-line">
                {(events.data ?? []).map((e) => (
                  <div key={e.id} className="px-3 py-2.5 text-sm">
                    <div className="flex items-baseline justify-between gap-2">
                      {e.project_slug ? (
                        <Link to={`/p/${e.project_slug}`} className="truncate font-medium hover:text-accent">
                          {e.project_name}
                        </Link>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                      <span className="shrink-0 text-xs text-muted">{relativeTime(e.occurred_at)}</span>
                    </div>
                    <div className="mt-0.5 truncate text-xs text-muted" title={describeEvent(e)}>
                      {describeEvent(e)}
                    </div>
                  </div>
                ))}
                {events.data?.length === 0 && <Empty>Nessun evento.</Empty>}
              </div>
            </aside>
          </div>
        </>
      )}
    </div>
  );
}

function ProjectRow({ project: p }: { project: Project }) {
  const summary = p.description || p.readme_summary;
  const dirty = p.git ? p.git.dirty_files + p.git.untracked_files : 0;
  return (
    <Link to={`/p/${p.slug}`} className="flex gap-3 px-4 py-3 transition hover:bg-surface-2">
      <div className="pt-1.5">
        <HealthDot health={p.health} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-medium">{p.name}</span>
          {p.priority > 0 && (
            <span className="text-xs text-warn" title={`Priorità ${p.priority}/3`}>
              {"●".repeat(p.priority)}
            </span>
          )}
          <LifecycleBadge lifecycle={p.lifecycle} />
          {p.phase && <Chip>{PHASE_LABEL[p.phase]}</Chip>}
          {p.memory.decisions_review > 0 && (
            <span className="rounded bg-warn-soft px-1.5 py-px text-[11px] text-warn" title="Decisioni da rivedere">
              {p.memory.decisions_review} da rivedere
            </span>
          )}
          {p.stack.slice(0, 4).map((s) => (
            <Chip key={s} mono>
              {s}
            </Chip>
          ))}
        </div>
        {summary && <p className="mt-0.5 line-clamp-1 text-sm text-muted">{summary}</p>}
        {(p.memory.open_session || p.memory.next_step) && (
          <p className="mt-0.5 line-clamp-1 text-sm">
            {p.memory.open_session ? (
              <span className="text-info">● In corso: {p.memory.open_session.goal}</span>
            ) : (
              <span className="text-accent">→ {p.memory.next_step}</span>
            )}
          </p>
        )}
      </div>
      <div className="shrink-0 text-right text-xs text-muted">
        <div title={p.last_activity_at ?? undefined}>{relativeTime(p.last_activity_at)}</div>
        {p.git && (
          <div className="mt-0.5 font-mono">
            {p.git.branch ?? "detached"}
            {dirty > 0 && <span className="text-warn"> ±{dirty}</span>}
          </div>
        )}
        {!p.git && p.repositories.length === 0 && <div className="mt-0.5">manuale</div>}
      </div>
    </Link>
  );
}
