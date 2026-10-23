"""Team runs: Claude Code and Antigravity working together on one request.

A run moves through these states:

  planning     claude, headless and read-only, splits the request into tasks
  plan_ready   the plan waits for the user, who can edit it, then starts it
  running      each agent works in its own git worktree and branch, inside a
               visible terminal (tmux); they coordinate through the team_*
               MCP tools, which call into this module
  integrating  the agents' branches are merged into team/<id> and claude
               reviews the combined diff
  conflict     the branches do not merge cleanly; the user resolves it in the
               integration worktree and retries
  review       the user applies team/<id> to the project branch (as one squashed
               commit), or discards it
  applied | discarded | failed

Planning and integration take minutes, so the API process runs them from a
background loop (see pending_jobs()); everything else is immediate. The
agents never talk to each other directly: a message or a finished task is
stored here and typed into the other agent's terminal.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import agents, terminal
from .config import Settings
from .db import Database, utcnow
from .errors import Conflict, NotFound, record_event

AGENTS = agents.AGENTS
AGENT_NAME = {"claude": "Claude Code", "agy": "Antigravity"}
TASK_STATUSES = ("todo", "doing", "done", "blocked")
ACTIVE_STATUSES = ("planning", "plan_ready", "running", "integrating", "conflict", "review")
GIT_TIMEOUT_S = 120
PLAN_TIMEOUT_S = 900
REVIEW_TIMEOUT_S = 900
# Tools an agent needs to take part; Claude Code would otherwise ask before each call.
TEAM_TOOLS = ("team_board", "team_task", "team_message", "team_done")
# Questions a CLI asks before it goes on: folder trust and permission prompts.
_WAITING = re.compile(
    r"trust this folder|Do you trust|Allow calling this tool\?|Do you want to proceed\?|"
    r"Do you want to (make this edit|create)|Allow (command|running|executing)", re.I)
# A [team] line still sitting in a CLI's input box: the prompt marker, then our prefix.
_UNSENT = re.compile(r"^\s*[>❯]\s+\[team\] ")
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
# Left out when Project OS commits what an agent did not: build and test
# output is regenerated anyway, and two agents' copies of it always conflict.
_GENERATED = ("__pycache__", "*.pyc", ".pytest_cache", "node_modules", "dist", "build", ".venv",
              "*.egg-info", ".mypy_cache", ".ruff_cache", "coverage", ".coverage", "*.log")

PLAN_PROMPT = """\
You are planning work for a team of two coding agents, "claude" (Claude Code) and \
"agy" (Google Antigravity). They will work at the same time, each in its own git \
worktree of this repository, and their branches will then be merged.

# Project brief

{brief}

{previous}# User request

{request}

# What to do

Read the code you need to understand the request. Do not modify anything. Then split \
the work into tasks:
- Assign each task to "claude" or "agy" and balance the work, but keep the two agents \
on different files so their branches merge cleanly. Never give both agents the same file.
- If the request is small, or cannot be split without both touching the same files, \
give everything to one agent.
- Prefer independent tasks. Use depends_on (1-based numbers of other tasks) only when \
a task really needs another task's result.
- Each task's details must be self-contained instructions: what to change, where, and \
how to check it works.
- Write the summary, titles and details in Italian.

Reply with only this JSON and no other text:
{{"summary": "...", "tasks": [{{"agent": "claude", "title": "...", "details": "...", "depends_on": []}}]}}
"""

AGENT_PROMPT = """\
Sei "{agent}" ({agent_name}) in un team di due agenti che lavora sul progetto \
{project}. È il team run #{run_id}.

# Richiesta dell'utente

{request}

# Piano approvato

{plan_summary}

{tasks}

Le tue attività: {mine}.

# Come lavori

- Sei nel tuo git worktree {worktree}, sul branch {branch}. {other_line} \
Modifica solo file del tuo worktree e resta nelle tue attività; se devi toccare \
la parte dell'altro, scrivigli prima.
- Coordinati con gli strumenti MCP del server project-os, sempre con \
run_id={run_id} e agent="{agent}":
  - team_board: piano, stato di tutte le attività e messaggi;
  - team_task: aggiorna una tua attività (status "doing", "done" o "blocked", con un summary);
  - team_message: scrivi all'altro agente (to="{other}") o all'utente (to="user");
  - team_done: quando tutte le tue attività sono "done", con un riassunto di cosa hai fatto.
- Segna "doing" un'attività quando la inizi e "done" quando è finita e verificata.
- Se un'attività dipende da una dell'altro agente, aspetta che sia "done": te lo \
dirà un messaggio. Poi porta il suo lavoro nel tuo branch con `git merge <suo branch>`.
- Fai un commit sul tuo branch alla fine di ogni attività. Niente push, niente \
modifiche fuori dal worktree, niente nuove dipendenze se la richiesta non le prevede.
- Le righe che iniziano con "[team]" nel tuo input arrivano dal coordinatore: \
messaggi dell'altro agente, dell'utente o attività completate.

Inizia ora dalla tua prima attività senza dipendenze aperte. Se non ne hai, \
chiama team_board e aspetta i messaggi.
"""

REVIEW_PROMPT = """\
Two coding agents implemented this request in parallel; their branches are merged \
in this worktree. Review the combined change with `git diff {base}..HEAD`. Do not \
modify any file.

User request:
{request}

Plan:
{plan}

Report in Italian, concisely:
1. whether the request is fully met;
2. bugs, or problems where the two agents' parts meet;
3. what the user should check before merging.
"""


def _git(cwd: str | Path, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, input=input,
                              timeout=GIT_TIMEOUT_S, env=_GIT_ENV)
    except subprocess.TimeoutExpired as exc:
        raise Conflict(f"git {args[0]} timed out") from exc
    if check and proc.returncode != 0:
        raise Conflict(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


def _identity(repo: str | Path) -> list[str]:
    """Commit identity flags, for repositories where none is configured."""
    if _git(repo, "config", "user.email", check=False).stdout.strip():
        return []
    return ["-c", "user.name=Project OS", "-c", "user.email=project-os@localhost"]


def claude_trusts(repo: str | Path) -> bool | None:
    """Whether Claude Code was told to trust this repository; None when unknown.

    A worktree inherits the trust of its main repository. Without it Claude
    Code asks in the terminal, and its default answer is "No, exit".
    Read-only: ~/.claude.json belongs to Claude Code.
    """
    try:
        projects = json.loads((Path.home() / ".claude.json").read_text()).get("projects", {})
    except (OSError, ValueError, AttributeError):
        return None
    return bool((projects.get(str(Path(repo).resolve())) or {}).get("hasTrustDialogAccepted"))


def _extract_json(text: str) -> dict:
    """The first JSON object in an agent's reply, fenced or bare."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    if "{" in text:
        candidates.append(text[text.index("{"):text.rindex("}") + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("the reply contains no JSON object")


def normalize_tasks(raw: Any) -> list[dict]:
    """Validate a task list; depends_on is rewritten to the 1-based numbers of the result.

    Each input task may carry a `ref` (any integer) that the depends_on of the
    others refer to; without refs, depends_on refers to 1-based positions.
    """
    if not isinstance(raw, list) or not raw:
        raise Conflict("the plan has no tasks")
    tasks, refs = [], {}
    for i, t in enumerate(raw, start=1):
        if not isinstance(t, dict):
            raise Conflict(f"task {i} is not an object")
        agent = str(t.get("agent", "")).strip().lower()
        title = str(t.get("title") or "").strip()
        if agent not in AGENTS:
            raise Conflict(f"task {i}: agent must be one of {', '.join(AGENTS)}")
        if not title:
            raise Conflict(f"task {i}: title is required")
        try:
            refs[int(t.get("ref", i))] = i
        except (TypeError, ValueError):
            refs[i] = i
        tasks.append({"agent": agent, "title": title[:300], "details": str(t.get("details") or "").strip() or None,
                      "depends_on": t.get("depends_on") or []})
    for number, t in enumerate(tasks, start=1):
        deps = set()
        for d in t["depends_on"] if isinstance(t["depends_on"], list) else []:
            try:
                d = int(d)
            except (TypeError, ValueError):
                continue
            if d in refs and refs[d] != number:
                deps.add(refs[d])
        t["depends_on"] = sorted(deps)
    return tasks


class Team:
    def __init__(self, db: Database, settings: Settings, registry=None):
        self.db = db
        self.settings = settings
        # Needed only to plan (for the project brief); the MCP server works without it.
        self.registry = registry
        self.team_dir = settings.team_dir or settings.db_path.parent / "team"

    # ------------------------------------------------------------------ reads

    def _run_row(self, run_id: int) -> dict:
        row = self.db.query_one(
            """SELECT r.*, p.slug AS project_slug, p.name AS project_name
               FROM team_runs r JOIN projects p ON p.id = r.project_id WHERE r.id = ?""",
            (run_id,),
        )
        if not row:
            raise NotFound(f"team run {run_id}")
        return dict(row)

    def _tasks(self, run_id: int) -> list[dict]:
        rows = self.db.query("SELECT * FROM team_tasks WHERE run_id = ? ORDER BY position", (run_id,))
        out = []
        for r in rows:
            t = dict(r)
            t["number"] = t.pop("position")
            t["depends_on"] = json.loads(t.pop("depends_on_json"))
            out.append(t)
        return out

    def _members(self, run_id: int) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM team_members WHERE run_id = ? ORDER BY agent", (run_id,))]

    def get_run(self, run_id: int) -> dict:
        run = self._run_row(run_id)
        run["tasks"] = self._tasks(run_id)
        run["members"] = self._members(run_id)
        run["messages"] = [dict(r) for r in self.db.query(
            "SELECT * FROM team_messages WHERE run_id = ? ORDER BY id", (run_id,))]
        session = run["tmux_session"]
        run["session_alive"] = bool(session) and terminal.session_exists(session)
        run["attach_command"] = terminal.attach_command(session) if session else None
        run["integration_worktree"] = str(self._worktree_root(run) / "integration")
        for m in run["members"]:
            m["waiting"] = run["status"] == "running" and run["session_alive"] and self._waiting(m["pane"])
        run["claude_trusted"] = claude_trusts(run["repo_path"])
        return run

    @staticmethod
    def _waiting(pane: str | None) -> bool:
        if not pane:
            return False
        try:
            return any(_WAITING.search(line) for line in terminal.capture(pane, 30))
        except terminal.TerminalError:
            return False

    def list_runs(self, project_id: int, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            """SELECT r.id, r.request, r.status, r.created_at, r.updated_at,
                      (SELECT COUNT(*) FROM team_tasks t WHERE t.run_id = r.id) AS task_count,
                      (SELECT COUNT(*) FROM team_tasks t WHERE t.run_id = r.id AND t.status = 'done') AS done_count
               FROM team_runs r WHERE r.project_id = ? ORDER BY r.id DESC LIMIT ?""",
            (project_id, limit),
        )
        return [dict(r) for r in rows]

    def pending_jobs(self) -> list[tuple[int, str]]:
        """Runs waiting for slow work: ("plan" | "integrate", run id)."""
        rows = self.db.query("SELECT id, status FROM team_runs WHERE status IN ('planning', 'integrating')")
        return [(r["id"], "plan" if r["status"] == "planning" else "integrate") for r in rows]

    # ------------------------------------------------------------- transitions

    def _set(self, run_id: int, source: str = "system", event: str | None = None, **fields) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db.transaction() as c:
            c.execute(f"UPDATE team_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))
            if event:
                project_id = c.execute("SELECT project_id FROM team_runs WHERE id = ?", (run_id,)).fetchone()[0]
                record_event(c, project_id, event, source, {"run": run_id, "status": fields.get("status")})

    def _require(self, run: dict, *statuses: str) -> None:
        if run["status"] not in statuses:
            raise Conflict(f"team run #{run['id']} is {run['status']}, expected {' or '.join(statuses)}")

    def create_run(self, project_id: int, repo_path: str | Path, request: str, source: str = "user") -> dict:
        request = (request or "").strip()
        if not request:
            raise Conflict("describe what the team should do")
        repo = Path(repo_path)
        if not (repo / ".git").exists():
            raise Conflict("team runs need a Git repository")
        head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip()
        if not head:
            raise Conflict("the repository has no commits yet")
        branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip() or None
        now = utcnow()
        with self.db.transaction() as c:
            open_run = c.execute(
                f"SELECT id FROM team_runs WHERE project_id = ? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                (project_id, *ACTIVE_STATUSES),
            ).fetchone()
            if open_run:
                raise Conflict(f"team run #{open_run['id']} is still open for this project")
            run_id = c.execute(
                """INSERT INTO team_runs (project_id, request, status, repo_path, base_branch, base_commit,
                                          created_at, updated_at)
                   VALUES (?, ?, 'planning', ?, ?, ?, ?, ?)""",
                (project_id, request, str(repo), branch, head, now, now),
            ).lastrowid
            record_event(c, project_id, "team_run_created", source, {"run": run_id, "request": request[:200]})
        return self.get_run(run_id)

    def plan(self, run_id: int) -> None:
        """Ask claude for a plan. Slow: called from the background loop."""
        run = self._run_row(run_id)
        self._require(run, "planning")
        brief = f"{run['project_name']}"
        if self.registry is not None:
            from . import resume

            try:
                brief = resume.build_brief(self.registry, run["project_id"], "analysis_only").markdown
            except Exception as exc:  # the brief is context, not a requirement
                brief = f"{run['project_name']} (brief unavailable: {exc})"
        prompt = PLAN_PROMPT.format(brief=brief, previous=self._previous_run(run), request=run["request"])
        try:
            result = agents.run("claude", prompt, run["repo_path"], mode="read", timeout_s=PLAN_TIMEOUT_S)
            if not result.ok:
                raise Conflict(f"planner {result.status}: {(result.text or result.stderr)[:500]}")
            data = _extract_json(result.text)
            tasks = normalize_tasks(data.get("tasks"))
        except (Conflict, ValueError, agents.AgentError) as exc:
            if self._run_row(run_id)["status"] == "planning":
                self._set(run_id, status="failed", error=f"Pianificazione non riuscita: {exc}", event="team_run_failed")
            return
        with self.db.lock:
            # The user may have discarded the run while the planner was thinking.
            if self._run_row(run_id)["status"] != "planning":
                return
            self._replace_tasks(run_id, tasks)
            self._set(run_id, status="plan_ready", plan_summary=str(data.get("summary") or "").strip() or None,
                      error=None)

    def _previous_run(self, run: dict) -> str:
        """The outcome of the project's last team run, so a request can build on it
        (for example: apply what the previous analysis found)."""
        prev = self.db.query_one(
            "SELECT * FROM team_runs WHERE project_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (run["project_id"], run["id"]),
        )
        if not prev:
            return ""
        parts = [f"# Previous team run on this project (#{prev['id']}, {prev['status']})\n",
                 f"Request: {prev['request']}\n"]
        if prev["plan_summary"]:
            parts.append(f"Plan: {prev['plan_summary']}\n")
        for m in self._members(prev["id"]):
            if m["summary"]:
                parts.append(f"{m['agent']} finished with: {m['summary']}\n")
        reports = self.db.query(
            "SELECT sender, body FROM team_messages WHERE run_id = ? AND recipient = 'user' ORDER BY id", (prev["id"],))
        for r in reports:
            parts.append(f"Report from {r['sender']} to the user:\n{r['body']}\n")
        if prev["review"]:
            parts.append(f"Final review: {prev['review']}\n")
        return "\n".join(parts) + "\n"

    def _replace_tasks(self, run_id: int, tasks: list[dict]) -> None:
        now = utcnow()
        with self.db.transaction() as c:
            c.execute("DELETE FROM team_tasks WHERE run_id = ?", (run_id,))
            for number, t in enumerate(tasks, start=1):
                c.execute(
                    """INSERT INTO team_tasks (run_id, position, agent, title, details, depends_on_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, number, t["agent"], t["title"], t["details"], json.dumps(t["depends_on"]), now),
                )

    def update_plan(self, run_id: int, tasks: list[dict], summary: str | None = None) -> dict:
        run = self._run_row(run_id)
        self._require(run, "plan_ready")
        self._replace_tasks(run_id, normalize_tasks(tasks))
        self._set(run_id, plan_summary=(summary or "").strip() or run["plan_summary"])
        return self.get_run(run_id)

    def replan(self, run_id: int, request: str | None = None) -> dict:
        run = self._run_row(run_id)
        self._require(run, "plan_ready", "failed")
        if run["status"] == "failed" and run["tmux_session"]:
            raise Conflict("this run already started; discard it and create a new one")
        fields = {"request": request.strip()} if request and request.strip() else {}
        self._set(run_id, status="planning", error=None, **fields)
        return self.get_run(run_id)

    def _worktree_root(self, run: dict) -> Path:
        base = self.settings.worktrees_dir or Path(run["repo_path"]).parent / ".project-os-worktrees"
        return base / f"{run['project_slug']}-{run['id']}"

    def start(self, run_id: int, source: str = "user") -> dict:
        run = self._run_row(run_id)
        self._require(run, "plan_ready")
        tasks = self._tasks(run_id)
        team = [a for a in AGENTS if any(t["agent"] == a for t in tasks)]
        if not team:
            raise Conflict("the plan has no tasks")
        missing = [a for a in team if not shutil.which(a)]
        if missing:
            raise Conflict(f"not found on PATH: {', '.join(missing)}")

        root = self._worktree_root(run)
        session = f"pos-{run_id}"
        run_dir = self.team_dir / f"run-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        members, created = [], []
        try:
            for agent in team:
                branch = f"team/{run_id}-{agent}"
                worktree = root / agent
                _git(run["repo_path"], "worktree", "add", "-b", branch, str(worktree), run["base_commit"])
                created.append((worktree, branch))
                members.append({"agent": agent, "branch": branch, "worktree": str(worktree)})
            panes = []
            for m in members:
                others = [o for o in members if o["agent"] != m["agent"]]
                prompt = self._agent_prompt(run, tasks, m, others[0] if others else None)
                prompt_file = run_dir / f"prompt-{m['agent']}.md"
                prompt_file.write_text(prompt)
                script = run_dir / f"launch-{m['agent']}.sh"
                script.write_text(self._launcher(run_id, m, prompt_file))
                script.chmod(0o755)
                panes.append((f"{AGENT_NAME[m['agent']]} · {m['branch']}", Path(m["worktree"]), script))
            pane_ids = terminal.open_session(session, panes)
        except Exception as exc:
            terminal.kill_session(session)
            for worktree, branch in created:
                _git(run["repo_path"], "worktree", "remove", "--force", str(worktree), check=False)
                _git(run["repo_path"], "branch", "-D", branch, check=False)
            raise Conflict(f"could not start the team: {exc}") from exc

        with self.db.transaction() as c:
            for m, pane in zip(members, pane_ids):
                c.execute("INSERT INTO team_members (run_id, agent, worktree, branch, pane) VALUES (?, ?, ?, ?, ?)",
                          (run_id, m["agent"], m["worktree"], m["branch"], pane))
        self._set(run_id, source, "team_run_started", status="running", tmux_session=session, error=None)
        terminal.open_window(session, self.settings.terminal)
        return self.get_run(run_id)

    def open_terminal(self, run_id: int) -> bool:
        run = self._run_row(run_id)
        if not run["tmux_session"] or not terminal.session_exists(run["tmux_session"]):
            raise Conflict("the terminal session of this run is not running")
        return terminal.open_window(run["tmux_session"], self.settings.terminal)

    def _agent_prompt(self, run: dict, tasks: list[dict], me: dict, other: dict | None) -> str:
        def line(t: dict) -> str:
            deps = f" (dipende da {', '.join(f'#{d}' for d in t['depends_on'])})" if t["depends_on"] else ""
            details = f"\n   {t['details']}" if t["details"] else ""
            return f"#{t['number']} [{t['agent']}] {t['title']}{deps}{details}"

        mine = [f"#{t['number']}" for t in tasks if t["agent"] == me["agent"]]
        if other:
            other_line = f"L'altro agente, \"{other['agent']}\", lavora in {other['worktree']} sul branch {other['branch']}."
        else:
            other_line = "In questo run lavori da solo: tutte le attività sono tue."
        return AGENT_PROMPT.format(
            agent=me["agent"], agent_name=AGENT_NAME[me["agent"]], project=run["project_name"], run_id=run["id"],
            request=run["request"], plan_summary=run["plan_summary"] or "", tasks="\n".join(line(t) for t in tasks),
            mine=", ".join(mine), worktree=me["worktree"], branch=me["branch"], other_line=other_line,
            other=other["agent"] if other else "user",
        )

    def _launcher(self, run_id: int, member: dict, prompt_file: Path) -> str:
        prompt = f'"$(cat {shlex.quote(str(prompt_file))})"'
        if member["agent"] == "claude":
            tools = ",".join(f"mcp__project-os__{t}" for t in TEAM_TOOLS)
            # The prompt comes first: --allowedTools takes a variable number of values.
            cmd = (f"claude {prompt} --permission-mode acceptEdits --allowedTools {shlex.quote(tools)} "
                   f"-n {shlex.quote(f'team #{run_id}')}")
        else:
            cmd = f"agy --mode accept-edits -i {prompt}"
        # The project-os MCP server the agent starts inherits this environment,
        # so its team_* tools write to the database the dashboard reads.
        return (f"#!/usr/bin/env bash\n# Project OS team run #{run_id}: {member['agent']}\n"
                f"export PROJECT_OS_DB={shlex.quote(str(self.settings.db_path))}\n"
                f"cd {shlex.quote(member['worktree'])} || exit 1\nexec {cmd}\n")

    # ------------------------------------------------------------- the agents

    def _member(self, run_id: int, agent: str) -> dict:
        row = self.db.query_one("SELECT * FROM team_members WHERE run_id = ? AND agent = ?", (run_id, agent))
        if not row:
            raise Conflict(f"{agent} is not part of team run #{run_id}")
        return dict(row)

    def _log(self, run_id: int, sender: str, recipient: str, body: str) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT INTO team_messages (run_id, sender, recipient, body, created_at) VALUES (?, ?, ?, ?, ?)",
                      (run_id, sender, recipient, body, utcnow()))

    def _notify(self, run_id: int, agent: str, text: str) -> bool:
        """Type a [team] line into an agent's terminal. False when it cannot be delivered."""
        member = self.db.query_one("SELECT pane FROM team_members WHERE run_id = ? AND agent = ?", (run_id, agent))
        if not member or not member["pane"]:
            return False
        try:
            terminal.send(member["pane"], f"[team] {text}")
        except terminal.TerminalError:
            return False
        return True

    def resend_unsent(self) -> None:
        """Submit [team] lines a busy agent left in its input box.

        Antigravity queues one message while it works; the Enter of a second
        one is ignored and its text stays typed in the input box. Pressing
        Enter again once the agent is free sends it. Called from the
        background loop.
        """
        rows = self.db.query(
            """SELECT m.pane FROM team_members m JOIN team_runs r ON r.id = m.run_id
               WHERE r.status = 'running' AND m.pane IS NOT NULL""")
        for row in rows:
            try:
                if any(_UNSENT.match(line) for line in terminal.capture(row["pane"])):
                    terminal.press_enter(row["pane"])
            except terminal.TerminalError:
                continue

    def board(self, run_id: int, agent: str | None = None) -> dict:
        run = self.get_run(run_id)
        messages = run["messages"]
        if agent:
            messages = [m for m in messages if agent in (m["sender"], m["recipient"]) or m["recipient"] == "team"]
        return {
            "run_id": run_id, "status": run["status"], "request": run["request"], "plan_summary": run["plan_summary"],
            "tasks": [{k: t[k] for k in ("number", "agent", "title", "details", "depends_on", "status", "summary")}
                      for t in run["tasks"]],
            "members": [{k: m[k] for k in ("agent", "branch", "worktree", "status", "summary")} for m in run["members"]],
            "messages": messages[-30:],
        }

    def _require_running(self, run_id: int) -> dict:
        run = self._run_row(run_id)
        self._require(run, "running")
        return run

    def update_task(self, run_id: int, agent: str, number: int, status: str, summary: str | None = None) -> dict:
        self._require_running(run_id)
        self._member(run_id, agent)
        if status not in TASK_STATUSES:
            raise Conflict(f"status must be one of {', '.join(TASK_STATUSES)}")
        task = next((t for t in self._tasks(run_id) if t["number"] == number), None)
        if not task:
            raise NotFound(f"task #{number} in team run #{run_id}")
        if task["agent"] != agent:
            raise Conflict(f"task #{number} is assigned to {task['agent']}; send them a team_message instead")
        summary = (summary or "").strip() or None
        with self.db.transaction() as c:
            c.execute("UPDATE team_tasks SET status = ?, summary = COALESCE(?, summary), updated_at = ? WHERE id = ?",
                      (status, summary, utcnow(), task["id"]))
        self._log(run_id, agent, "team", f"#{number} → {status}" + (f": {summary}" if summary else ""))

        if status == "done":
            tasks = self._tasks(run_id)
            done = {t["number"] for t in tasks if t["status"] == "done"}
            branch = self._member(run_id, agent)["branch"]
            for other in AGENTS:
                if other == agent:
                    continue
                waiting = [t for t in tasks if t["agent"] == other and number in t["depends_on"]]
                if not waiting:
                    continue
                ready = [f"#{t['number']}" for t in waiting if set(t["depends_on"]) <= done]
                text = (f"{agent} ha completato #{number} «{task['title']}»"
                        + (f": {summary}" if summary else "") + f". Il suo lavoro è sul branch {branch}"
                        f" (git merge {branch})." + (f" Ora puoi procedere con {', '.join(ready)}." if ready else ""))
                self._notify(run_id, other, text)
        return self.board(run_id, agent)

    def post_message(self, run_id: int, sender: str, to: str, text: str) -> dict:
        run = self._require_running(run_id)
        text = (text or "").strip()
        if not text:
            raise Conflict("the message is empty")
        if sender != "user":
            self._member(run_id, sender)
        if to != "user":
            self._member(run_id, to)
        if to == sender:
            raise Conflict("send the message to the other agent or to the user")
        self._log(run_id, sender, to, text)
        delivered = True
        if to != "user":
            who = "dall'utente" if sender == "user" else f"da {sender}"
            delivered = self._notify(run_id, to, f"Messaggio {who}: {text}")
        return {"run_id": run["id"], "to": to, "delivered": delivered}

    def agent_done(self, run_id: int, agent: str, summary: str) -> dict:
        self._require_running(run_id)
        self._member(run_id, agent)
        open_tasks = [f"#{t['number']}" for t in self._tasks(run_id) if t["agent"] == agent and t["status"] != "done"]
        if open_tasks:
            raise Conflict(f"mark these tasks done (or blocked, telling the user) first: {', '.join(open_tasks)}")
        with self.db.transaction() as c:
            c.execute("UPDATE team_members SET status = 'done', summary = ? WHERE run_id = ? AND agent = ?",
                      (summary.strip() or None, run_id, agent))
        self._log(run_id, agent, "team", f"ha finito: {summary.strip()}")
        members = self._members(run_id)
        if all(m["status"] == "done" for m in members):
            self._set(run_id, status="integrating")
            return {"run_id": run_id, "team_finished": True,
                    "next": "Project OS now merges the branches and reviews the result; you can stop here."}
        for m in members:
            if m["agent"] != agent and m["status"] != "done":
                self._notify(run_id, m["agent"], f"{agent} ha finito la sua parte: {summary.strip()}")
        return {"run_id": run_id, "team_finished": False, "next": "Wait: the other agent is still working."}

    def finish_now(self, run_id: int) -> dict:
        """The user stops waiting for the agents and integrates what they have so far."""
        self._require_running(run_id)
        self._set(run_id, status="integrating")
        return self.get_run(run_id)

    # ------------------------------------------------------------ integration

    def integrate(self, run_id: int) -> None:
        """Merge the agents' branches into team/<id> and review the result. Slow: background loop."""
        run = self._run_row(run_id)
        self._require(run, "integrating")
        try:
            self._integrate(run)
        except Exception as exc:
            if self._run_row(run_id)["status"] == "integrating":
                self._set(run_id, status="conflict", error=f"Integrazione non riuscita: {exc}")

    def _integrate(self, run: dict) -> None:
        run_id, repo = run["id"], run["repo_path"]
        members = self._members(run_id)
        for m in members:
            # Work the agent did not commit still belongs to the run.
            wt = m["worktree"]
            if not Path(wt).is_dir():
                continue
            excludes = [f":(exclude,glob)**/{pattern}" for pattern in _GENERATED]
            excludes += [f":(exclude,glob)**/{pattern}/**" for pattern in _GENERATED if "*" not in pattern]
            _git(wt, "add", "-A", "--", ".", *excludes)
            if _git(wt, "diff", "--cached", "--quiet", check=False).returncode != 0:
                _git(wt, *_identity(wt), "commit", "-q", "-m", f"Team run #{run_id}: modifiche non committate di {m['agent']}")

        integration = self._worktree_root(run) / "integration"
        branch = f"team/{run_id}"
        if not integration.is_dir():
            _git(repo, "worktree", "add", "-b", branch, str(integration), run["base_commit"])
        for m in members:
            merged = _git(integration, *_identity(integration), "merge", "--no-ff", "--no-edit",
                          "-m", f"Team run #{run_id}: lavoro di {m['agent']}", m["branch"], check=False)
            if merged.returncode != 0:
                files = _git(integration, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                _git(integration, "merge", "--abort", check=False)
                self._set(run_id, status="conflict",
                          error=f"Il branch {m['branch']} non si unisce in modo pulito"
                                + (f" (conflitti in: {', '.join(files)})" if files else f": {merged.stderr.strip()[:300]}"))
                return

        diffstat = _git(integration, "diff", "--stat", run["base_commit"], "HEAD").stdout.strip()
        if not diffstat:
            review = "Gli agenti non hanno prodotto modifiche."
        else:
            plan = "\n".join(f"#{t['number']} [{t['agent']}] {t['title']}: {t['status']}" for t in self._tasks(run_id))
            prompt = REVIEW_PROMPT.format(base=run["base_commit"], request=run["request"], plan=plan)
            try:
                result = agents.run("claude", prompt, integration, mode="read", timeout_s=REVIEW_TIMEOUT_S)
                review = result.text if result.ok else f"Revisione non disponibile ({result.status}): {result.stderr[:300]}"
            except agents.AgentError as exc:
                review = f"Revisione non disponibile: {exc}"
        if self._run_row(run_id)["status"] == "integrating":
            self._set(run_id, event="team_run_review", status="review", diffstat=diffstat, review=review, error=None)

    def retry_integration(self, run_id: int) -> dict:
        run = self._run_row(run_id)
        self._require(run, "conflict")
        self._set(run_id, status="integrating", error=None)
        return self.get_run(run_id)

    def apply(self, run_id: int, source: str = "user") -> dict:
        """Merge team/<id> into the branch the run started from, in the project's own checkout."""
        run = self._run_row(run_id)
        self._require(run, "review")
        repo = run["repo_path"]
        current = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
        if not run["base_branch"] or current != run["base_branch"]:
            raise Conflict(f"switch the project to branch {run['base_branch'] or '(none)'} first; it is on {current or 'a detached HEAD'}")
        if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            raise Conflict("the project has uncommitted changes: commit or stash them first")
        # Squashed: the project gets one commit per run, without the agents'
        # intermediate and automatic commits.
        merged = _git(repo, "merge", "--squash", f"team/{run_id}", check=False)
        if merged.returncode != 0:
            _git(repo, "reset", "--merge", check=False)
            raise Conflict(f"merge failed, nothing was changed: {(merged.stderr or merged.stdout).strip()[:300]}")
        tasks = "\n".join(f"- #{t['number']} [{t['agent']}] {t['title']}" for t in self._tasks(run_id))
        message = f"Team run #{run_id}: {run['request'].splitlines()[0][:72]}\n\n{run['request']}\n\n{tasks}\n"
        _git(repo, *_identity(repo), "commit", "-q", "-F", "-", input=message)
        self._cleanup(run)
        self._set(run_id, source, "team_run_applied", status="applied")
        return self.get_run(run_id)

    def discard(self, run_id: int, source: str = "user") -> dict:
        run = self._run_row(run_id)
        if run["status"] in ("applied", "discarded"):
            raise Conflict(f"team run #{run_id} is already {run['status']}")
        self._cleanup(run)
        self._set(run_id, source, "team_run_discarded", status="discarded")
        return self.get_run(run_id)

    def _cleanup(self, run: dict) -> None:
        """Close the terminals and remove the run's worktrees and branches."""
        if run["tmux_session"]:
            terminal.kill_session(run["tmux_session"])
        repo = run["repo_path"]
        if not Path(repo).is_dir():
            return
        root = self._worktree_root(run)
        branches = [m["branch"] for m in self._members(run["id"])] + [f"team/{run['id']}"]
        for path in [Path(m["worktree"]) for m in self._members(run["id"])] + [root / "integration"]:
            if path.exists():
                _git(repo, "worktree", "remove", "--force", str(path), check=False)
        _git(repo, "worktree", "prune", check=False)
        for branch in branches:
            _git(repo, "branch", "-D", branch, check=False)
        try:
            root.rmdir()
            if self.settings.worktrees_dir is None:
                root.parent.rmdir()  # .project-os-worktrees, once no run uses it
        except OSError:
            pass
