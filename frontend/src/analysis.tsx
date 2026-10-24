import { useEffect, useState } from "react";
import { api, type Analysis, type AnalysisStep, type Finding, type ProjectDetail } from "./api";
import { Chip, ErrorBox, Panel } from "./components";
import { relativeTime, useAsync } from "./util";

const LIVE = ["queued", "running"];
const AGENT = { claude: "Claude", agy: "Antigravity" } as const;
const ROLE = { extract: "lettura", verify: "verifica" } as const;

/** Agy reads what is declared, claude checks it in the code; nothing changes until the user accepts. */
export function AnalysisPanel({ project, onChange }: { project: ProjectDetail; onChange: () => void }) {
  const { data: analysis, error, setData, reload } = useAsync(() => api.analysis(project.slug), [project.slug]);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<Error | null>(null);
  const live = !!analysis && LIVE.includes(analysis.status);
  const hasFolder = project.repositories.some((r) => !r.missing_since);

  // Poll while the agents work; the project page refreshes once they are done.
  useEffect(() => {
    if (!live) return;
    const t = setTimeout(async () => {
      try {
        const next = await api.getAnalysis(analysis!.id);
        if (LIVE.includes(next.status)) setData({ ...analysis!, ...next });
        else {
          reload();
          onChange();
        }
      } catch {
        reload();
      }
    }, 3000);
    return () => clearTimeout(t);
  }, [analysis, live, setData, reload, onChange]);

  async function start(force: boolean) {
    setBusy(true);
    setActionError(null);
    try {
      setData(await api.requestAnalysis(project.slug, force));
    } catch (e) {
      setActionError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  async function resolve(f: Finding, accept: boolean) {
    setActionError(null);
    try {
      const updated = await api.resolveFinding(f.id, accept);
      setData({ ...analysis!, findings: analysis!.findings.map((x) => (x.id === f.id ? updated : x)) });
      if (accept && f.action) onChange();
    } catch (e) {
      setActionError(e as Error);
    }
  }

  const done = analysis?.status === "done";
  const button =
    !hasFolder || live ? null : !analysis || analysis.status === "failed" || !analysis.current ? (
      <button className="btn py-1 text-xs" onClick={() => start(false)} disabled={busy}>
        {analysis ? "Analizza di nuovo" : "Analizza"}
      </button>
    ) : (
      <button className="text-xs text-muted hover:text-fg" onClick={() => start(true)} disabled={busy} title="Niente è cambiato dall'ultima analisi">
        Rianalizza comunque
      </button>
    );

  return (
    <Panel
      title="Analisi AI"
      subtitle="Antigravity legge ciò che è dichiarato, Claude lo verifica nel codice. Lo stato dichiarato cambia solo se accetti una proposta."
      action={button}
    >
      <div className="space-y-4">
        {!hasFolder && <p className="text-sm text-muted">Serve la cartella del progetto sul disco.</p>}
        {hasFolder && !analysis && !error && <p className="text-sm text-muted">Nessuna analisi. Costa token: parte solo quando premi Analizza.</p>}
        {analysis && live && <Progress analysis={analysis} />}
        {analysis?.status === "failed" && <ErrorBox error={new Error(`Analisi #${analysis.id} non riuscita: ${analysis.error ?? "errore sconosciuto"}`)} />}
        {analysis && done && <Result analysis={analysis} onResolve={resolve} />}
        {analysis && !live && analysis.steps.length > 0 && <Steps steps={analysis.steps} />}
        <ErrorBox error={actionError ?? error} />
      </div>
    </Panel>
  );
}

function Progress({ analysis }: { analysis: Analysis }) {
  const step = analysis.steps.at(-1);
  const text =
    analysis.status === "queued" || !step
      ? "In coda…"
      : step.role === "extract"
        ? `${AGENT[step.agent]} legge README, documentazione e stato dichiarato…`
        : `${AGENT[step.agent]} verifica le affermazioni nel codice…`;
  return (
    <div className="flex items-center gap-2 text-sm text-muted">
      <span className="size-2 animate-pulse rounded-full bg-accent" />
      {text}
    </div>
  );
}

function Result({ analysis, onResolve }: { analysis: Analysis; onResolve: (f: Finding, accept: boolean) => void }) {
  const of = (kind: Finding["kind"]) => analysis.findings.filter((f) => f.kind === kind);
  const contradictions = of("contradiction");
  const suggestions = of("suggestion");
  const checked = [...of("confirmed"), ...of("unverifiable")];
  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <span>
          Analisi #{analysis.id} · {relativeTime(analysis.finished_at ?? analysis.created_at)}
        </span>
        {analysis.head_commit && <Chip mono>{analysis.head_commit.slice(0, 9)}</Chip>}
        {analysis.current === false && (
          <span className="rounded-full bg-warn-soft px-2 py-0.5 font-medium text-warn" title="Codice o stato dichiarato sono cambiati dopo questa analisi">
            superata
          </span>
        )}
      </div>
      {analysis.summary && <p className="text-sm">{analysis.summary}</p>}

      <Section title="Contraddizioni" empty="Nessuna: quello che è dichiarato torna con il codice." items={contradictions}>
        {(f) => <FindingRow key={f.id} finding={f} accept="Risolta" onResolve={onResolve} />}
      </Section>

      {suggestions.length > 0 && (
        <Section title="Proposte" items={suggestions}>
          {(f) => <FindingRow key={f.id} finding={f} accept="Accetta" onResolve={onResolve} />}
        </Section>
      )}

      {checked.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-xs text-muted">
            {of("confirmed").length} confermate · {of("unverifiable").length} non verificabili
          </summary>
          <ul className="mt-2 space-y-2">
            {checked.map((f) => (
              <li key={f.id} className="flex gap-2">
                <span className={f.kind === "confirmed" ? "text-accent" : "text-muted"} aria-label={f.kind === "confirmed" ? "confermata" : "non verificabile"}>
                  {f.kind === "confirmed" ? "✓" : "?"}
                </span>
                <div className="min-w-0">
                  <div>{f.text}</div>
                  <Meta finding={f} />
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

function Section({ title, empty, items, children }: { title: string; empty?: string; items: Finding[]; children: (f: Finding) => React.ReactNode }) {
  const open = items.filter((f) => f.status === "open").length;
  return (
    <div>
      <div className="text-xs text-muted">
        {title}
        {items.length > 0 && ` (${open} aperte su ${items.length})`}
      </div>
      {items.length === 0 ? <p className="mt-1 text-sm text-muted">{empty}</p> : <ul className="mt-2 space-y-3">{items.map(children)}</ul>}
    </div>
  );
}

function FindingRow({ finding: f, accept, onResolve }: { finding: Finding; accept: string; onResolve: (f: Finding, accept: boolean) => void }) {
  const [busy, setBusy] = useState(false);
  const act = async (yes: boolean) => {
    setBusy(true);
    await onResolve(f, yes);
    setBusy(false);
  };
  const resolved = f.status !== "open";
  return (
    <li className={`flex items-start gap-3 text-sm ${resolved ? "opacity-60" : ""}`}>
      <div className="min-w-0 flex-1">
        <div className="font-medium">{f.text}</div>
        {f.detail && <div className="mt-0.5 text-muted">{f.detail}</div>}
        <Meta finding={f} />
      </div>
      {resolved ? (
        <span className="shrink-0 text-xs text-muted">{f.status === "accepted" ? (f.kind === "suggestion" ? "accettata" : "risolta") : "ignorata"}</span>
      ) : (
        <div className="flex shrink-0 gap-1">
          <button className="btn py-1 text-xs" onClick={() => act(true)} disabled={busy}>
            {accept}
          </button>
          <button className="rounded-md px-2 py-1 text-xs text-muted hover:text-fg" onClick={() => act(false)} disabled={busy}>
            Ignora
          </button>
        </div>
      )}
    </li>
  );
}

function Meta({ finding: f }: { finding: Finding }) {
  if (!f.source && f.refs.length === 0) return null;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-muted">
      {f.source && <span>da {f.source}</span>}
      {f.refs.map((r) => (
        <Chip key={r} mono>
          {r}
        </Chip>
      ))}
    </div>
  );
}

function Steps({ steps }: { steps: AnalysisStep[] }) {
  const cost = steps.reduce((t, s) => t + (s.cost_usd ?? 0), 0);
  return (
    <div className="border-t border-line pt-2 text-xs text-muted">
      {steps.map((s, i) => (
        <span key={i}>
          {i > 0 && " · "}
          {AGENT[s.agent]} {ROLE[s.role]}
          {s.status !== "success" ? (s.fallback ? " non riuscita, ripresa da Claude" : ` ${s.status}`) : ""}
          {s.duration_s != null && s.status === "success" && ` ${formatSeconds(s.duration_s)}`}
          {s.tokens && s.status === "success" && tokenCount(s.tokens) > 0 && `, ${formatTokens(tokenCount(s.tokens))} token`}
        </span>
      ))}
      {cost > 0 && ` · $${cost.toFixed(2)}`}
    </div>
  );
}

const tokenCount = (t: { input: number | null; output: number | null }) => (t.input ?? 0) + (t.output ?? 0);
const formatTokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${n}`);
const formatSeconds = (s: number) => (s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
