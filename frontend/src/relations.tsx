import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type LinkKind, type ObservedKind, type ProjectDetail, type Relation } from "./api";
import { ErrorBox, Field, Panel } from "./components";
import { LINK_LABEL, LINK_LABEL_REVERSE, useAsync } from "./util";

export const LINK_KINDS: LinkKind[] = ["depends_on", "part_of", "evolves_from", "related"];

/** Stroke color of each relation kind; observed relations are also dashed. */
export const KIND_COLOR: Record<LinkKind | ObservedKind, string> = {
  depends_on: "var(--info)",
  part_of: "var(--accent)",
  evolves_from: "var(--warn)",
  related: "var(--fg)",
  same_remote: "var(--muted)",
  contains: "var(--muted)",
};

export function KindSwatch({ kind }: { kind: LinkKind | ObservedKind }) {
  const observed = kind === "same_remote" || kind === "contains";
  return (
    <svg viewBox="0 0 24 8" className="h-2 w-6 shrink-0" aria-hidden>
      <line x1="1" y1="4" x2="23" y2="4" stroke={KIND_COLOR[kind]} strokeWidth="2" strokeDasharray={observed ? "3 3" : undefined} strokeOpacity={kind === "related" ? 0.55 : 1} />
    </svg>
  );
}

/** Relations of one project, phrased from its side. Declared ones can be removed. */
export function RelationList({ slug, relations, onChange, onSelect }: {
  slug: string;
  relations: Relation[];
  onChange: () => void;
  /** Called instead of following the link to the other project's page. */
  onSelect?: (slug: string) => void;
}) {
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<Error | null>(null);

  async function remove(id: number) {
    setBusy(id);
    setError(null);
    try {
      await api.removeLink(id);
      onChange();
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(null);
    }
  }

  if (relations.length === 0) return <p className="text-sm text-muted">Nessuna relazione.</p>;
  return (
    <div className="space-y-2">
      <ul className="space-y-2">
        {relations.map((r) => {
          const outgoing = r.source === slug;
          const other = outgoing ? { slug: r.target, name: r.target_name } : { slug: r.source, name: r.source_name };
          return (
            <li key={`${r.origin}-${r.id ?? `${r.kind}-${r.source}-${r.target}`}`} className="group flex items-start gap-2 text-sm">
              <span className="mt-1.5">
                <KindSwatch kind={r.kind} />
              </span>
              <div className="min-w-0 flex-1">
                <span className="text-muted">{(outgoing ? LINK_LABEL : LINK_LABEL_REVERSE)[r.kind]} </span>
                {onSelect ? (
                  <button className="font-medium hover:text-accent" onClick={() => onSelect(other.slug)}>
                    {other.name}
                  </button>
                ) : (
                  <Link to={`/p/${other.slug}`} className="font-medium hover:text-accent">
                    {other.name}
                  </Link>
                )}
                {r.origin === "observed" && <span className="ml-1.5 text-xs text-muted">· osservata</span>}
                {r.note && <div className="truncate text-xs text-muted" title={r.note}>{r.note}</div>}
              </div>
              {r.id !== null && (
                <button
                  className="text-xs text-muted opacity-0 transition hover:text-danger focus:opacity-100 group-hover:opacity-100 disabled:opacity-50"
                  onClick={() => remove(r.id!)}
                  disabled={busy === r.id}
                  aria-label={`Rimuovi relazione con ${other.name}`}
                >
                  rimuovi
                </button>
              )}
            </li>
          );
        })}
      </ul>
      <ErrorBox error={error} />
    </div>
  );
}

/** Declare a relation from `slug` to another project. */
export function LinkForm({ slug, onAdded }: { slug: string; onAdded: () => void }) {
  const projects = useAsync(() => api.projects(), []);
  const [target, setTarget] = useState("");
  const [kind, setKind] = useState<LinkKind>("depends_on");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!target) return;
    setSaving(true);
    setError(null);
    try {
      await api.addLink(slug, { target, kind, note: note.trim() || null });
      setTarget("");
      setNote("");
      onAdded();
    } catch (err) {
      setError(err as Error);
    } finally {
      setSaving(false);
    }
  }

  const others = (projects.data ?? []).filter((p) => p.slug !== slug).sort((a, b) => a.name.localeCompare(b.name));
  return (
    <form onSubmit={submit} className="space-y-2">
      <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-2">
        <Field label="Questo progetto">
          <select className="field" value={kind} onChange={(e) => setKind(e.target.value as LinkKind)}>
            {LINK_KINDS.map((k) => (
              <option key={k} value={k}>
                {LINK_LABEL[k]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Progetto">
          <select className="field" value={target} onChange={(e) => setTarget(e.target.value)} disabled={!projects.data}>
            <option value="">Scegli…</option>
            {others.map((p) => (
              <option key={p.slug} value={p.slug}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <input className="field" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Nota (facoltativa): perché sono collegati" maxLength={2000} />
      <button className="btn btn-primary" disabled={!target || saving}>
        Aggiungi relazione
      </button>
      <ErrorBox error={error ?? projects.error} />
    </form>
  );
}

export function RelationsPanel({ project, onChange }: { project: ProjectDetail; onChange: () => void }) {
  const [adding, setAdding] = useState(false);
  return (
    <Panel
      title="Relazioni"
      subtitle="Dichiarate da te o da un agente; quelle osservate vengono dai repository."
      action={
        <Link to={`/map?focus=${encodeURIComponent(project.slug)}`} className="shrink-0 text-xs text-accent hover:underline">
          Mappa
        </Link>
      }
    >
      <div className="space-y-3">
        <RelationList slug={project.slug} relations={project.relations} onChange={onChange} />
        {adding ? (
          <LinkForm slug={project.slug} onAdded={() => { setAdding(false); onChange(); }} />
        ) : (
          <button className="text-xs text-accent hover:underline" onClick={() => setAdding(true)}>
            + Aggiungi relazione
          </button>
        )}
      </div>
    </Panel>
  );
}
