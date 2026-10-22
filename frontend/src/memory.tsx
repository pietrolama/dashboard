import { useState, type FormEvent } from "react";
import { api, type Decision, type DecisionKind, type ProjectDetail, type SessionNotes, type WorkSession } from "./api";
import { Chip, ErrorBox, Field, Panel } from "./components";
import { formatDate, relativeTime } from "./util";

type Props = { project: ProjectDetail; onChange: () => void };

/** Runs an API action, reports its error inline and refreshes the project. */
function useAction(onChange: () => void) {
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<unknown>): Promise<boolean> {
    setError(null);
    setBusy(true);
    try {
      await action();
      onChange();
      return true;
    } catch (e) {
      setError(e as Error);
      return false;
    } finally {
      setBusy(false);
    }
  }
  return { error, busy, run };
}

const lines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);

// ------------------------------------------------------------ where you left off

export function ResumePanel({ project, onChange }: Props) {
  const m = project.memory;
  const [mode, setMode] = useState<"idle" | "start" | "log">("idle");
  const { error, busy, run } = useAction(onChange);
  const [goal, setGoal] = useState("");

  if (m.open_session) {
    return (
      <Panel title="Sessione in corso" subtitle={`Iniziata ${relativeTime(m.open_session.started_at)}`}>
        <p className="mb-4 text-base">{m.open_session.goal}</p>
        <SessionNotesForm
          submitLabel="Chiudi sessione"
          busy={busy}
          initialQuestions={m.open_questions}
          onSubmit={(notes) => run(() => api.endSession(m.open_session!.id, notes))}
        />
        <ErrorBox error={error} />
      </Panel>
    );
  }

  async function start(e: FormEvent) {
    e.preventDefault();
    if (await run(() => api.startSession(project.slug, goal))) {
      setGoal("");
      setMode("idle");
    }
  }

  return (
    <Panel
      title="Dove eri rimasto"
      subtitle={m.last_session ? `Ultima sessione ${relativeTime(m.last_session.ended_at)}: ${m.last_session.goal}` : "Nessuna sessione registrata."}
    >
      <div className="space-y-4">
        <div>
          <div className="text-xs text-muted">Prossimo passo</div>
          <p className={`mt-1 ${m.next_step ? "text-base" : "text-sm text-muted"}`}>{m.next_step ?? "Non ancora definito. Chiudi una sessione indicando il prossimo passo."}</p>
        </div>
        {m.open_questions.length > 0 && (
          <div>
            <div className="text-xs text-muted">Domande aperte</div>
            <ul className="mt-1 space-y-1 text-sm">
              {m.open_questions.map((q, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-warn">?</span>
                  {q}
                </li>
              ))}
            </ul>
          </div>
        )}

        {mode === "idle" && (
          <div className="flex flex-wrap gap-2">
            <button className="btn btn-primary" onClick={() => setMode("start")}>
              Inizia sessione
            </button>
            <button className="btn" onClick={() => setMode("log")}>
              Registra sessione conclusa
            </button>
          </div>
        )}
        {mode === "start" && (
          <form onSubmit={start} className="flex flex-wrap gap-2">
            <input autoFocus className="field flex-1" placeholder="Obiettivo di questa sessione" value={goal} onChange={(e) => setGoal(e.target.value)} required />
            <button className="btn btn-primary" disabled={busy}>
              Inizia
            </button>
            <button type="button" className="btn" onClick={() => setMode("idle")}>
              Annulla
            </button>
          </form>
        )}
        {mode === "log" && (
          <SessionNotesForm
            withGoal
            submitLabel="Registra"
            busy={busy}
            initialQuestions={m.open_questions}
            onCancel={() => setMode("idle")}
            onSubmit={async (notes, g) => {
              if (await run(() => api.logSession(project.slug, { ...notes, goal: g! }))) setMode("idle");
            }}
          />
        )}
        <ErrorBox error={error} />
      </div>
    </Panel>
  );
}

function SessionNotesForm({
  withGoal,
  submitLabel,
  busy,
  initialQuestions,
  onSubmit,
  onCancel,
}: {
  withGoal?: boolean;
  submitLabel: string;
  busy: boolean;
  initialQuestions: string[];
  onSubmit: (notes: SessionNotes, goal?: string) => void;
  onCancel?: () => void;
}) {
  const [goal, setGoal] = useState("");
  const [outcome, setOutcome] = useState("");
  // Carry unresolved questions forward; the user deletes the ones answered.
  const [questions, setQuestions] = useState(initialQuestions.join("\n"));
  const [next, setNext] = useState("");

  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ outcome: outcome || null, open_questions: lines(questions), next_step: next || null }, goal);
      }}
    >
      {withGoal && (
        <Field label="Obiettivo">
          <input autoFocus className="field" value={goal} onChange={(e) => setGoal(e.target.value)} required />
        </Field>
      )}
      <Field label="Com'è andata">
        <textarea className="field min-h-16" value={outcome} onChange={(e) => setOutcome(e.target.value)} placeholder="Cosa è stato fatto, cosa no" />
      </Field>
      <Field label="Domande aperte (una per riga)">
        <textarea className="field min-h-16" value={questions} onChange={(e) => setQuestions(e.target.value)} />
      </Field>
      <Field label="Prossimo passo">
        <input className="field" value={next} onChange={(e) => setNext(e.target.value)} placeholder="La prima cosa da fare la prossima volta" />
      </Field>
      <div className="flex gap-2">
        <button className="btn btn-primary" disabled={busy}>
          {submitLabel}
        </button>
        {onCancel && (
          <button type="button" className="btn" onClick={onCancel}>
            Annulla
          </button>
        )}
      </div>
    </form>
  );
}

// ------------------------------------------------------------------- decisions

type DecisionDraft = { title: string; rationale: string; alternatives: string; kind: DecisionKind };

const emptyDraft = (kind: DecisionKind = "decision"): DecisionDraft => ({ title: "", rationale: "", alternatives: "", kind });

export function DecisionsPanel({ project, onChange }: Props) {
  const { error, busy, run } = useAction(onChange);
  const [showSuperseded, setShowSuperseded] = useState(false);
  // 'new', or the id of the decision being superseded / edited.
  const [form, setForm] = useState<{ mode: "new" } | { mode: "supersede" | "edit"; target: Decision } | null>(null);

  const superseded = project.decisions.filter((d) => d.status === "superseded");
  const visible = project.decisions.filter((d) => showSuperseded || d.status !== "superseded");
  const byId = new Map(project.decisions.map((d) => [d.id, d]));

  async function submit(draft: DecisionDraft) {
    const body = {
      title: draft.title,
      rationale: draft.rationale || null,
      alternatives: draft.alternatives || null,
      kind: draft.kind,
    };
    const ok = await run(() =>
      form?.mode === "edit"
        ? api.updateDecision(form.target.id, body)
        : api.recordDecision(project.slug, { ...body, supersedes_id: form?.mode === "supersede" ? form.target.id : null }),
    );
    if (ok) setForm(null);
  }

  return (
    <Panel
      title="Decisioni e vincoli"
      subtitle="Le scelte prese e il perché. Una decisione non si cancella: si sostituisce."
      action={
        !form && (
          <button className="btn py-1 text-xs" onClick={() => setForm({ mode: "new" })}>
            + Nuova
          </button>
        )
      }
    >
      {form && (
        <div className="mb-4 rounded-md border border-line bg-surface-2 p-3">
          {form.mode === "supersede" && <p className="mb-2 text-xs text-muted">Sostituisce: {form.target.title}</p>}
          <DecisionForm
            key={form.mode === "new" ? "new" : `${form.mode}-${form.target.id}`}
            initial={
              form.mode === "edit"
                ? { title: form.target.title, rationale: form.target.rationale ?? "", alternatives: form.target.alternatives ?? "", kind: form.target.kind }
                : emptyDraft(form.mode === "supersede" ? form.target.kind : "decision")
            }
            submitLabel={form.mode === "edit" ? "Salva" : form.mode === "supersede" ? "Sostituisci" : "Registra"}
            busy={busy}
            onSubmit={submit}
            onCancel={() => setForm(null)}
          />
        </div>
      )}
      <ErrorBox error={error} />

      {visible.length === 0 && !form ? (
        <p className="text-sm text-muted">Nessuna decisione registrata.</p>
      ) : (
        <ul className="divide-y divide-line">
          {visible.map((d) => (
            <DecisionItem
              key={d.id}
              decision={d}
              replacedBy={d.superseded_by_id ? byId.get(d.superseded_by_id) : undefined}
              busy={busy}
              onReview={() => run(() => api.updateDecision(d.id, { status: d.status === "review" ? "active" : "review" }))}
              onEdit={() => setForm({ mode: "edit", target: d })}
              onSupersede={() => setForm({ mode: "supersede", target: d })}
            />
          ))}
        </ul>
      )}
      {superseded.length > 0 && (
        <button className="mt-3 text-xs text-muted hover:text-fg" onClick={() => setShowSuperseded(!showSuperseded)}>
          {showSuperseded ? "Nascondi" : "Mostra"} le {superseded.length} sostituite
        </button>
      )}
    </Panel>
  );
}

function DecisionItem({
  decision: d,
  replacedBy,
  busy,
  onReview,
  onEdit,
  onSupersede,
}: {
  decision: Decision;
  replacedBy?: Decision;
  busy: boolean;
  onReview: () => void;
  onEdit: () => void;
  onSupersede: () => void;
}) {
  const old = d.status === "superseded";
  return (
    <li className={`group py-3 first:pt-0 last:pb-0 ${old ? "opacity-60" : ""}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className={`rounded px-1.5 py-px text-[11px] font-medium ${d.kind === "constraint" ? "bg-info/15 text-info" : "bg-surface-2 text-muted"}`}>
          {d.kind === "constraint" ? "Vincolo" : "Decisione"}
        </span>
        {d.status === "review" && <span className="rounded bg-warn-soft px-1.5 py-px text-[11px] font-medium text-warn">Da rivedere</span>}
        <span className={`font-medium ${old ? "line-through" : ""}`}>{d.title}</span>
        <span className="ml-auto text-xs text-muted" title={formatDate(d.created_at)}>
          #{d.id} · {relativeTime(d.created_at)}
        </span>
      </div>
      {d.rationale && <p className="mt-1 whitespace-pre-line text-sm text-muted">{d.rationale}</p>}
      {d.alternatives && (
        <p className="mt-1 whitespace-pre-line text-xs text-muted">
          <span className="font-medium">Alternative scartate:</span> {d.alternatives}
        </p>
      )}
      {old && replacedBy && <p className="mt-1 text-xs text-muted">Sostituita da #{replacedBy.id}: {replacedBy.title}</p>}
      {!old && (
        <div className="mt-2 flex gap-3 text-xs text-muted opacity-0 transition group-focus-within:opacity-100 group-hover:opacity-100">
          <button className="hover:text-fg" disabled={busy} onClick={onReview}>
            {d.status === "review" ? "Conferma" : "Da rivedere"}
          </button>
          <button className="hover:text-fg" onClick={onSupersede}>
            Sostituisci
          </button>
          <button className="hover:text-fg" onClick={onEdit}>
            Correggi
          </button>
        </div>
      )}
    </li>
  );
}

function DecisionForm({
  initial,
  submitLabel,
  busy,
  onSubmit,
  onCancel,
}: {
  initial: DecisionDraft;
  submitLabel: string;
  busy: boolean;
  onSubmit: (d: DecisionDraft) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const set = (k: keyof DecisionDraft) => (e: { target: { value: string } }) => setDraft({ ...draft, [k]: e.target.value });
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(draft);
      }}
    >
      <div className="flex gap-2">
        <input autoFocus className="field flex-1" placeholder="Cosa è stato deciso" value={draft.title} onChange={set("title")} required />
        <select className="field w-auto" value={draft.kind} onChange={set("kind")} aria-label="Tipo">
          <option value="decision">Decisione</option>
          <option value="constraint">Vincolo</option>
        </select>
      </div>
      <textarea className="field min-h-16" placeholder="Perché" value={draft.rationale} onChange={set("rationale")} />
      <textarea className="field min-h-12" placeholder="Alternative considerate e scartate" value={draft.alternatives} onChange={set("alternatives")} />
      <div className="flex gap-2">
        <button className="btn btn-primary" disabled={busy}>
          {submitLabel}
        </button>
        <button type="button" className="btn" onClick={onCancel}>
          Annulla
        </button>
      </div>
    </form>
  );
}

// -------------------------------------------------------------------- sessions

export function SessionsPanel({ sessions }: { sessions: WorkSession[] }) {
  const finished = sessions.filter((s) => s.ended_at);
  if (finished.length === 0) return null;
  return (
    <Panel title="Sessioni di lavoro">
      <ul className="space-y-4">
        {finished.map((s) => (
          <li key={s.id} className="text-sm">
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-medium">{s.goal}</span>
              <span className="shrink-0 text-xs text-muted" title={`${formatDate(s.started_at)} → ${formatDate(s.ended_at)}`}>
                {relativeTime(s.ended_at)}
              </span>
            </div>
            {s.outcome && <p className="mt-1 whitespace-pre-line text-muted">{s.outcome}</p>}
            <div className="mt-1 flex flex-wrap gap-1.5">
              {s.next_step && <Chip>→ {s.next_step}</Chip>}
              {s.open_questions.map((q, i) => (
                <Chip key={i}>? {q}</Chip>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
