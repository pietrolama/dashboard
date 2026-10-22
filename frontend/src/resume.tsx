import { useEffect, useState } from "react";
import { api, type ResumeExport, type ResumeMode } from "./api";
import { ErrorBox } from "./components";

const FRESHNESS: Record<ResumeExport["manifest"]["resume"]["freshness"], { label: string; tone: string; hint: string }> = {
  current: { label: "Aggiornato", tone: "text-accent", hint: "Repository riletto adesso." },
  partial: { label: "Parziale", tone: "text-warn", hint: "Parte del repository non è leggibile: vedi le informazioni mancanti." },
  stale: { label: "Non aggiornato", tone: "text-danger", hint: "La directory del progetto non è stata trovata." },
  unknown: { label: "Sconosciuto", tone: "text-muted", hint: "Nessuna directory collegata: il briefing contiene solo la memoria dichiarata." },
};

/** POSIX single-quoting, so paths with spaces survive copy-paste into a shell. */
const sq = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;

function claudeCommand(out: ResumeExport): string {
  const prompt =
    `Leggi ${out.latest}/RESUME.md: è il briefing PRP di questo progetto (manifest.json ed evidence.json sono nella stessa cartella). ` +
    `Verifica lo stato del repository, rispetta vincoli e policy del briefing e riprendi dalla prossima azione.`;
  const root = out.manifest.project.root;
  return `${root ? `cd ${sq(root)} && ` : ""}claude ${sq(prompt)}`;
}

export function ResumeDialog({ slug, onClose, onExported }: { slug: string; onClose: () => void; onExported: () => void }) {
  const [mode, setMode] = useState<ResumeMode>("implementation_within_scope");
  const [out, setOut] = useState<ResumeExport | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      setOut(await api.exportResume(slug, mode));
      onExported();
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  function copy(label: string, text: string) {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(label);
      setTimeout(() => setCopied(null), 1500);
    });
  }

  const m = out?.manifest;
  const fresh = m ? FRESHNESS[m.resume.freshness] : null;

  return (
    <div className="fixed inset-0 z-20 flex items-start justify-center overflow-y-auto bg-black/50 p-4 sm:p-10" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-labelledby="resume-title" className="card w-full max-w-3xl p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="resume-title" className="text-lg font-semibold">
              Riprendi progetto
            </h2>
            <p className="mt-1 text-sm text-muted">
              Genera il briefing PRP: <code>RESUME.md</code>, <code>manifest.json</code>, <code>evidence.json</code>. Il repository viene riletto adesso e non viene modificato.
            </p>
          </div>
          <button className="text-muted hover:text-fg" onClick={onClose} aria-label="Chiudi">
            ✕
          </button>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <div className="flex rounded-md border border-line bg-surface p-0.5 text-xs">
            {(
              [
                ["implementation_within_scope", "Implementazione"],
                ["analysis_only", "Solo analisi"],
              ] as const
            ).map(([value, label]) => (
              <button key={value} onClick={() => setMode(value)} className={`rounded px-2.5 py-1 ${mode === value ? "bg-surface-2 text-fg" : "text-muted hover:text-fg"}`}>
                {label}
              </button>
            ))}
          </div>
          <span className="text-xs text-muted">
            {mode === "analysis_only" ? "L'agente non deve modificare file." : "L'agente può modificare file entro la prossima azione; niente commit, push o nuove dipendenze."}
          </span>
          <button className="btn btn-primary ml-auto" onClick={generate} disabled={busy}>
            {busy ? "Generazione…" : out ? "Rigenera" : "Genera briefing"}
          </button>
        </div>

        <div className="mt-3">
          <ErrorBox error={error} />
        </div>

        {out && m && fresh && (
          <div className="mt-4 space-y-4">
            <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
              <Metric label="Stato" value={<span className={fresh.tone}>{fresh.label}</span>} hint={fresh.hint} />
              <Metric label="Vincoli · decisioni" value={`${m.constraints.length} · ${m.decisions.filter((d) => d.status !== "superseded").length}`} />
              <Metric label="Domande · azioni" value={`${m.open_questions.length} · ${m.next_actions.length}`} />
              <Metric label="Evidenze" value={m.evidence.length} />
            </div>

            {m.state.unknowns.length > 0 && (
              <details className="rounded-md border border-line px-3 py-2 text-sm">
                <summary className="cursor-pointer text-muted">{m.state.unknowns.length} informazioni mancanti dichiarate nel briefing</summary>
                <ul className="mt-2 list-disc space-y-1 pl-5 text-muted">
                  {m.state.unknowns.map((u) => (
                    <li key={u.id}>{u.text}</li>
                  ))}
                </ul>
              </details>
            )}

            <div>
              <div className="mb-1 flex items-center justify-between text-xs text-muted">
                <span>Avvia Claude Code sul briefing</span>
                <button className="hover:text-fg" onClick={() => copy("cmd", claudeCommand(out))}>
                  {copied === "cmd" ? "copiato ✓" : "copia"}
                </button>
              </div>
              <pre className="overflow-x-auto rounded-md bg-surface-2 p-3 font-mono text-xs">{claudeCommand(out)}</pre>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button className="btn" onClick={() => copy("md", out.markdown)}>
                {copied === "md" ? "Copiato ✓" : "Copia RESUME.md"}
              </button>
              <a className="btn" href={api.resumeZipUrl(slug, out.brief_id)}>
                Scarica bundle .zip
              </a>
              <button className="btn" onClick={() => copy("path", out.path)} title={out.path}>
                {copied === "path" ? "Copiato ✓" : "Copia percorso"}
              </button>
              <span className="truncate font-mono text-xs text-muted" title={out.path}>
                {out.brief_id}
              </span>
            </div>

            <pre className="max-h-[50vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-line p-3 font-mono text-xs leading-relaxed">{out.markdown}</pre>
          </div>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="rounded-md border border-line px-3 py-2" title={hint}>
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-0.5 font-medium tabular-nums">{value}</div>
    </div>
  );
}
