import json
import time

import pytest
from fastapi.testclient import TestClient

from project_os import analysis as analysis_mod
from project_os import resume
from project_os.agents import AgentError, AgentResult
from project_os.analysis import Analysis, merge_findings, normalize_action
from project_os.api import create_app
from project_os.config import Settings
from project_os.errors import Conflict
from tests.conftest import git, make_repo

EXTRACT = {
    "summary": "Una demo che stampa un saluto.",
    "claims": [
        {"claim": "main.py stampa 'hi'", "source": "README"},
        {"claim": "Usa PostgreSQL", "source": "decisione #1"},
        {"claim": "Ha una CLI", "source": "README"},
    ],
}
VERIFY = {
    "verdicts": [
        {"n": 1, "verdict": "confirmed", "explanation": "print('hi')", "refs": ["main.py:1"]},
        {"n": 2, "verdict": "contradicted", "explanation": "Nessun driver PostgreSQL", "refs": []},
        # claim 3 is skipped by the verifier
    ],
    "suggestions": [
        {"action": {"type": "phase", "value": "prototype"}, "reason": "Un solo file", "refs": ["main.py"]},
        {"action": {"type": "link", "target": "other", "kind": "related"}, "reason": "Stesso tema"},
        {"action": {"type": "phase", "value": "bogus"}, "reason": "scartata"},
        {"action": {"type": "link", "target": "nope"}, "reason": "progetto inesistente"},
        {"action": {"type": "next_step", "value": "Aggiungere i test"}, "reason": "Nessun test"},
    ],
}


def result(agent: str, text: str, status: str = "success") -> AgentResult:
    return AgentResult(agent=agent, status=status, text=text, session_id=None, duration_s=1.0, num_turns=1,
                       cost_usd=0.01 if agent == "claude" else None, tokens={"input": 10, "output": 5},
                       exit_code=0, stderr="", cwd=".")


@pytest.fixture
def agents(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    replies = {"agy": json.dumps(EXTRACT), "claude-extract": json.dumps(EXTRACT), "claude": json.dumps(VERIFY)}
    status = {"agy": "success", "claude": "success"}

    def run(agent, prompt, cwd, mode="read", timeout_s=600, json_schema=None, **_):
        calls.append((agent, "extract" if "Stai preparando" in prompt else "verify", mode))
        # Only claude is given a schema: agy's structured output loses the reply.
        assert (json_schema is not None) == (agent == "claude")
        if replies.get(agent) is AgentError:
            raise AgentError(f"{agent} executable not found on PATH")
        key = f"{agent}-extract" if agent == "claude" and calls[-1][1] == "extract" else agent
        return result(agent, replies[key], status[agent])

    monkeypatch.setattr(analysis_mod.agents, "run", run)
    return {"calls": calls, "replies": replies, "status": status}


@pytest.fixture
def setup(registry, workspace):
    make_repo(workspace / "demo")
    make_repo(workspace / "other")
    registry.add_root(str(workspace))
    registry.scan()
    registry.memory.record_decision(registry.resolve("demo"), "Usa PostgreSQL")
    return registry, Analysis(registry.db, registry), workspace


def test_extract_then_verify(setup, agents):
    registry, analysis, _ = setup
    pid = registry.resolve("demo")
    a = analysis.request(pid)
    assert a["status"] == "queued"
    analysis.run(a["id"])

    a = analysis.latest(pid)
    assert a["status"] == "done" and a["current"] is True
    assert a["summary"] == EXTRACT["summary"]
    assert [(c[0], c[1], c[2]) for c in agents["calls"]] == [("agy", "extract", "read"), ("claude", "verify", "read")]
    assert [s["agent"] for s in a["steps"]] == ["agy", "claude"]
    assert a["steps"][1]["cost_usd"] == 0.01

    kinds = [(f["kind"], f["text"]) for f in a["findings"]]
    assert kinds[:3] == [("confirmed", "main.py stampa 'hi'"), ("contradiction", "Usa PostgreSQL"),
                         ("unverifiable", "Ha una CLI")]
    assert a["findings"][2]["detail"] == "Il verificatore non l'ha esaminata."
    # Only the suggestions Project OS can apply survive.
    assert [f["action"]["type"] for f in a["findings"] if f["kind"] == "suggestion"] == ["phase", "link", "next_step"]

    ev = registry.get_project(pid)["evidence"]["agent_analysis"]
    assert ev["payload"]["contradictions"] == ["Usa PostgreSQL"]
    assert registry.list_events(project_id=pid, limit=1)[0]["event_type"] == "analysis_done"


def test_unchanged_project_is_not_analysed_again(setup, agents):
    registry, analysis, workspace = setup
    pid = registry.resolve("demo")
    first = analysis.request(pid)
    with pytest.raises(Conflict, match="already running"):
        analysis.request(pid, force=True)
    analysis.run(first["id"])
    with pytest.raises(Conflict, match="Nothing changed"):
        analysis.request(pid)
    assert analysis.request(pid, force=True)["status"] == "queued"


def test_new_commit_makes_the_analysis_stale(setup, agents):
    registry, analysis, workspace = setup
    pid = registry.resolve("demo")
    analysis.run(analysis.request(pid)["id"])
    (workspace / "demo" / "cli.py").write_text("pass\n")
    git(workspace / "demo", "add", ".")
    git(workspace / "demo", "commit", "-q", "-m", "cli")
    assert analysis.latest(pid)["current"] is False
    assert analysis.request(pid)["status"] == "queued"


def test_claude_extracts_when_agy_is_missing(setup, agents):
    registry, analysis, _ = setup
    agents["replies"]["agy"] = AgentError
    pid = registry.resolve("demo")
    analysis.run(analysis.request(pid)["id"])
    a = analysis.latest(pid)
    assert a["status"] == "done"
    assert [(s["agent"], s["role"], s["status"]) for s in a["steps"]] == [
        ("agy", "extract", "error"), ("claude", "extract", "success"), ("claude", "verify", "success")]
    assert a["steps"][0]["fallback"] == "claude"


def test_failed_verification(setup, agents):
    registry, analysis, _ = setup
    agents["status"]["claude"] = "timeout"
    pid = registry.resolve("demo")
    analysis.run(analysis.request(pid)["id"])
    a = analysis.latest(pid)
    assert a["status"] == "failed" and "timeout" in a["error"]
    assert a["findings"] == []
    assert "agent_analysis" not in registry.get_project(pid)["evidence"]


def test_recover_fails_interrupted_runs(setup, agents):
    registry, analysis, _ = setup
    pid = registry.resolve("demo")
    a = analysis.request(pid)
    analysis._update(a["id"], status="running")
    analysis.recover()
    assert analysis.get(a["id"])["status"] == "failed"
    assert analysis.pending() == []


def test_accepting_findings_changes_declared_state(setup, agents):
    registry, analysis, _ = setup
    pid = registry.resolve("demo")
    analysis.run(analysis.request(pid)["id"])
    by_text = {f["text"]: f for f in analysis.latest(pid)["findings"]}

    phase = next(f for f in by_text.values() if f["action"] and f["action"]["type"] == "phase")
    assert analysis.resolve(phase["id"], accept=True)["status"] == "accepted"
    assert registry.get_project(pid)["phase"] == "prototype"

    link = next(f for f in by_text.values() if f["action"] and f["action"]["type"] == "link")
    analysis.resolve(link["id"], accept=True)
    assert registry.relations.relations(pid)[0]["target"] == "other"

    step = next(f for f in by_text.values() if f["action"] and f["action"]["type"] == "next_step")
    analysis.resolve(step["id"], accept=True)
    assert registry.memory.snapshot(pid)["next_step"] == "Aggiungere i test"

    contradiction = by_text["Usa PostgreSQL"]
    assert analysis.resolve(contradiction["id"], accept=False)["status"] == "dismissed"
    with pytest.raises(Conflict, match="already dismissed"):
        analysis.resolve(contradiction["id"], accept=True)
    with pytest.raises(Conflict, match="only contradictions and suggestions"):
        analysis.resolve(by_text["main.py stampa 'hi'"]["id"], accept=True)


def test_brief_carries_the_analysis_as_ai_inferences(setup, agents):
    registry, analysis, _ = setup
    pid = registry.resolve("demo")
    analysis.run(analysis.request(pid)["id"])
    brief = resume.build_brief(registry, pid)
    assert resume.validate(brief) == []
    texts = [i["text"] for i in brief.manifest["state"]["inferences"] if i["method"] == "ai"]
    assert EXTRACT["summary"] in texts
    assert any(t.startswith("Possible contradiction: Usa PostgreSQL") for t in texts)


def test_normalize_action():
    slugs = {"a", "b"}
    assert normalize_action({"type": "lifecycle", "value": "paused"}, slugs, "a") == {"type": "lifecycle", "value": "paused"}
    assert normalize_action({"type": "link", "target": "a"}, slugs, "a") is None  # not to itself
    assert normalize_action({"type": "link", "target": "b", "kind": "weird"}, slugs, "a")["kind"] == "related"
    assert normalize_action({"type": "decision", "title": " "}, slugs, "a") is None
    assert normalize_action({"type": "rm -rf"}, slugs, "a") is None
    assert merge_findings([{"claim": "x", "source": None}], {"verdicts": [{"n": 9, "verdict": "confirmed"}]},
                          slugs, "a")[0]["kind"] == "unverifiable"


def test_api_and_background_loop(tmp_path, workspace, agents):
    make_repo(workspace / "demo")
    settings = Settings(db_path=tmp_path / "api.db", scan_interval_s=0, frontend_dist=tmp_path / "none",
                        exports_dir=tmp_path / "exports")
    with TestClient(create_app(settings)) as client:
        client.post("/api/roots", json={"path": str(workspace)})
        client.post("/api/scans")
        assert client.get("/api/projects/demo/analysis").json() is None
        r = client.post("/api/projects/demo/analyses", json={})
        assert r.status_code == 201
        analysis_id = r.json()["id"]
        for _ in range(50):
            if client.get(f"/api/analyses/{analysis_id}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        latest = client.get("/api/projects/demo/analysis").json()
        assert latest["status"] == "done" and latest["current"]
        assert client.post("/api/projects/demo/analyses", json={}).status_code == 409
        suggestion = next(f for f in latest["findings"] if f["kind"] == "suggestion")
        assert client.post(f"/api/findings/{suggestion['id']}/dismiss").json()["status"] == "dismissed"
        assert client.post(f"/api/findings/{suggestion['id']}/accept").status_code == 409
        assert len(client.get("/api/projects/demo/analyses").json()) == 1
