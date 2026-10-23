import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from project_os import team as team_mod
from project_os.agents import AgentResult
from project_os.api import create_app
from project_os.config import Settings
from project_os.errors import Conflict
from project_os.team import Team, normalize_tasks
from tests.conftest import git, make_repo

PLAN = {
    "summary": "API e interfaccia in parallelo",
    "tasks": [
        {"agent": "claude", "title": "Endpoint", "details": "Aggiungi api.py", "depends_on": []},
        {"agent": "agy", "title": "Pagina", "details": "Aggiungi ui.py", "depends_on": [1]},
    ],
}


def result(text: str, status: str = "success") -> AgentResult:
    return AgentResult(agent="claude", status=status, text=text, session_id=None, duration_s=1.0, num_turns=1,
                       cost_usd=None, tokens={"input": None, "output": None}, exit_code=0, stderr="", cwd=".")


class FakeTerminal:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.sessions: dict[str, list] = {}
        self.screens: dict[str, list[str]] = {}
        self.entered: list[str] = []

    def open_session(self, name, panes):
        self.sessions[name] = panes
        return [f"%{i}" for i in range(len(panes))]

    def send(self, pane, text):
        self.sent.append((pane, text))

    def session_exists(self, name):
        return name in self.sessions

    def kill_session(self, name):
        self.sessions.pop(name, None)

    def open_window(self, name, terminal=None):
        return True

    def attach_command(self, name):
        return f"tmux attach -t {name}"

    def capture(self, pane, lines=12):
        return self.screens.get(pane, [])

    def press_enter(self, pane):
        self.entered.append(pane)


@pytest.fixture
def fake(monkeypatch):
    term = FakeTerminal()
    for name in ("open_session", "send", "session_exists", "kill_session", "open_window", "attach_command",
                 "capture", "press_enter"):
        monkeypatch.setattr(team_mod.terminal, name, getattr(term, name))
    monkeypatch.setattr(team_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    replies = {"plan": json.dumps(PLAN), "review": "Tutto a posto."}
    prompts = []

    def run(agent, prompt, cwd, mode="read", timeout_s=600, **_):
        prompts.append((prompt, str(cwd), mode))
        return result(replies["review" if "Review the combined change" in prompt else "plan"])

    monkeypatch.setattr(team_mod.agents, "run", run)
    term.replies, term.prompts = replies, prompts
    return term


@pytest.fixture
def setup(registry, workspace, tmp_path):
    repo = make_repo(workspace / "synth")
    registry.add_root(str(workspace))
    registry.scan()
    settings = Settings(db_path=tmp_path / "test.db", scan_interval_s=0, frontend_dist=tmp_path / "none",
                        exports_dir=tmp_path / "exports")
    return Team(registry.db, settings, registry), registry.resolve("synth"), repo


def branches(repo) -> set[str]:
    out = subprocess.run(["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
                         capture_output=True, text=True, check=True).stdout
    return set(out.split())


def commit_file(path, name, text):
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", f"add {name}")


def planned(team, pid, repo):
    run = team.create_run(pid, repo, "Aggiungi una pagina con la sua API")
    assert run["status"] == "planning"
    team.plan(run["id"])
    return team.get_run(run["id"])


def test_full_run(setup, fake):
    team, pid, repo = setup
    run = planned(team, pid, repo)
    assert run["status"] == "plan_ready"
    assert run["plan_summary"] == "API e interfaccia in parallelo"
    assert [(t["number"], t["agent"], t["depends_on"]) for t in run["tasks"]] == [(1, "claude", []), (2, "agy", [1])]
    plan_prompt, cwd, mode = fake.prompts[0]
    assert "Aggiungi una pagina con la sua API" in plan_prompt and mode == "read" and cwd == str(repo)

    run = team.start(run["id"])
    assert run["status"] == "running"
    members = {m["agent"]: m for m in run["members"]}
    wt_root = repo.parent / ".project-os-worktrees" / f"synth-{run['id']}"
    assert members["claude"]["worktree"] == str(wt_root / "claude")
    assert {f"team/{run['id']}-claude", f"team/{run['id']}-agy"} <= branches(repo)
    run_dir = team.team_dir / f"run-{run['id']}"
    claude_prompt = (run_dir / "prompt-claude.md").read_text()
    assert f"run_id={run['id']}" in claude_prompt and "Le tue attività: #1." in claude_prompt
    launcher = (run_dir / "launch-claude.sh").read_text()
    assert "--permission-mode acceptEdits" in launcher and "mcp__project-os__team_done" in launcher
    assert f"export PROJECT_OS_DB={team.settings.db_path}" in launcher
    assert "--mode accept-edits -i" in (run_dir / "launch-agy.sh").read_text()

    rid = run["id"]
    with pytest.raises(Conflict, match="assigned to agy"):
        team.update_task(rid, "claude", 2, "done")
    claude_wt, agy_wt = wt_root / "claude", wt_root / "agy"
    team.update_task(rid, "claude", 1, "doing")
    commit_file(claude_wt, "api.py", "def api(): ...\n")
    team.update_task(rid, "claude", 1, "done", "endpoint pronto")
    pane_agy = members["agy"]["pane"]
    note = [t for p, t in fake.sent if p == pane_agy][-1]
    assert "#1" in note and f"git merge team/{rid}-claude" in note and "Ora puoi procedere con #2" in note

    assert team.post_message(rid, "claude", "agy", "usa /api/x")["delivered"]
    assert fake.sent[-1] == (pane_agy, "[team] Messaggio da claude: usa /api/x")

    assert team.agent_done(rid, "claude", "fatto")["team_finished"] is False
    with pytest.raises(Conflict, match="#2"):
        team.agent_done(rid, "agy", "fatto")
    (agy_wt / "ui.py").write_text("print('ui')\n")  # left uncommitted on purpose
    for wt in (claude_wt, agy_wt):  # test output that differs between the two worktrees
        (wt / "__pycache__").mkdir()
        (wt / "__pycache__" / "api.cpython-313.pyc").write_bytes(str(wt).encode())
    team.update_task(rid, "agy", 2, "done")
    assert team.agent_done(rid, "agy", "pagina pronta")["team_finished"] is True
    assert team.pending_jobs() == [(rid, "integrate")]

    team.integrate(rid)
    run = team.get_run(rid)
    assert run["status"] == "review", run["error"]
    assert "api.py" in run["diffstat"] and "ui.py" in run["diffstat"]
    assert "__pycache__" not in run["diffstat"]
    assert run["review"] == "Tutto a posto."

    run = team.apply(rid)
    assert run["status"] == "applied"
    assert (repo / "api.py").exists() and (repo / "ui.py").exists()
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True).stdout.splitlines()
    assert log == [f"Team run #{rid}: Aggiungi una pagina con la sua API", "initial"]
    assert not any(b.startswith("team/") for b in branches(repo))
    assert not wt_root.exists() and not (repo.parent / ".project-os-worktrees").exists()
    assert f"pos-{rid}" not in fake.sessions


def test_members_waiting_for_the_user_are_flagged(setup, fake):
    team, pid, repo = setup
    rid = planned(team, pid, repo)["id"]
    team.start(rid)
    fake.screens = {"%0": ["● working"], "%1": ["project-os/team_board", "Allow calling this tool?", "> 1. Yes"]}
    waiting = {m["agent"]: m["waiting"] for m in team.get_run(rid)["members"]}
    assert waiting == {"claude": False, "agy": True}


def test_unsent_team_lines_are_submitted(setup, fake):
    team, pid, repo = setup
    rid = planned(team, pid, repo)["id"]
    team.start(rid)
    fake.screens = {
        "%0": ["● working", "❯ "],
        "%1": ["▸ [team] Messaggio da claude: primo (alt+m to send immediately)", "> [team] claude ha finito la sua parte"],
    }
    team.resend_unsent()
    assert fake.entered == ["%1"]


def test_conflicting_branches_stop_for_the_user(setup, fake):
    team, pid, repo = setup
    rid = planned(team, pid, repo)["id"]
    run = team.start(rid)
    for m in run["members"]:
        commit_file(type(repo)(m["worktree"]), "shared.py", f"# by {m['agent']}\n")
    team.finish_now(rid)
    team.integrate(rid)
    run = team.get_run(rid)
    assert run["status"] == "conflict"
    assert "shared.py" in run["error"]
    run = team.discard(rid)
    assert run["status"] == "discarded"
    assert not any(b.startswith("team/") for b in branches(repo))


def test_planner_failure_marks_the_run_failed(setup, fake):
    team, pid, repo = setup
    fake.replies["plan"] = "Non saprei."
    run = planned(team, pid, repo)
    assert run["status"] == "failed"
    assert "Pianificazione non riuscita" in run["error"]
    fake.replies["plan"] = "Ecco il piano:\n```json\n" + json.dumps(PLAN) + "\n```"
    team.replan(run["id"])
    team.plan(run["id"])
    assert team.get_run(run["id"])["status"] == "plan_ready"


def test_planner_sees_the_previous_run(setup, fake):
    team, pid, repo = setup
    first = planned(team, pid, repo)["id"]
    team.start(first)
    team.post_message(first, "claude", "user", "REPORT: manca la gestione degli errori in api.py")
    team.discard(first)
    planned(team, pid, repo)
    prompt = fake.prompts[-1][0]
    assert f"Previous team run on this project (#{first}, discarded)" in prompt
    assert "REPORT: manca la gestione degli errori in api.py" in prompt


def test_one_open_run_per_project(setup, fake):
    team, pid, repo = setup
    planned(team, pid, repo)
    with pytest.raises(Conflict, match="still open"):
        team.create_run(pid, repo, "altro")


def test_blocked_tasks_let_the_agent_finish(setup, fake):
    team, pid, repo = setup
    rid = planned(team, pid, repo)["id"]
    team.start(rid)
    team.update_task(rid, "claude", 1, "doing")
    with pytest.raises(Conflict, match="#1"):
        team.agent_done(rid, "claude", "fatto")
    team.update_task(rid, "claude", 1, "blocked", "manca una dipendenza")
    assert team.agent_done(rid, "claude", "bloccato su #1")["team_finished"] is False
    assert {m["agent"]: m["status"] for m in team.get_run(rid)["members"]}["claude"] == "done"


def test_apply_refuses_a_dirty_project(setup, fake):
    team, pid, repo = setup
    rid = planned(team, pid, repo)["id"]
    team.start(rid)
    team.finish_now(rid)
    team.integrate(rid)
    (repo / "main.py").write_text("changed\n")
    with pytest.raises(Conflict, match="uncommitted"):
        team.apply(rid)
    assert team.get_run(rid)["status"] == "review"


def test_apply_without_changes_closes_the_run(setup, fake):
    team, pid, repo = setup
    rid = planned(team, pid, repo)["id"]
    team.start(rid)
    team.finish_now(rid)
    team.integrate(rid)
    assert team.get_run(rid)["status"] == "review"
    head = lambda: subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
    before = head()
    assert team.apply(rid)["status"] == "applied"
    assert head() == before
    assert not {b for b in branches(repo) if b.startswith("team/")}


def test_edited_plan_keeps_dependencies_by_ref(setup, fake):
    team, pid, repo = setup
    rid = planned(team, pid, repo)["id"]
    run = team.update_plan(rid, [
        {"ref": 3, "agent": "agy", "title": "Nuova", "depends_on": []},
        {"ref": 2, "agent": "claude", "title": "Pagina", "depends_on": [3, 1]},  # 1 was removed
    ])
    assert [(t["number"], t["agent"], t["depends_on"]) for t in run["tasks"]] == [(1, "agy", []), (2, "claude", [1])]


def test_single_agent_plan_starts_one_pane(setup, fake):
    team, pid, repo = setup
    rid = planned(team, pid, repo)["id"]
    team.update_plan(rid, [{"agent": "claude", "title": "Tutto"}])
    run = team.start(rid)
    assert [m["agent"] for m in run["members"]] == ["claude"]
    assert len(fake.sessions[f"pos-{rid}"]) == 1
    assert "lavori da solo" in (team.team_dir / f"run-{rid}" / "prompt-claude.md").read_text()


def test_normalize_tasks_rejects_bad_input():
    with pytest.raises(Conflict, match="no tasks"):
        normalize_tasks([])
    with pytest.raises(Conflict, match="agent"):
        normalize_tasks([{"agent": "gpt", "title": "x"}])
    assert normalize_tasks([{"agent": "Claude", "title": "x", "depends_on": ["1", "zzz"]}])[0]["depends_on"] == []


def test_api_flow(tmp_path, fake):
    workspace = tmp_path / "projects"
    make_repo(workspace / "synth")
    settings = Settings(db_path=tmp_path / "api.db", scan_interval_s=0, frontend_dist=tmp_path / "none",
                        exports_dir=tmp_path / "exports")
    client = TestClient(create_app(settings))
    client.post("/api/roots", json={"path": str(workspace)})
    client.post("/api/scans")

    res = client.post("/api/projects/synth/team-runs", json={"request": "Fai una cosa"})
    assert res.status_code == 201
    rid = res.json()["id"]
    assert client.post("/api/projects/synth/team-runs", json={"request": "Altro"}).status_code == 409
    assert client.post(f"/api/team-runs/{rid}/start").status_code == 409  # still planning
    assert client.get("/api/projects/synth/team-runs").json()[0]["id"] == rid
    assert client.post(f"/api/team-runs/{rid}/discard").json()["status"] == "discarded"
