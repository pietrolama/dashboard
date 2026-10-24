"""AI analysis of one project: what is declared, checked against the code.

The work is split between the two agents so neither reads what the other
already read:

  1. extract  agy (read-only) reads the declared state and the project's
              documentation, never the source, and returns a summary and a
              list of concrete claims that the code or Git history can settle;
  2. verify   claude (read-only) checks each claim against the code and the
              recent history, and proposes changes to the declared state
              only where it has evidence.

Project OS merges the two replies itself. Nothing declared changes on its
own: each contradiction and suggestion waits for the user to accept or
dismiss it. A finished analysis is also stored as an `agent_analysis`
evidence, so agents and briefs see it next to git, readme and filesystem.

An analysis takes minutes, so the API process runs queued ones from a
background loop (see pending()). If agy is missing or fails, claude does the
extraction too, and the step records it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import agents, gitinfo
from .db import Database, utcnow
from .errors import Conflict, NotFound, record_event
from .relations import LINK_KINDS
from .team import _extract_json

EXTRACT_TIMEOUT_S = 300
VERIFY_TIMEOUT_S = 600
MAX_CLAIMS = 10
MAX_SUGGESTIONS = 5
VERDICTS = {"confirmed": "confirmed", "contradicted": "contradiction", "unverifiable": "unverifiable"}
PHASES = ("idea", "prototype", "development", "stabilization", "release")
LIFECYCLES = ("active", "paused", "archived")
DECISION_KINDS = ("decision", "constraint")
OPEN_STATUSES = ("queued", "running")

EXTRACT_PROMPT = """\
Stai preparando l'analisi del progetto "{name}". Un secondo agente verificherà nel \
codice quello che scrivi: il tuo compito è solo leggere ciò che è dichiarato.

# Stato dichiarato in Project OS

{declared}

# Cosa fare

Leggi il README e la documentazione del repository (i file .md nella radice e in docs/). \
Non aprire il codice sorgente e non modificare nulla. Poi:
1. scrivi una sintesi di 3-5 frasi: cosa fa il progetto e a che punto dice di essere;
2. elenca da 3 a {max_claims} affermazioni che si possono verificare nel codice o nella \
cronologia Git, scegliendo quelle che più probabilmente non tornano più: lo stato e il \
prossimo passo dichiarati, le decisioni e i vincoli, le funzionalità date per fatte o \
recenti. Tralascia i dettagli stabili (valori di default, nomi di file, comandi di avvio) \
salvo che sembrino in contrasto con altro. Ognuna deve essere concreta e controllabile, e \
dire da dove viene (per esempio "README", "decisione #3", "sessione #45").

Scrivi in italiano. Rispondi solo con questo JSON, senza altro testo:
{{"summary": "...", "claims": [{{"claim": "...", "source": "..."}}]}}
"""

VERIFY_PROMPT = """\
Verifica alcune affermazioni sul progetto "{name}" confrontandole con il codice di \
questo repository. Non modificare nessun file.

# Repository

{git}

# Stato dichiarato in Project OS

{declared}

# Altri progetti dell'utente

{projects}

# Affermazioni da verificare

{claims}

# Cosa fare

Per ogni affermazione cerca nel codice e nella cronologia sopra solo quello che serve a \
deciderla; non esplorare il resto del repository. Il verdetto è:
- "confirmed": il codice o la cronologia la confermano;
- "contradicted": la smentiscono (spiega cosa risulta invece);
- "unverifiable": il repository non basta per deciderla.
In "refs" indica i file (con la riga quando serve) o i commit su cui ti basi.

Poi, solo se hai prove, proponi al massimo {max_suggestions} modifiche allo stato dichiarato:
- {{"type": "next_step", "value": "..."}} se il prossimo passo dichiarato è già fatto o superato;
- {{"type": "phase", "value": "idea|prototype|development|stabilization|release"}};
- {{"type": "lifecycle", "value": "active|paused|archived"}};
- {{"type": "description", "value": "..."}} se la descrizione manca o non corrisponde al codice;
- {{"type": "decision", "kind": "decision|constraint", "title": "...", "rationale": "..."}} \
per una scelta evidente nel codice e non ancora registrata;
- {{"type": "link", "target": "<slug>", "kind": "depends_on|part_of|evolves_from|related", \
"note": "..."}} se il codice usa o richiama uno degli altri progetti elencati.

Scrivi in italiano. Rispondi solo con questo JSON, senza altro testo:
{{"verdicts": [{{"n": 1, "verdict": "confirmed", "explanation": "...", "refs": ["src/app.py:12"]}}], \
"suggestions": [{{"action": {{"type": "phase", "value": "development"}}, "reason": "...", "refs": []}}]}}
"""


_STRINGS = {"type": "array", "items": {"type": "string"}}
EXTRACT_SCHEMA = {
    "type": "object", "required": ["summary", "claims"], "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "claims": {"type": "array", "items": {
            "type": "object", "required": ["claim", "source"], "additionalProperties": False,
            "properties": {"claim": {"type": "string"}, "source": {"type": "string"}}}},
    },
}
# Actions are checked by normalize_action; the schema only fixes the shape.
VERIFY_SCHEMA = {
    "type": "object", "required": ["verdicts", "suggestions"], "additionalProperties": False,
    "properties": {
        "verdicts": {"type": "array", "items": {
            "type": "object", "required": ["n", "verdict", "explanation", "refs"], "additionalProperties": False,
            "properties": {"n": {"type": "integer"}, "verdict": {"enum": list(VERDICTS)},
                           "explanation": {"type": "string"}, "refs": _STRINGS}}},
        "suggestions": {"type": "array", "items": {
            "type": "object", "required": ["action", "reason", "refs"], "additionalProperties": False,
            "properties": {"action": {"type": "object"}, "reason": {"type": "string"}, "refs": _STRINGS}}},
    },
}


class AnalysisError(Exception):
    pass


def _text(value: Any, limit: int = 2000) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def _refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [r.strip()[:300] for r in value if isinstance(r, str) and r.strip()][:10]


def normalize_claims(data: dict) -> tuple[str | None, list[dict]]:
    claims = []
    for c in data.get("claims") or []:
        if isinstance(c, dict) and (claim := _text(c.get("claim"), 500)):
            claims.append({"claim": claim, "source": _text(c.get("source"), 200)})
    if not claims:
        raise AnalysisError("the reply contains no claims")
    return _text(data.get("summary"), 3000), claims[:MAX_CLAIMS]


def normalize_action(raw: Any, slugs: set[str], own_slug: str) -> dict | None:
    """A suggestion's action if it is one Project OS knows how to apply, else None."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    if kind in ("next_step", "description"):
        value = _text(raw.get("value"), 2000)
        return {"type": kind, "value": value} if value else None
    if kind == "phase" and raw.get("value") in PHASES:
        return {"type": kind, "value": raw["value"]}
    if kind == "lifecycle" and raw.get("value") in LIFECYCLES:
        return {"type": kind, "value": raw["value"]}
    if kind == "decision" and (title := _text(raw.get("title"), 300)):
        k = raw.get("kind") if raw.get("kind") in DECISION_KINDS else "decision"
        return {"type": kind, "kind": k, "title": title, "rationale": _text(raw.get("rationale"), 5000)}
    if kind == "link" and raw.get("target") in slugs and raw["target"] != own_slug:
        k = raw.get("kind") if raw.get("kind") in LINK_KINDS else "related"
        return {"type": kind, "target": raw["target"], "kind": k, "note": _text(raw.get("note"), 2000)}
    return None


def merge_findings(claims: list[dict], verify: dict, slugs: set[str], own_slug: str) -> list[dict]:
    """The claims with their verdicts, then the suggestions that can be applied.

    A claim the verifier skipped is kept as unverifiable, so nothing the
    extractor read silently disappears.
    """
    verdicts: dict[int, dict] = {}
    for v in verify.get("verdicts") or []:
        if isinstance(v, dict) and isinstance(v.get("n"), int) and 1 <= v["n"] <= len(claims):
            verdicts.setdefault(v["n"], v)
    findings = []
    for n, c in enumerate(claims, 1):
        v = verdicts.get(n, {})
        findings.append({
            "kind": VERDICTS.get(v.get("verdict"), "unverifiable"),
            "text": c["claim"], "source": c["source"],
            "detail": _text(v.get("explanation")) or (None if v else "Il verificatore non l'ha esaminata."),
            "refs": _refs(v.get("refs")), "action": None,
        })
    for s in (verify.get("suggestions") or [])[:MAX_SUGGESTIONS * 2]:
        if not isinstance(s, dict):
            continue
        action = normalize_action(s.get("action"), slugs, own_slug)
        if action and sum(f["kind"] == "suggestion" for f in findings) < MAX_SUGGESTIONS:
            findings.append({"kind": "suggestion", "text": describe_action(action), "source": None,
                             "detail": _text(s.get("reason")), "refs": _refs(s.get("refs")), "action": action})
    return findings


def describe_action(a: dict) -> str:
    t = a["type"]
    if t == "next_step":
        return f"Prossimo passo: {a['value']}"
    if t == "description":
        return f"Descrizione: {a['value']}"
    if t == "phase":
        return f"Fase: {a['value']}"
    if t == "lifecycle":
        return f"Ciclo di vita: {a['value']}"
    if t == "decision":
        return f"{'Vincolo' if a['kind'] == 'constraint' else 'Decisione'}: {a['title']}"
    return f"Relazione {a['kind']} con {a['target']}"


class Analysis:
    def __init__(self, db: Database, registry):
        self.db = db
        self.registry = registry

    # ------------------------------------------------------------------ input

    def _repo(self, project: dict) -> dict:
        repo = next((r for r in project["repositories"] if not r["missing_since"]), None)
        if not repo or not Path(repo["path"]).is_dir():
            raise Conflict("the analysis needs the project folder on disk")
        return repo

    def _declared(self, project: dict) -> str:
        """The declared state as the agents read it; also the bulk of the input hash."""
        memory = self.registry.memory
        lines = [
            f"- Nome: {project['name']} ({project['slug']})",
            f"- Descrizione: {project['description'] or '(non dichiarata)'}",
            f"- Ciclo di vita: {project['lifecycle']}; fase: {project['phase'] or '(non dichiarata)'}",
        ]
        decisions = [d for d in memory.list_decisions(project["id"]) if d["status"] != "superseded"]
        if decisions:
            lines.append("- Decisioni e vincoli:")
            lines += [f"  - #{d['id']} ({d['kind']}, {d['status']}): {d['title']}"
                      + (f" — {d['rationale']}" if d["rationale"] else "") for d in decisions]
        for s in memory.list_sessions(project["id"], limit=3):
            lines.append(f"- Sessione #{s['id']} ({(s['ended_at'] or s['started_at'])[:10]}): {s['goal']}")
            if s["outcome"]:
                lines.append(f"  - Esito: {s['outcome']}")
            if s["next_step"]:
                lines.append(f"  - Prossimo passo: {s['next_step']}")
            lines += [f"  - Domanda aperta: {q}" for q in s["open_questions"]]
        relations = self.registry.relations.relations(project["id"])
        if relations:
            lines.append("- Relazioni con altri progetti:")
            lines += [f"  - {r['source']} {r['kind']} {r['target']}" for r in relations]
        return "\n".join(lines)

    def _input(self, project_id: int) -> dict:
        project = self.registry.get_project(project_id)
        repo = self._repo(project)
        root = Path(repo["path"])
        git = None
        if repo["vcs"] == "git":
            try:
                info = gitinfo.inspect(root)
                git = {"branch": info.branch, "head": info.head,
                       "dirty": info.dirty_files + info.untracked_files,
                       "commits": gitinfo.recent_commits(root, 30)}
            except gitinfo.GitError:
                git = None
        declared = self._declared(project)
        readme = (project["evidence"].get("readme") or {}).get("content_hash")
        basis = {"declared": declared, "readme": readme,
                 "head": git and git["head"], "dirty": git and git["dirty"]}
        digest = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
        return {"project": project, "root": root, "git": git, "declared": declared, "hash": digest}

    @staticmethod
    def _git_text(git: dict | None) -> str:
        if not git:
            return "Nessuna cronologia Git disponibile."
        out = [f"Branch {git['branch'] or '(detached)'}, HEAD {(git['head'] or '')[:12]}, "
               f"{git['dirty']} file modificati o non tracciati.", "", "Ultimi commit:"]
        out += [f"- {c['commit'][:9]} {c['date'][:10]} {c['subject']}" for c in git["commits"]]
        return "\n".join(out)

    def _projects_text(self, own_slug: str) -> tuple[str, set[str]]:
        rows = [p for p in self.registry.list_projects() if p["slug"] != own_slug]
        text = "\n".join(f"- {p['slug']}: {p['name']}" + (f" — {(p['readme_summary'] or '')[:120]}" if p.get("readme_summary") else "")
                         for p in rows)
        return text or "(nessuno)", {p["slug"] for p in rows}

    # ------------------------------------------------------------------ reads

    def _row(self, analysis_id: int) -> dict:
        row = self.db.query_one(
            """SELECT a.*, p.slug AS project_slug FROM analyses a
               JOIN projects p ON p.id = a.project_id WHERE a.id = ?""", (analysis_id,))
        if not row:
            raise NotFound(f"analysis {analysis_id}")
        a = dict(row)
        a["steps"] = json.loads(a.pop("steps_json"))
        return a

    def get(self, analysis_id: int) -> dict:
        a = self._row(analysis_id)
        a["findings"] = [_finding(r) for r in self.db.query(
            "SELECT * FROM analysis_findings WHERE analysis_id = ? ORDER BY position", (analysis_id,))]
        return a

    def list(self, project_id: int, limit: int = 20) -> list[dict]:
        rows = self.db.query("SELECT id FROM analyses WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                             (project_id, limit))
        return [self._row(r["id"]) for r in rows]

    def latest(self, project_id: int) -> dict | None:
        """The newest analysis with its findings, and whether its input is still current."""
        row = self.db.query_one("SELECT id FROM analyses WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                                (project_id,))
        if not row:
            return None
        a = self.get(row["id"])
        try:
            a["current"] = a["input_hash"] == self._input(project_id)["hash"]
        except Conflict:
            a["current"] = False
        return a

    def pending(self) -> list[int]:
        return [r["id"] for r in self.db.query("SELECT id FROM analyses WHERE status = 'queued' ORDER BY id")]

    # ------------------------------------------------------------- lifecycle

    def request(self, project_id: int, force: bool = False, source: str = "user") -> dict:
        """Queue an analysis; refused when nothing changed since the last one, unless forced."""
        inp = self._input(project_id)
        last = self.db.query_one(
            "SELECT id, input_hash FROM analyses WHERE project_id = ? AND status = 'done' ORDER BY id DESC LIMIT 1",
            (project_id,))
        if last and last["input_hash"] == inp["hash"] and not force:
            raise Conflict(f"Nothing changed since analysis #{last['id']}")
        now = utcnow()
        head = inp["git"] and inp["git"]["head"]
        with self.db.transaction() as c:
            if c.execute("SELECT 1 FROM analyses WHERE project_id = ? AND status IN ('queued', 'running')",
                         (project_id,)).fetchone():
                raise Conflict("An analysis of this project is already running")
            analysis_id = c.execute(
                """INSERT INTO analyses (project_id, status, input_hash, head_commit, created_at)
                   VALUES (?, 'queued', ?, ?, ?)""", (project_id, inp["hash"], head, now)).lastrowid
            record_event(c, project_id, "analysis_requested", source, {"analysis": analysis_id})
        return self.get(analysis_id)

    def recover(self) -> None:
        """Fail analyses left running by a server that stopped mid-way."""
        with self.db.transaction() as c:
            for r in c.execute("SELECT id, project_id FROM analyses WHERE status = 'running'").fetchall():
                c.execute("UPDATE analyses SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                          ("Interrotta: il server si è fermato durante l'analisi.", utcnow(), r["id"]))
                record_event(c, r["project_id"], "analysis_failed", "system", {"analysis": r["id"]})

    def _update(self, analysis_id: int, **fields) -> None:
        if "steps" in fields:
            fields["steps_json"] = json.dumps(fields.pop("steps"))
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db.transaction() as c:
            c.execute(f"UPDATE analyses SET {cols} WHERE id = ?", (*fields.values(), analysis_id))

    def run(self, analysis_id: int) -> None:
        """Extract with agy, verify with claude, store the findings. Slow: called from the background loop."""
        a = self._row(analysis_id)
        if a["status"] != "queued":
            return
        self._update(analysis_id, status="running")
        steps: list[dict] = []
        try:
            inp = self._input(a["project_id"])
            project = inp["project"]
            summary, claims = self._extract(inp, steps, analysis_id)
            projects_text, slugs = self._projects_text(project["slug"])
            claims_text = "\n".join(f"{n}. {c['claim']}" + (f" (fonte: {c['source']})" if c["source"] else "")
                                    for n, c in enumerate(claims, 1))
            prompt = VERIFY_PROMPT.format(name=project["name"], git=self._git_text(inp["git"]),
                                          declared=inp["declared"], projects=projects_text, claims=claims_text,
                                          max_suggestions=MAX_SUGGESTIONS)
            verify = self._step("claude", "verify", prompt, inp["root"], VERIFY_TIMEOUT_S, steps, analysis_id,
                                VERIFY_SCHEMA)
            findings = merge_findings(claims, verify, slugs, project["slug"])
        except Exception as exc:  # whatever went wrong, the analysis must not stay "running"
            self._finish(analysis_id, a["project_id"], "failed", steps, error=str(exc) or type(exc).__name__)
            if not isinstance(exc, (AnalysisError, Conflict, NotFound)):
                raise
            return
        self._finish(analysis_id, a["project_id"], "done", steps, summary=summary, findings=findings)

    def _extract(self, inp: dict, steps: list[dict], analysis_id: int) -> tuple[str | None, list[dict]]:
        prompt = EXTRACT_PROMPT.format(name=inp["project"]["name"], declared=inp["declared"], max_claims=MAX_CLAIMS)
        try:
            return normalize_claims(self._step("agy", "extract", prompt, inp["root"], EXTRACT_TIMEOUT_S,
                                               steps, analysis_id))
        except AnalysisError:
            steps[-1]["fallback"] = "claude"
            return normalize_claims(self._step("claude", "extract", prompt, inp["root"], EXTRACT_TIMEOUT_S,
                                               steps, analysis_id, EXTRACT_SCHEMA))

    def _step(self, agent: str, role: str, prompt: str, root: Path, timeout_s: int,
              steps: list[dict], analysis_id: int, schema: dict | None = None) -> dict:
        """Run one agent read-only and return the JSON object of its reply."""
        steps.append({"agent": agent, "role": role, "status": "running"})
        self._update(analysis_id, steps=steps)
        try:
            result = agents.run(agent, prompt, root, mode="read", timeout_s=timeout_s, json_schema=schema)
        except agents.AgentError as exc:
            steps[-1].update(status="error", error=str(exc))
            raise AnalysisError(f"{agent} ({role}): {exc}") from exc
        steps[-1].update(status=result.status, duration_s=result.duration_s, tokens=result.tokens,
                         cost_usd=result.cost_usd)
        self._update(analysis_id, steps=steps)
        if not result.ok:
            steps[-1]["error"] = (result.text or result.stderr)[:500]
            raise AnalysisError(f"{agent} ({role}) {result.status}: {steps[-1]['error']}")
        if result.structured is not None:
            return result.structured
        try:
            return _extract_json(result.text)
        except ValueError as exc:
            # Kept so a failed step can be understood without rerunning it.
            steps[-1].update(status="error", error=f"reply without JSON: {result.text.strip()[:400]}")
            raise AnalysisError(f"{agent} ({role}): {exc}") from exc

    def _finish(self, analysis_id: int, project_id: int, status: str, steps: list[dict],
                summary: str | None = None, findings: list[dict] | None = None, error: str | None = None) -> None:
        now = utcnow()
        with self.db.transaction() as c:
            c.execute("""UPDATE analyses SET status = ?, steps_json = ?, summary = ?, error = ?, finished_at = ?
                         WHERE id = ?""", (status, json.dumps(steps), summary, error, now, analysis_id))
            counts: dict[str, int] = {}
            for pos, f in enumerate(findings or [], 1):
                counts[f["kind"]] = counts.get(f["kind"], 0) + 1
                c.execute(
                    """INSERT INTO analysis_findings (analysis_id, position, kind, text, detail, source,
                                                      refs_json, action_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (analysis_id, pos, f["kind"], f["text"], f["detail"], f["source"], json.dumps(f["refs"]),
                     json.dumps(f["action"]) if f["action"] else None))
            if status == "done":
                head = c.execute("SELECT head_commit FROM analyses WHERE id = ?", (analysis_id,)).fetchone()[0]
                payload = {"analysis": analysis_id, "head_commit": head, "summary": summary, "counts": counts,
                           "contradictions": [f["text"] for f in findings or [] if f["kind"] == "contradiction"]}
                body = json.dumps(payload, sort_keys=True)
                c.execute(
                    """INSERT INTO evidence (project_id, source_type, source_ref, content_hash, observed_at, payload_json)
                       VALUES (?, 'agent_analysis', ?, ?, ?, ?)""",
                    (project_id, f"analysis:{analysis_id}", hashlib.sha256(body.encode()).hexdigest(), now, body))
            record_event(c, project_id, "analysis_done" if status == "done" else "analysis_failed", "system",
                         {"analysis": analysis_id, **({"error": (error or "")[:200]} if error else counts)})

    # -------------------------------------------------------------- findings

    def resolve(self, finding_id: int, accept: bool, source: str = "user") -> dict:
        """Accept or dismiss a finding. Accepting a suggestion applies it to the declared state."""
        row = self.db.query_one(
            """SELECT f.*, a.project_id FROM analysis_findings f JOIN analyses a ON a.id = f.analysis_id
               WHERE f.id = ?""", (finding_id,))
        if not row:
            raise NotFound(f"finding {finding_id}")
        if row["kind"] not in ("contradiction", "suggestion"):
            raise Conflict("only contradictions and suggestions can be accepted or dismissed")
        if row["status"] != "open":
            raise Conflict(f"finding {finding_id} is already {row['status']}")
        if accept and row["action_json"]:
            self._apply(row["project_id"], row["analysis_id"], json.loads(row["action_json"]), source)
        status = "accepted" if accept else "dismissed"
        with self.db.transaction() as c:
            c.execute("UPDATE analysis_findings SET status = ?, resolved_at = ? WHERE id = ?",
                      (status, utcnow(), finding_id))
            record_event(c, row["project_id"], f"finding_{status}", source,
                         {"analysis": row["analysis_id"], "kind": row["kind"], "text": row["text"][:200]})
        return _finding(self.db.query_one("SELECT * FROM analysis_findings WHERE id = ?", (finding_id,)))

    def _apply(self, project_id: int, analysis_id: int, action: dict, source: str) -> None:
        t = action["type"]
        if t in ("phase", "lifecycle", "description"):
            self.registry.update_project(project_id, **{t: action["value"]})
        elif t == "decision":
            self.registry.memory.record_decision(
                project_id, action["title"], action.get("rationale"), kind=action["kind"], source=source)
        elif t == "link":
            self.registry.relations.add_link(project_id, self.registry.resolve(action["target"]),
                                             action["kind"], action.get("note"), source=source)
        elif t == "next_step":
            self.registry.memory.log_session(
                project_id, f"Prossimo passo dall'analisi AI #{analysis_id}", next_step=action["value"],
                source=source)
        else:
            raise Conflict(f"unknown action {t!r}")


def _finding(r) -> dict:
    f = dict(r)
    f["refs"] = json.loads(f.pop("refs_json"))
    action = f.pop("action_json")
    f["action"] = json.loads(action) if action else None
    f.pop("project_id", None)
    return f
