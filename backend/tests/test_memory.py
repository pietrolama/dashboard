import pytest
from fastapi.testclient import TestClient

from project_os.api import create_app
from project_os.config import Settings
from project_os.db import Database
from project_os.errors import Conflict, NotFound
from project_os.registry import Registry


@pytest.fixture
def project(registry):
    return registry.create_project("Synth")["id"]


def events(registry, pid):
    return [e["event_type"] for e in registry.list_events(project_id=pid)]


def test_record_and_supersede_decision(registry, project):
    memory = registry.memory
    old = memory.record_decision(project, "Use SQLite", rationale="local-first", alternatives="Postgres")
    new = memory.record_decision(project, "Use SQLite + FTS5", supersedes_id=old["id"])

    old = memory.get_decision(old["id"])
    assert old["status"] == "superseded"
    assert old["superseded_by_id"] == new["id"]
    assert new["supersedes_id"] == old["id"]
    assert [d["id"] for d in memory.list_decisions(project, include_superseded=False)] == [new["id"]]
    assert {"decision_recorded", "decision_superseded"} <= set(events(registry, project))

    with pytest.raises(Conflict):
        memory.record_decision(project, "Again", supersedes_id=old["id"])
    with pytest.raises(Conflict):
        memory.update_decision(old["id"], title="Rewrite history")


def test_supersede_must_stay_in_project(registry, project):
    other = registry.create_project("Other")["id"]
    d = registry.memory.record_decision(other, "Elsewhere")
    with pytest.raises(Conflict):
        registry.memory.record_decision(project, "Steal", supersedes_id=d["id"])
    assert registry.memory.get_decision(d["id"])["status"] == "active"


def test_decision_edit_and_review(registry, project):
    memory = registry.memory
    d = memory.record_decision(project, "Keep modules small", kind="constraint")
    memory.update_decision(d["id"], status="review")
    assert memory.snapshot(project)["decisions_review"] == 1
    with pytest.raises(Conflict):
        memory.update_decision(d["id"], status="superseded")
    with pytest.raises(Conflict):
        memory.update_decision(d["id"], title="   ")

    edited = memory.update_decision(d["id"], rationale="  easier to test  ")
    assert edited["rationale"] == "easier to test"
    assert "decision_edited" in events(registry, project)
    # A no-op edit writes nothing.
    n = len(events(registry, project))
    memory.update_decision(d["id"], rationale="easier to test")
    assert len(events(registry, project)) == n


def test_constraints_listed_first(registry, project):
    memory = registry.memory
    memory.record_decision(project, "A decision")
    c = memory.record_decision(project, "A constraint", kind="constraint")
    assert memory.list_decisions(project)[0]["id"] == c["id"]


def test_session_lifecycle_feeds_snapshot(registry, project):
    memory = registry.memory
    s = memory.start_session(project, "Fix the envelope")
    assert memory.snapshot(project)["open_session"]["id"] == s["id"]
    with pytest.raises(Conflict):
        memory.start_session(project, "Second one")

    memory.end_session(s["id"], outcome="Done", open_questions=["Linear or exp?", " "], next_step="Add tests")
    snap = memory.snapshot(project)
    assert snap["open_session"] is None
    assert snap["next_step"] == "Add tests"
    assert snap["open_questions"] == ["Linear or exp?"]
    with pytest.raises(Conflict):
        memory.end_session(s["id"])

    # The latest finished session wins.
    memory.log_session(project, "Write tests", next_step="Ship")
    assert memory.snapshot(project)["next_step"] == "Ship"
    assert registry.list_projects()[0]["memory"]["next_step"] == "Ship"


def test_session_correction(registry, project):
    memory = registry.memory
    s = memory.log_session(project, "Goal", open_questions=["q1"])
    s = memory.update_session(s["id"], open_questions=["q1", "q2"], next_step="later")
    assert s["open_questions"] == ["q1", "q2"]
    assert "session_edited" in events(registry, project)
    with pytest.raises(Conflict):
        memory.update_session(s["id"], ended_at="2020-01-01")


def test_memory_survives_restart(tmp_path):
    first = Registry(Database(tmp_path / "m.db"))
    pid = first.create_project("Persist")["id"]
    first.memory.record_decision(pid, "Decided")
    first.memory.log_session(pid, "Worked", open_questions=["Open?"], next_step="Continue")
    first.db.close()

    second = Registry(Database(tmp_path / "m.db"))
    snap = second.memory.snapshot(pid)
    assert (snap["next_step"], snap["open_questions"], snap["decisions_active"]) == ("Continue", ["Open?"], 1)


def test_unknown_project(registry):
    with pytest.raises(NotFound):
        registry.memory.record_decision(999, "x")


def test_api_flow(tmp_path):
    settings = Settings(db_path=tmp_path / "api.db", scan_interval_s=0, frontend_dist=tmp_path / "none",
                        exports_dir=tmp_path / "exports")
    with TestClient(create_app(settings)) as client:
        client.post("/api/projects", json={"name": "Live set"})

        r = client.post("/api/projects/live-set/decisions", json={"title": "Ableton only", "kind": "constraint"})
        assert r.status_code == 201
        did = r.json()["id"]
        r = client.post("/api/projects/live-set/decisions", json={"title": "Bitwig", "supersedes_id": did})
        assert r.status_code == 201
        assert client.patch(f"/api/decisions/{did}", json={"title": "x"}).status_code == 409
        assert client.patch(f"/api/decisions/{r.json()['id']}", json={"status": "superseded"}).status_code == 422

        s = client.post("/api/projects/live-set/sessions", json={"goal": "Arrange intro"}).json()
        assert client.post("/api/projects/live-set/sessions", json={"goal": "again"}).status_code == 409
        ended = client.post(f"/api/sessions/{s['id']}/end",
                            json={"outcome": "ok", "open_questions": ["BPM?"], "next_step": "Mix"}).json()
        assert ended["ended_at"]

        detail = client.get("/api/projects/live-set").json()
        assert detail["memory"]["next_step"] == "Mix"
        assert len(detail["decisions"]) == 2
        assert detail["sessions"][0]["open_questions"] == ["BPM?"]


def test_cli(tmp_path, monkeypatch, capsys):
    from project_os.__main__ import main

    monkeypatch.setenv("PROJECT_OS_DB", str(tmp_path / "cli.db"))
    Registry(Database(tmp_path / "cli.db")).create_project("Agent work")

    assert main(["decide", "agent-work", "Use FastAPI", "--why", "simple", "--source", "agent"]) == 0
    assert main(["session-start", "agent-work", "Implement API"]) == 0
    assert main(["session-end", "agent-work", "--next", "Write docs", "--question", "Auth?"]) == 0
    assert main(["session-end", "agent-work"]) == 1
    capsys.readouterr()
    assert main(["memory", "agent-work"]) == 0
    out = capsys.readouterr().out
    assert "Next step: Write docs" in out and "? Auth?" in out and "Use FastAPI" in out
    assert main(["decide", "nope", "x"]) == 1
