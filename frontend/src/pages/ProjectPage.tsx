import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type GitState, type Lifecycle, type Phase, type ProjectDetail } from "../api";
import { Chip, Empty, ErrorBox, Field, HealthBadge, Panel } from "../components";
import { DecisionsPanel, ResumePanel, SessionsPanel } from "../memory";
import { ResumeDialog } from "../resume";
import { describeEvent, formatDate, HEALTH_HINT, LIFECYCLE_LABEL, PHASE_LABEL, PRIORITY_LABEL, relativeTime, useAsync } from "../util";

const SOURCE_LABEL: Record<string, string> = { git: "Git", readme: "README", filesystem: "Filesystem" };

export default function ProjectPage() {
  const { slug = "" } = useParams();
  const { data: p, error, loading, setData, reload } = useAsync(() => api.project(slug), [slug]);
  const [saveError, setSaveError] = useState<Error | null>(null);
  const [resumeOpen, setResumeOpen] = useState(false);
  const navigate = useNavigate();

  async function save(patch: Parameters<typeof api.updateProject>[1]) {
    setSaveError(null);
    try {
      const updated = await api.updateProject(slug, patch);
      if (updated.slug !== slug) navigate(`/p/${updated.slug}`, { replace: true });
      setData(updated);
    } catch (e) {
      setSaveError(e as Error);
      reload();
    }
  }

  async function remove() {
    if (!p) return;
    try {
      await api.deleteProject(slug);
      navigate("/");
    } catch (e) {
      setSaveError(e as Error);
    }
  }

  if (error) return <ErrorBox error={error} />;
  if (loading && !p) return <Empty>Caricamento…</Empty>;
  if (!p) return null;

  const canDelete = p.origin === "manual" || p.health === "missing";

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="text-sm text-muted hover:text-fg">
          ← Tutti i progetti
        </Link>
        <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <EditableTitle value={p.name} onSave={(name) => save({ name })} />
            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted">
              <HealthBadge health={p.health} />
              <span title={formatDate(p.last_activity_at)}>Ultima attività {relativeTime(p.last_activity_at)}</span>
              <span>·</span>
              <span title={formatDate(p.last_observed_at)}>osservato {relativeTime(p.last_observed_at)}</span>
              {p.stack.map((s) => (
                <Chip key={s} mono>
                  {s}
                </Chip>
              ))}
            </div>
          </div>
          <button className="btn btn-primary" onClick={() => setResumeOpen(true)}>
            Riprendi progetto
          </button>
        </div>
      </div>

      <ErrorBox error={saveError} />
      {resumeOpen && <ResumeDialog slug={p.slug} onClose={() => setResumeOpen(false)} onExported={reload} />}

      <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
        <div className="min-w-0 space-y-6">
          <ResumePanel project={p} onChange={reload} />

          <DecisionsPanel project={p} onChange={reload} />

          <Panel title="Descrizione" subtitle="Dichiarata da te. Lo scanner non la modifica mai.">
            <DescriptionEditor key={p.description ?? ""} value={p.description} placeholder={p.readme_summary} onSave={(description) => save({ description })} />
          </Panel>

          {p.git && <GitPanel git={p.git} />}

          <SessionsPanel sessions={p.sessions} />

          {p.evidence.readme && <ReadmePanel project={p} />}

          <Panel title="Cronologia" subtitle="Eventi registrati dallo scanner e dalle tue modifiche.">
            {p.events.length === 0 ? (
              <Empty>Nessun evento.</Empty>
            ) : (
              <ol className="relative space-y-3 border-l border-line pl-4">
                {p.events.map((e) => (
                  <li key={e.id} className="text-sm">
                    <span className="absolute -left-[3.5px] mt-1.5 size-1.5 rounded-full bg-muted" />
                    <div>{describeEvent(e)}</div>
                    <div className="text-xs text-muted" title={formatDate(e.occurred_at)}>
                      {relativeTime(e.occurred_at)} · {e.source}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Panel>
        </div>

        <aside className="space-y-6">
          <Panel title="Stato dichiarato">
            <div className="space-y-3">
              <Field label="Ciclo di vita">
                <select className="field" value={p.lifecycle} onChange={(e) => save({ lifecycle: e.target.value as Lifecycle })}>
                  {(Object.keys(LIFECYCLE_LABEL) as Lifecycle[]).map((l) => (
                    <option key={l} value={l}>
                      {LIFECYCLE_LABEL[l]}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Fase">
                <select className="field" value={p.phase ?? ""} onChange={(e) => save({ phase: (e.target.value || null) as Phase | null })}>
                  <option value="">Non definita</option>
                  {(Object.keys(PHASE_LABEL) as Phase[]).map((ph) => (
                    <option key={ph} value={ph}>
                      {PHASE_LABEL[ph]}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Priorità">
                <select className="field" value={p.priority} onChange={(e) => save({ priority: Number(e.target.value) })}>
                  {PRIORITY_LABEL.map((label, i) => (
                    <option key={i} value={i}>
                      {label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
          </Panel>

          <Panel title="Posizione">
            {p.repositories.length === 0 ? (
              <p className="text-sm text-muted">Progetto manuale senza directory.</p>
            ) : (
              <ul className="space-y-3">
                {p.repositories.map((r) => (
                  <li key={r.id} className="text-sm">
                    <CopyPath path={r.path} />
                    <div className="mt-1 flex flex-wrap gap-1.5">
                      <Chip>{r.vcs === "git" ? "Git" : "senza versionamento"}</Chip>
                      {r.missing_since && <span className="text-xs text-danger">non trovato dal {formatDate(r.missing_since)}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title="Provenienza" subtitle="Ultima osservazione per ogni fonte.">
            <ul className="space-y-2 text-sm">
              {Object.values(p.evidence).map((e) => (
                <li key={e.id} className="flex items-baseline justify-between gap-2">
                  <span>{SOURCE_LABEL[e.source_type] ?? e.source_type}</span>
                  <span className="text-xs text-muted" title={`sha256 ${e.content_hash}`}>
                    {formatDate(e.observed_at)} · <span className="font-mono">{e.content_hash.slice(0, 8)}</span>
                  </span>
                </li>
              ))}
              {Object.keys(p.evidence).length === 0 && <li className="text-muted">Nessuna evidenza raccolta.</li>}
            </ul>
            <p className="mt-3 text-xs text-muted">{HEALTH_HINT[p.health]}.</p>
          </Panel>

          {canDelete && (
            <button className="btn w-full justify-center text-danger" onClick={remove}>
              Rimuovi dal registro
            </button>
          )}
        </aside>
      </div>
    </div>
  );
}

function GitPanel({ git }: { git: GitState }) {
  const dirty = git.dirty_files + git.untracked_files;
  return (
    <Panel title="Git">
      <dl className="grid grid-cols-[110px_1fr] gap-x-4 gap-y-2 text-sm">
        <dt className="text-muted">Branch</dt>
        <dd className="font-mono">{git.branch ?? "detached HEAD"}</dd>
        <dt className="text-muted">Ultimo commit</dt>
        <dd className="min-w-0">
          {git.head ? (
            <>
              <span className="font-mono text-muted">{git.head.slice(0, 7)}</span> {git.head_subject}
              <div className="text-xs text-muted">{formatDate(git.head_date)}</div>
            </>
          ) : (
            <span className="text-muted">nessun commit</span>
          )}
        </dd>
        <dt className="text-muted">Working tree</dt>
        <dd className={dirty ? "text-warn" : "text-accent"}>
          {dirty ? `${git.dirty_files} modificati, ${git.untracked_files} non tracciati` : "pulita"}
        </dd>
        {git.remote_url && (
          <>
            <dt className="text-muted">Remote</dt>
            <dd className="truncate font-mono text-xs leading-5">{git.remote_url}</dd>
          </>
        )}
      </dl>
    </Panel>
  );
}

function ReadmePanel({ project }: { project: ProjectDetail }) {
  const [open, setOpen] = useState(false);
  const payload = project.evidence.readme.payload as { file: string; excerpt: string };
  return (
    <Panel title={payload.file}>
      <pre className={`overflow-hidden whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-muted ${open ? "" : "max-h-48"}`}>
        {payload.excerpt}
      </pre>
      <button className="mt-2 text-xs text-accent hover:underline" onClick={() => setOpen(!open)}>
        {open ? "Comprimi" : "Mostra tutto"}
      </button>
    </Panel>
  );
}

function EditableTitle({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  if (draft === null) {
    return (
      <h1 className="group flex items-center gap-2 text-2xl font-semibold tracking-tight">
        {value}
        <button onClick={() => setDraft(value)} className="text-xs font-normal text-muted opacity-0 transition group-hover:opacity-100" aria-label="Rinomina">
          rinomina
        </button>
      </h1>
    );
  }
  const commit = () => {
    if (draft.trim() && draft.trim() !== value) onSave(draft.trim());
    setDraft(null);
  };
  return (
    <input
      autoFocus
      className="field text-2xl font-semibold"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") setDraft(null);
      }}
    />
  );
}

function DescriptionEditor({ value, placeholder, onSave }: { value: string | null; placeholder: string | null; onSave: (v: string | null) => void }) {
  const [draft, setDraft] = useState(value ?? "");
  const dirty = draft !== (value ?? "");
  return (
    <div>
      <textarea
        className="field min-h-20 resize-y"
        value={draft}
        placeholder={placeholder ? `Dal README: ${placeholder}` : "Cos'è questo progetto e a cosa serve?"}
        onChange={(e) => setDraft(e.target.value)}
      />
      {dirty && (
        <div className="mt-2 flex gap-2">
          <button className="btn btn-primary" onClick={() => onSave(draft.trim() || null)}>
            Salva
          </button>
          <button className="btn" onClick={() => setDraft(value ?? "")}>
            Annulla
          </button>
        </div>
      )}
    </div>
  );
}

function CopyPath({ path }: { path: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="w-full truncate text-left font-mono text-xs hover:text-accent"
      title="Copia percorso"
      onClick={() => {
        navigator.clipboard?.writeText(path).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
    >
      {copied ? "copiato ✓" : path}
    </button>
  );
}
