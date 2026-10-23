import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type ProjectDetail, type TeamAgent, type TeamRun, type TeamRunStatus, type TeamTask, type TeamTaskDraft } from "./api";
import { Chip, Empty, ErrorBox, Panel } from "./components";
import { relativeTime, useAsync } from "./util";

const AGENT_LABEL: Record<TeamAgent, string> = { claude: "Claude Code", agy: "Antigravity" };
const AGENT_TONE: Record<TeamAgent, string> = { claude: "text-warn", agy: "text-info" };
const ACTIVE: TeamRunStatus[] = ["planning", "plan_ready", "running", "integrating", "conflict", "review", "failed"];
// While the run is in these states something else (an agent, the planner) changes it.
const LIVE: TeamRunStatus[] = ["planning", "running", "integrating"];

const STATUS_LABEL: Record<TeamRunStatus, string> = {
  planning: "Pianificazione",
  plan_ready: "Piano da approvare",
  running: "Al lavoro",
  integrating: "Integrazione",
  conflict: "Conflitto",
  review: "Da rivedere",
  applied: "Applicato",
  discarded: "Scartato",
  failed: "Non riuscito",
};

const TASK_STATUS: Record<TeamTask["status"], { label: string; tone: string }> = {
  todo: { label: "da fare", tone: "text-muted" },
  doing: { label: "in corso", tone: "text-info" },
  done: { label: "fatto", tone: "text-accent" },
  blocked: { label: "bloccato", tone: "text-danger" },
};

/** Runs an action on the run, shows its error and replaces the run with the response. */
function useRunAction(onRun: (r: TeamRun) => void) {
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<TeamRun | void>) {
    setError(null);
    setBusy(true);
    try {
      const r = await action();
      if (r) onRun(r);
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }
  return { error, busy, run };
}

export function TeamPanel({ project, onChange }: { project: ProjectDetail; onChange: () => void }) {
  const { data: runs, reload } = useAsync(() => api.teamRuns(project.slug), [project.slug]);
  const active = runs?.find((r) => ACTIVE.includes(r.status));
  const past = runs?.filter((r) => r !== active) ?? [];
  const hasGit = project.repositories.some((r) => r.vcs === "git" && !r.missing_since);

  // Stable, because ActiveRun restarts its polling when it changes.
  const refresh = useCallback(() => {
    reload();
    onChange();
  }, [reload, onChange]);

  return (
    <Panel
      title="Team · Claude Code + Antigravity"
      subtitle="Descrivi il lavoro: Claude lo divide, tu approvi il piano, i due agenti lavorano in terminali separati su branch separati."
    >
      {!hasGit ? (
        <p className="text-sm text-muted">Serve un repository Git sul disco.</p>
      ) : active ? (
        <ActiveRun key={active.id} runId={active.id} onClosed={refresh} />
      ) : (
        <NewRun slug={project.slug} dirty={(project.git?.dirty_files ?? 0) > 0} onCreated={refresh} />
      )}
      {past.length > 0 && (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer text-xs text-muted">Run precedenti ({past.length})</summary>
          <ul className="mt-2 space-y-1">
            {past.map((r) => (
              <li key={r.id} className="flex items-baseline gap-2">
                <span className="font-mono text-xs text-muted">#{r.id}</span>
                <span className="min-w-0 flex-1 truncate">{r.request}</span>
                <Chip>{STATUS_LABEL[r.status]}</Chip>
                <span className="text-xs text-muted">{relativeTime(r.updated_at)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Panel>
  );
}

function NewRun({ slug, dirty, onCreated }: { slug: string; dirty: boolean; onCreated: () => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createTeamRun(slug, text);
      setText("");
      onCreated();
    } catch (err) {
      setError(err as Error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-2">
      <textarea
        className="field min-h-24 resize-y"
        placeholder="Cosa deve fare il team? Es. «Aggiungi l'esportazione in CSV con il relativo endpoint e un pulsante nella pagina»"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      {dirty && (
        <p className="text-xs text-warn">
          Ci sono modifiche non committate: gli agenti partono dall'ultimo commit e non le vedranno.
        </p>
      )}
      <div className="flex items-center gap-2">
        <button className="btn btn-primary" disabled={busy || !text.trim()}>
          {busy ? "Invio…" : "Pianifica"}
        </button>
        <span className="text-xs text-muted">Claude legge il progetto senza modificarlo e propone chi fa cosa.</span>
      </div>
      <ErrorBox error={error} />
    </form>
  );
}

function ActiveRun({ runId, onClosed }: { runId: number; onClosed: () => void }) {
  const [run, setRun] = useState<TeamRun | null>(null);
  const [loadError, setLoadError] = useState<Error | null>(null);
  const { error, busy, run: act } = useRunAction((r) => {
    setRun(r);
    if (!ACTIVE.includes(r.status)) onClosed();
  });

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const r = await api.teamRun(runId);
        if (cancelled) return;
        setRun(r);
        setLoadError(null);
        if (!ACTIVE.includes(r.status)) return onClosed();
        timer = setTimeout(poll, LIVE.includes(r.status) ? 2000 : 8000);
      } catch (e) {
        if (!cancelled) {
          setLoadError(e as Error);
          timer = setTimeout(poll, 5000);
        }
      }
    };
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [runId, onClosed]);

  if (!run) return loadError ? <ErrorBox error={loadError} /> : <Empty>Caricamento…</Empty>;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-2">
        <span className="font-mono text-xs text-muted">#{run.id}</span>
        <p className="min-w-0 flex-1 whitespace-pre-wrap text-sm">{run.request}</p>
        <Chip>{STATUS_LABEL[run.status]}</Chip>
      </div>

      {run.error && <div className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-sm text-danger">{run.error}</div>}

      {run.status === "planning" && <Working>Claude sta leggendo il progetto e dividendo il lavoro…</Working>}
      {run.status === "plan_ready" && <PlanEditor run={run} busy={busy} act={act} />}
      {(run.status === "running" || run.status === "integrating" || run.status === "conflict" || run.status === "review") && (
        <Board run={run} />
      )}
      {run.status === "running" && <RunningControls run={run} busy={busy} act={act} />}
      {run.status === "integrating" && <Working>Unione dei branch e revisione di Claude in corso…</Working>}
      {run.status === "conflict" && (
        <div className="space-y-2 text-sm">
          <p>
            Risolvi il conflitto nel worktree di integrazione, fai commit, poi riprova:
          </p>
          <pre className="overflow-x-auto rounded-md bg-surface-2 p-2 font-mono text-xs">{run.integration_worktree}</pre>
          <button className="btn btn-primary" disabled={busy} onClick={() => act(() => api.teamAction(run.id, "retry"))}>
            Riprova integrazione
          </button>
        </div>
      )}
      {run.status === "review" && <Review run={run} busy={busy} act={act} />}
      {run.status === "failed" && (
        <button className="btn btn-primary" disabled={busy} onClick={() => act(() => api.replanTeamRun(run.id))}>
          Riprova la pianificazione
        </button>
      )}

      {run.messages.length > 0 && <Activity run={run} />}

      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <DiscardButton busy={busy} onConfirm={() => act(() => api.teamAction(run.id, "discard"))} />
        <span className="text-xs text-muted">Chiude i terminali e cancella worktree e branch del run. Il progetto non viene toccato.</span>
      </div>
      <ErrorBox error={error} />
    </div>
  );
}

function Working({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted">
      <span className="size-2 animate-pulse rounded-full bg-accent" />
      {children}
    </div>
  );
}

type Act = (action: () => Promise<TeamRun | void>) => Promise<void>;

function PlanEditor({ run, busy, act }: { run: TeamRun; busy: boolean; act: Act }) {
  const fromRun = () =>
    run.tasks.map<TeamTaskDraft>((t) => ({ ref: t.number, agent: t.agent, title: t.title, details: t.details, depends_on: t.depends_on }));
  const [tasks, setTasks] = useState<TeamTaskDraft[]>(fromRun);
  const dirty = JSON.stringify(tasks) !== JSON.stringify(fromRun());
  const label = (ref: number) => tasks.findIndex((t) => t.ref === ref) + 1;

  const edit = (i: number, patch: Partial<TeamTaskDraft>) => setTasks(tasks.map((t, j) => (j === i ? { ...t, ...patch } : t)));
  const remove = (i: number) => setTasks(tasks.filter((_, j) => j !== i));
  const add = () =>
    setTasks([...tasks, { ref: Math.max(0, ...tasks.map((t) => t.ref)) + 1, agent: "claude", title: "", details: null, depends_on: [] }]);

  const save = () => api.updateTeamPlan(run.id, tasks);
  const valid = tasks.length > 0 && tasks.every((t) => t.title.trim());

  return (
    <div className="space-y-3">
      {run.plan_summary && <p className="text-sm text-muted">{run.plan_summary}</p>}
      <ol className="space-y-2">
        {tasks.map((t, i) => (
          <li key={t.ref} className="rounded-md border border-line p-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs text-muted">#{i + 1}</span>
              <select
                className={`field w-auto font-medium ${AGENT_TONE[t.agent]}`}
                value={t.agent}
                onChange={(e) => edit(i, { agent: e.target.value as TeamAgent })}
              >
                {(Object.keys(AGENT_LABEL) as TeamAgent[]).map((a) => (
                  <option key={a} value={a}>
                    {AGENT_LABEL[a]}
                  </option>
                ))}
              </select>
              <input className="field flex-1" value={t.title} onChange={(e) => edit(i, { title: e.target.value })} />
              <button type="button" className="text-xs text-muted hover:text-danger" onClick={() => remove(i)} aria-label="Rimuovi attività">
                ✕
              </button>
            </div>
            <textarea
              className="field mt-2 min-h-28 resize-y text-xs"
              value={t.details ?? ""}
              placeholder="Istruzioni per l'agente"
              onChange={(e) => edit(i, { details: e.target.value || null })}
            />
            {t.depends_on.some((d) => label(d) > 0) && (
              <div className="mt-1 text-xs text-muted">
                Dipende da {t.depends_on.filter((d) => label(d) > 0).map((d) => `#${label(d)}`).join(", ")}
              </div>
            )}
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap items-center gap-2">
        <button
          className="btn btn-primary"
          disabled={busy || !valid}
          onClick={() =>
            act(async () => {
              if (dirty) await save();
              return api.teamAction(run.id, "start");
            })
          }
        >
          Avvia team
        </button>
        <button className="btn" type="button" onClick={add}>
          + Attività
        </button>
        {dirty && (
          <>
            <button className="btn" disabled={busy || !valid} onClick={() => act(save)}>
              Salva piano
            </button>
            <button className="btn" onClick={() => setTasks(fromRun())}>
              Annulla modifiche
            </button>
          </>
        )}
        <button className="btn ml-auto" disabled={busy} onClick={() => act(() => api.replanTeamRun(run.id))}>
          Rigenera piano
        </button>
      </div>
      <p className="text-xs text-muted">
        All'avvio si apre un terminale con le due CLI affiancate. Possono modificare i file del proprio worktree senza chiedere; per i comandi
        shell ti chiedono conferma lì. Antigravity ti chiederà anche di autorizzare la cartella del worktree: premi Invio.
      </p>
      {run.claude_trusted === false && tasks.some((t) => t.agent === "claude") && (
        <p className="text-xs text-warn">
          Claude Code non ha ancora autorizzato questo progetto: nel suo terminale scegli «Yes, I trust this folder» (l'opzione predefinita è
          «No, exit»). Basta una volta: i run successivi la ereditano.
        </p>
      )}
    </div>
  );
}

function Board({ run }: { run: TeamRun }) {
  const agents = (Object.keys(AGENT_LABEL) as TeamAgent[]).filter((a) => run.tasks.some((t) => t.agent === a));
  return (
    <div className={`grid gap-3 ${agents.length > 1 ? "sm:grid-cols-2" : ""}`}>
      {agents.map((agent) => {
        const member = run.members.find((m) => m.agent === agent);
        return (
          <div key={agent} className="rounded-md border border-line p-3">
            <div className="flex items-baseline justify-between gap-2">
              <span className={`text-sm font-medium ${AGENT_TONE[agent]}`}>{AGENT_LABEL[agent]}</span>
              {member &&
                (member.waiting ? (
                  <span className="text-xs font-medium text-warn">aspetta te nel terminale</span>
                ) : (
                  <span className="text-xs text-muted">{member.status === "done" ? "ha finito" : "al lavoro"}</span>
                ))}
            </div>
            {member && <div className="truncate font-mono text-[11px] text-muted" title={member.worktree}>{member.branch}</div>}
            <ul className="mt-2 space-y-2">
              {run.tasks
                .filter((t) => t.agent === agent)
                .map((t) => (
                  <li key={t.id} className="text-sm">
                    <div className="flex items-baseline gap-2">
                      <span className="font-mono text-xs text-muted">#{t.number}</span>
                      <span className="min-w-0 flex-1">{t.title}</span>
                      <span className={`text-xs ${TASK_STATUS[t.status].tone}`}>{TASK_STATUS[t.status].label}</span>
                    </div>
                    {t.summary && <p className="ml-6 text-xs text-muted">{t.summary}</p>}
                  </li>
                ))}
            </ul>
            {member?.summary && <p className="mt-2 border-t border-line pt-2 text-xs">{member.summary}</p>}
          </div>
        );
      })}
    </div>
  );
}

function RunningControls({ run, busy, act }: { run: TeamRun; busy: boolean; act: Act }) {
  const agents = run.members.map((m) => m.agent);
  const [to, setTo] = useState<TeamAgent>(agents[0]);
  const [text, setText] = useState("");
  const [note, setNote] = useState<string | null>(null);

  async function send(e: FormEvent) {
    e.preventDefault();
    await act(async () => {
      const res = await api.sendTeamMessage(run.id, to, text);
      setText("");
      setNote(res.delivered ? null : "Messaggio salvato, ma il terminale dell'agente non risponde.");
      return api.teamRun(run.id);
    });
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {run.session_alive ? (
          <button className="btn" disabled={busy} onClick={() => act(async () => void (await api.openTeamTerminal(run.id)))}>
            Apri terminali
          </button>
        ) : (
          <span className="text-xs text-danger">La sessione dei terminali non è più attiva.</span>
        )}
        {run.attach_command && <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs">{run.attach_command}</code>}
        <button className="btn ml-auto" disabled={busy} onClick={() => act(() => api.teamAction(run.id, "finish"))}>
          Integra ora
        </button>
      </div>
      <form onSubmit={send} className="flex gap-2">
        <select className="field w-auto" value={to} onChange={(e) => setTo(e.target.value as TeamAgent)}>
          {agents.map((a) => (
            <option key={a} value={a}>
              a {AGENT_LABEL[a]}
            </option>
          ))}
        </select>
        <input className="field flex-1" placeholder="Scrivi a un agente: arriva nel suo terminale" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="btn" disabled={busy || !text.trim()}>
          Invia
        </button>
      </form>
      {note && <p className="text-xs text-warn">{note}</p>}
      <p className="text-xs text-muted">
        Quando entrambi gli agenti dichiarano finito, Project OS unisce i branch e Claude rivede il risultato. «Integra ora» lo fa subito con
        quello che c'è.
      </p>
    </div>
  );
}

function Review({ run, busy, act }: { run: TeamRun; busy: boolean; act: Act }) {
  return (
    <div className="space-y-3">
      {run.diffstat && <pre className="overflow-x-auto rounded-md bg-surface-2 p-3 font-mono text-xs">{run.diffstat}</pre>}
      {run.review && (
        <div>
          <div className="mb-1 text-xs text-muted">Revisione di Claude</div>
          <div className="whitespace-pre-wrap rounded-md border border-line p-3 text-sm">{run.review}</div>
        </div>
      )}
      <p className="text-xs text-muted">
        Il risultato è sul branch <code>team/{run.id}</code>, nel worktree <code>{run.integration_worktree}</code>: puoi provarlo lì prima di
        applicarlo.
      </p>
      <button className="btn btn-primary" disabled={busy || !run.diffstat} onClick={() => act(() => api.teamAction(run.id, "apply"))}>
        Applica a {run.base_branch ?? "branch"}
      </button>
    </div>
  );
}

function Activity({ run }: { run: TeamRun }) {
  const who = (s: string) => (s === "user" ? "Tu" : s === "team" ? "team" : AGENT_LABEL[s as TeamAgent] ?? s);
  return (
    <details className="text-sm" open={run.status === "running"}>
      <summary className="cursor-pointer text-xs text-muted">Attività e messaggi ({run.messages.length})</summary>
      <ol className="mt-2 max-h-72 space-y-1.5 overflow-y-auto">
        {[...run.messages].reverse().map((m) => (
          <li key={m.id} className="text-xs">
            <span className={`font-medium ${m.sender in AGENT_LABEL ? AGENT_TONE[m.sender as TeamAgent] : ""}`}>{who(m.sender)}</span>
            {m.recipient !== "team" && <span className="text-muted"> → {who(m.recipient)}</span>}
            <span className="text-muted"> · {relativeTime(m.created_at)}</span>
            <div className="whitespace-pre-wrap">{m.body}</div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function DiscardButton({ busy, onConfirm }: { busy: boolean; onConfirm: () => void }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return armed ? (
    <button className="btn text-danger" disabled={busy} onClick={onConfirm}>
      Conferma: scarta il run
    </button>
  ) : (
    <button className="btn" disabled={busy} onClick={() => setArmed(true)}>
      Scarta
    </button>
  );
}
