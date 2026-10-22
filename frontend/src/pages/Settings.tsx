import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Phase } from "../api";
import { Empty, ErrorBox } from "../components";
import { formatDate, PHASE_LABEL, relativeTime, useAsync } from "../util";

const STATUS_STYLE = { ok: "text-accent", partial: "text-warn", failed: "text-danger", running: "text-info" } as const;
const STATUS_LABEL = { ok: "completata", partial: "parziale", failed: "fallita", running: "in corso" } as const;

export default function Settings() {
  const roots = useAsync(api.roots, []);
  const scans = useAsync(api.scans, []);
  const [error, setError] = useState<Error | null>(null);
  const [path, setPath] = useState("");
  const [depth, setDepth] = useState(3);

  async function run(action: () => Promise<unknown>) {
    setError(null);
    try {
      await action();
      roots.reload();
      scans.reload();
    } catch (e) {
      setError(e as Error);
    }
  }

  async function addRoot(e: FormEvent) {
    e.preventDefault();
    if (!path.trim()) return;
    await run(async () => {
      await api.addRoot(path.trim(), depth);
      setPath("");
      await api.scan();
    });
  }

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <section>
        <h1 className="text-xl font-semibold tracking-tight">Sorgenti</h1>
        <p className="mt-1 text-sm text-muted">
          Directory che Project OS può scansionare. Vengono letti solo metadati, stato Git e README: mai file <code>.env</code>, chiavi o configurazioni.
        </p>

        <form onSubmit={addRoot} className="mt-4 flex flex-wrap gap-2">
          <input className="field flex-1" placeholder="/home/utente/projects" value={path} onChange={(e) => setPath(e.target.value)} aria-label="Percorso" />
          <select className="field w-auto" value={depth} onChange={(e) => setDepth(Number(e.target.value))} aria-label="Profondità">
            {[1, 2, 3, 4, 5, 6].map((d) => (
              <option key={d} value={d}>
                profondità {d}
              </option>
            ))}
          </select>
          <button className="btn btn-primary" type="submit">
            Aggiungi e scansiona
          </button>
        </form>

        <div className="mt-3">
          <ErrorBox error={error ?? roots.error} />
        </div>

        <div className="card mt-3 divide-y divide-line">
          {(roots.data ?? []).map((r) => (
            <div key={r.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <div className={`truncate font-mono text-sm ${r.enabled ? "" : "text-muted line-through"}`}>{r.path}</div>
                <div className="text-xs text-muted">
                  {r.project_count} progetti · aggiunta {relativeTime(r.created_at)}
                </div>
              </div>
              <select
                className="field w-auto py-1 text-xs"
                value={r.max_depth}
                onChange={(e) => run(() => api.updateRoot(r.id, { max_depth: Number(e.target.value) }))}
                aria-label="Profondità"
              >
                {[1, 2, 3, 4, 5, 6].map((d) => (
                  <option key={d} value={d}>
                    profondità {d}
                  </option>
                ))}
              </select>
              <button className="btn py-1 text-xs" onClick={() => run(() => api.updateRoot(r.id, { enabled: !r.enabled }))}>
                {r.enabled ? "Disattiva" : "Attiva"}
              </button>
              <button
                className="btn py-1 text-xs text-danger"
                title="Smette di scansionare questa directory. I progetti già registrati restano."
                onClick={() => run(() => api.removeRoot(r.id))}
              >
                Rimuovi
              </button>
            </div>
          ))}
          {roots.data?.length === 0 && <Empty>Nessuna sorgente. Aggiungi la cartella che contiene i tuoi progetti.</Empty>}
        </div>
      </section>

      <ManualProject />

      <section>
        <h2 className="font-medium">Storico scansioni</h2>
        <p className="mt-1 text-sm text-muted">Una scansione fallita non modifica lo stato già registrato dei progetti.</p>
        <div className="card mt-3 divide-y divide-line">
          {(scans.data ?? []).map((s) => (
            <details key={s.id} className="group px-4 py-3">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm">
                <span>
                  <span className={STATUS_STYLE[s.status]}>{STATUS_LABEL[s.status]}</span>
                  <span className="text-muted"> · {formatDate(s.started_at)}</span>
                </span>
                <span className="text-xs text-muted tabular-nums">
                  {s.stats.candidates ?? 0} trovati · {s.stats.new_projects ?? 0} nuovi · {s.stats.changed ?? 0} cambiati
                  {s.stats.missing ? ` · ${s.stats.missing} spariti` : ""}
                  {s.errors.length ? ` · ${s.errors.length} errori` : ""}
                </span>
              </summary>
              {s.errors.length > 0 && (
                <ul className="mt-2 space-y-1 font-mono text-xs text-danger">
                  {s.errors.map((err, i) => (
                    <li key={i}>
                      {err.path ?? err.root}: {err.error}
                    </li>
                  ))}
                </ul>
              )}
            </details>
          ))}
          {scans.data?.length === 0 && <Empty>Nessuna scansione.</Empty>}
        </div>
      </section>
    </div>
  );
}

function ManualProject() {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [phase, setPhase] = useState<Phase | "">("");
  const [error, setError] = useState<Error | null>(null);
  const navigate = useNavigate();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const p = await api.createProject({ name: name.trim(), path: path.trim() || undefined, phase: phase || null });
      navigate(`/p/${p.slug}`);
    } catch (err) {
      setError(err as Error);
    }
  }

  return (
    <section>
      <h2 className="font-medium">Aggiungi un progetto a mano</h2>
      <p className="mt-1 text-sm text-muted">Per progetti creativi, idee o lavori che non stanno in una directory scansionata.</p>
      <form onSubmit={submit} className="mt-3 grid gap-2 sm:grid-cols-[1fr_1fr_auto_auto]">
        <input className="field" placeholder="Nome" required value={name} onChange={(e) => setName(e.target.value)} aria-label="Nome" />
        <input className="field" placeholder="Percorso (opzionale)" value={path} onChange={(e) => setPath(e.target.value)} aria-label="Percorso" />
        <select className="field w-auto" value={phase} onChange={(e) => setPhase(e.target.value as Phase | "")} aria-label="Fase">
          <option value="">Fase…</option>
          {(Object.keys(PHASE_LABEL) as Phase[]).map((p) => (
            <option key={p} value={p}>
              {PHASE_LABEL[p]}
            </option>
          ))}
        </select>
        <button className="btn btn-primary" type="submit">
          Crea
        </button>
      </form>
      <div className="mt-2">
        <ErrorBox error={error} />
      </div>
    </section>
  );
}
