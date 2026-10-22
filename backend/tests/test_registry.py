import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from project_os import discovery
from project_os.registry import Conflict
from tests.conftest import git, make_repo


def event_types(registry, project_id=None):
    return [e["event_type"] for e in registry.list_events(project_id=project_id)]


def test_discovers_git_and_plain_projects(registry, workspace):
    make_repo(workspace / "alpha")
    make_repo(workspace / "group" / "beta")  # nested repo; 'group' only groups repos
    (workspace / "patch").mkdir()
    (workspace / "patch" / "synth.pd").write_text("#N canvas;\n")
    (workspace / "empty").mkdir()
    (workspace / "node_modules" / "x").mkdir(parents=True)

    registry.add_root(str(workspace))
    run = registry.scan()

    assert run["status"] == "ok"
    projects = {p["name"]: p for p in registry.list_projects()}
    assert set(projects) == {"alpha", "beta", "patch"}
    assert projects["alpha"]["repositories"][0]["vcs"] == "git"
    assert projects["alpha"]["readme_title"] == "Demo"
    assert projects["alpha"]["readme_summary"] == "A demo project."
    assert projects["alpha"]["git"]["branch"] == "main"
    assert projects["patch"]["repositories"][0]["vcs"] == "none"
    assert projects["patch"]["stack"] == ["puredata"]


def test_rescan_without_changes_writes_nothing(registry, workspace):
    make_repo(workspace / "alpha")
    registry.add_root(str(workspace))
    registry.scan()
    evidence = registry.db.query_one("SELECT COUNT(*) FROM evidence")[0]
    events = registry.db.query_one("SELECT COUNT(*) FROM events")[0]

    run = registry.scan()

    assert run["stats"]["unchanged"] == 1
    assert registry.db.query_one("SELECT COUNT(*) FROM evidence")[0] == evidence
    assert registry.db.query_one("SELECT COUNT(*) FROM events")[0] == events
    assert registry.db.query_one("SELECT COUNT(*) FROM projects")[0] == 1


def test_commit_and_dirty_worktree_produce_events(registry, workspace):
    repo = make_repo(workspace / "alpha")
    registry.add_root(str(workspace))
    registry.scan()
    pid = registry.list_projects()[0]["id"]

    (repo / "main.py").write_text("print('changed')\n")
    registry.scan()
    assert "worktree_changed" in event_types(registry, pid)

    git(repo, "commit", "-qam", "second commit")
    registry.scan()
    head_events = [e for e in registry.list_events(project_id=pid) if e["event_type"] == "git_head_changed"]
    assert head_events[0]["payload"]["subject"] == "second commit"


def test_failed_observation_keeps_previous_state(registry, workspace):
    make_repo(workspace / "alpha")
    registry.add_root(str(workspace))
    registry.scan()
    before = registry.list_projects()[0]

    with mock.patch.object(discovery, "observe", side_effect=RuntimeError("boom")):
        run = registry.scan()

    assert run["status"] == "partial"
    assert "boom" in run["errors"][0]["error"]
    after = registry.list_projects()[0]
    assert after["git"] == before["git"]
    assert after["last_observed_at"] == before["last_observed_at"]
    assert after["health"] != "missing"


def test_unreadable_root_does_not_mark_projects_missing(registry, workspace):
    make_repo(workspace / "alpha")
    registry.add_root(str(workspace))
    registry.scan()

    with mock.patch.object(discovery, "find_candidates", side_effect=PermissionError("denied")):
        run = registry.scan()

    assert run["status"] == "failed"
    assert registry.list_projects()[0]["health"] != "missing"


def test_missing_and_restored_repository(registry, workspace, tmp_path):
    repo = make_repo(workspace / "alpha")
    registry.add_root(str(workspace))
    registry.scan()

    moved = shutil.move(repo, tmp_path / "elsewhere")
    run = registry.scan()
    assert run["stats"]["missing"] == 1
    project = registry.list_projects()[0]
    assert project["health"] == "missing"

    shutil.move(moved, repo)
    run = registry.scan()
    assert run["stats"]["restored"] == 1
    assert registry.list_projects()[0]["health"] != "missing"
    assert registry.db.query_one("SELECT COUNT(*) FROM projects")[0] == 1


def test_scanner_never_overwrites_user_fields(registry, workspace):
    repo = make_repo(workspace / "alpha")
    registry.add_root(str(workspace))
    registry.scan()
    pid = registry.list_projects()[0]["id"]
    registry.update_project(pid, name="Alpha Prime", lifecycle="paused", phase="prototype", priority=2)

    (repo / "README.md").write_text("# Renamed upstream\n\nNew text.\n")
    registry.scan()

    p = registry.get_project(pid)
    assert (p["name"], p["lifecycle"], p["phase"], p["priority"]) == ("Alpha Prime", "paused", "prototype", 2)
    assert p["readme_title"] == "Renamed upstream"
    assert "readme_changed" in event_types(registry, pid)
    updated = next(e for e in p["events"] if e["event_type"] == "project_updated")
    assert updated["payload"]["lifecycle"] == {"from": "active", "to": "paused"}


def test_search_matches_readme_text(registry, workspace):
    make_repo(workspace / "alpha", readme="# Alpha\n\nGranular synthesis engine.\n")
    make_repo(workspace / "beta", readme="# Beta\n\nTrading bot.\n")
    registry.add_root(str(workspace))
    registry.scan()

    assert [p["name"] for p in registry.list_projects(q="granul")] == ["alpha"]
    assert registry.list_projects(q='"; DROP TABLE projects; --') == []


def test_manual_project_without_repository(registry):
    p = registry.create_project("Album concept", description="Ten tracks", phase="idea")
    assert p["origin"] == "manual"
    assert p["repositories"] == []
    assert p["health"] == "unknown"
    assert [x["name"] for x in registry.list_projects(q="album")] == ["Album concept"]

    registry.delete_project(p["id"])
    assert registry.list_projects() == []


def test_discovered_project_cannot_be_deleted_while_present(registry, workspace):
    make_repo(workspace / "alpha")
    registry.add_root(str(workspace))
    registry.scan()
    with pytest.raises(Conflict):
        registry.delete_project(registry.list_projects()[0]["id"])


def test_secrets_are_never_read(registry, workspace):
    repo = make_repo(workspace / "alpha")
    (repo / ".env").write_text("API_KEY=supersecret\n")
    git(repo, "remote", "add", "origin", "https://user:ghp_token123@github.com/u/alpha.git")
    registry.add_root(str(workspace))
    registry.scan()

    dump = json.dumps([dict(r) for r in registry.db.query("SELECT * FROM evidence")])
    dump += json.dumps([dict(r) for r in registry.db.query("SELECT * FROM repositories")])
    assert "supersecret" not in dump
    assert "ghp_token123" not in dump
    assert "https://github.com/u/alpha.git" in dump


def test_empty_repository_is_not_an_error(registry, workspace):
    (workspace / "fresh").mkdir()
    git(workspace / "fresh", "init", "-q")
    registry.add_root(str(workspace))
    run = registry.scan()
    assert run["status"] == "ok"
    assert registry.list_projects()[0]["git"]["head"] is None


def test_duplicate_root_rejected(registry, workspace):
    registry.add_root(str(workspace))
    with pytest.raises(Conflict):
        registry.add_root(str(workspace) + "/")


def test_state_survives_restart(tmp_path, workspace):
    from project_os.db import Database
    from project_os.registry import Registry

    make_repo(workspace / "alpha")
    first = Registry(Database(tmp_path / "persist.db"))
    first.add_root(str(workspace))
    first.scan()
    first.update_project(first.list_projects()[0]["id"], lifecycle="paused")
    first.db.close()

    second = Registry(Database(tmp_path / "persist.db"))
    assert second.list_projects()[0]["lifecycle"] == "paused"
    assert second.scan()["stats"]["unchanged"] == 1
