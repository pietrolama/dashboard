"""Tests for the Project OS MCP Server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from project_os.config import Settings
from project_os.db import Database
from project_os.mcp_server import create_mcp_server
from project_os.registry import Registry

from .conftest import make_repo


@pytest.fixture
def mcp_setup(tmp_path: Path):
    db_path = tmp_path / "test_mcp.db"
    exports_dir = tmp_path / "exports"
    db = Database(db_path)
    settings = Settings(
        db_path=db_path,
        scan_interval_s=0,
        frontend_dist=tmp_path / "frontend",
        exports_dir=exports_dir,
    )
    server, database = create_mcp_server(settings=settings, db=db)
    yield server, database, settings, tmp_path
    database.close()


@pytest.mark.anyio
async def test_server_init_and_close(tmp_path: Path):
    db_path = tmp_path / "init_test.db"
    db = Database(db_path)
    settings = Settings(
        db_path=db_path,
        scan_interval_s=0,
        frontend_dist=tmp_path / "frontend",
        exports_dir=tmp_path / "exports",
    )
    server, database = create_mcp_server(settings=settings, db=db)
    assert server.name == "project-os"

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    expected = {
        "list_projects",
        "get_project_context",
        "get_project_memory",
        "record_decision",
        "log_work_session",
        "preview_resume",
        "export_resume",
        "scan_projects",
        "link_projects",
    }
    assert expected.issubset(tool_names)
    database.close()


@pytest.mark.anyio
async def test_list_and_context_and_memory(mcp_setup):
    server, db, settings, tmp_path = mcp_setup
    registry = Registry(db)
    proj = registry.create_project("Alpha Test", description="Alpha project description", priority=2)
    slug = proj["slug"]

    # 1. list_projects
    projects_res = await server.call_tool("list_projects", {})
    assert not projects_res.is_error
    projects_data = json.loads(projects_res.content[0].text)
    assert projects_data["total"] == 1
    assert len(projects_data["projects"]) == 1
    assert projects_data["projects"][0]["slug"] == slug
    assert projects_data["projects"][0]["name"] == "Alpha Test"

    # Filter by lifecycle
    filtered = await server.call_tool("list_projects", {"lifecycle": "paused"})
    assert json.loads(filtered.content[0].text)["total"] == 0

    # 2. get_project_context
    ctx = await server.call_tool("get_project_context", {"project": slug})
    assert not ctx.is_error
    ctx_data = json.loads(ctx.content[0].text)
    assert ctx_data["name"] == "Alpha Test"
    assert "evidence" in ctx_data
    assert "events" in ctx_data

    # By ID as well
    ctx_by_id = await server.call_tool("get_project_context", {"project": str(proj["id"])})
    assert not ctx_by_id.is_error
    assert json.loads(ctx_by_id.content[0].text)["name"] == "Alpha Test"

    # 3. get_project_memory (initially empty)
    mem = await server.call_tool("get_project_memory", {"project": slug})
    assert not mem.is_error
    mem_data = json.loads(mem.content[0].text)
    assert mem_data["decisions"] == []
    assert mem_data["sessions"] == []
    assert mem_data["snapshot"]["open_session"] is None


@pytest.mark.anyio
async def test_record_decision_and_log_work_session(mcp_setup):
    server, db, settings, tmp_path = mcp_setup
    registry = Registry(db)
    proj = registry.create_project("Beta Test")
    slug = proj["slug"]

    # Record decision with source="agent"
    dec_res = await server.call_tool(
        "record_decision",
        {
            "project": slug,
            "title": "Use SQLite WAL mode",
            "rationale": "High concurrency read performance",
            "alternatives": "Postgres, MySQL",
            "kind": "decision",
        },
    )
    assert not dec_res.is_error
    dec = json.loads(dec_res.content[0].text)
    assert dec["title"] == "Use SQLite WAL mode"
    assert dec["kind"] == "decision"
    assert dec["status"] == "active"

    # Verify event source is 'agent'
    events = registry.list_events(project_id=proj["id"])
    dec_event = next(e for e in events if e["event_type"] == "decision_recorded")
    assert dec_event["source"] == "agent"

    # Record constraint superseding previous decision
    const_res = await server.call_tool(
        "record_decision",
        {
            "project": slug,
            "title": "Strict SQLite only",
            "rationale": "No remote DB dependencies allowed",
            "kind": "constraint",
            "supersedes_id": dec["id"],
        },
    )
    assert not const_res.is_error
    const_dec = json.loads(const_res.content[0].text)
    assert const_dec["kind"] == "constraint"
    assert const_dec["supersedes_id"] == dec["id"]

    # Log work session
    sess_res = await server.call_tool(
        "log_work_session",
        {
            "project": slug,
            "goal": "Implement persistence layer",
            "outcome": "Added WAL mode migrations",
            "open_questions": ["Is fsync required on checkpoint?"],
            "next_step": "Implement backup snapshot",
        },
    )
    assert not sess_res.is_error
    sess = json.loads(sess_res.content[0].text)
    assert sess["goal"] == "Implement persistence layer"
    assert sess["ended_at"] is not None
    assert sess["open_questions"] == ["Is fsync required on checkpoint?"]

    # Verify session event source is 'agent'
    events = registry.list_events(project_id=proj["id"])
    sess_event = next(e for e in events if e["event_type"] == "session_ended")
    assert sess_event["source"] == "agent"

    # Verify in memory tool
    mem = await server.call_tool("get_project_memory", {"project": slug, "include_superseded_decisions": True})
    mem_data = json.loads(mem.content[0].text)
    assert len(mem_data["decisions"]) == 2
    assert len(mem_data["sessions"]) == 1
    assert mem_data["snapshot"]["next_step"] == "Implement backup snapshot"


@pytest.mark.anyio
async def test_preview_resume_no_disk_writes(mcp_setup):
    server, db, settings, tmp_path = mcp_setup
    repo_path = make_repo(tmp_path / "repo_preview")
    registry = Registry(db)
    proj = registry.create_project("Preview Test", path=str(repo_path))
    slug = proj["slug"]

    # Before preview, exports_dir is empty or does not exist
    if settings.exports_dir.exists():
        assert list(settings.exports_dir.iterdir()) == []

    # Run preview_resume
    res = await server.call_tool("preview_resume", {"project": slug, "refresh": False})
    assert not res.is_error
    data = json.loads(res.content[0].text)
    assert "brief_id" in data
    assert "manifest" in data
    assert "markdown" in data
    assert data["manifest"]["protocol"]["name"] == "project-resume-protocol"
    assert "Preview Test" in data["markdown"]

    # Assert no files were written to exports_dir or repo
    if settings.exports_dir.exists():
        assert list(settings.exports_dir.iterdir()) == []
    assert not (repo_path / "RESUME.md").exists()
    assert not (repo_path / "manifest.json").exists()


@pytest.mark.anyio
async def test_export_resume_and_verify(mcp_setup):
    server, db, settings, tmp_path = mcp_setup
    repo_path = make_repo(tmp_path / "repo_export")
    registry = Registry(db)
    proj = registry.create_project("Export Test", path=str(repo_path))
    slug = proj["slug"]

    # Run export_resume
    res = await server.call_tool("export_resume", {"project": slug})
    assert not res.is_error
    data = json.loads(res.content[0].text)
    export_path = Path(data["export_path"])

    # Path must be inside settings.exports_dir
    assert export_path.resolve().is_relative_to(settings.exports_dir.resolve())
    assert (export_path / "RESUME.md").exists()
    assert (export_path / "manifest.json").exists()
    assert (export_path / "evidence.json").exists()
    assert (export_path / "SHA256SUMS").exists()

    # Verify event source
    events = registry.list_events(project_id=proj["id"])
    export_event = next(e for e in events if e["event_type"] == "brief_exported")
    assert export_event["source"] == "agent"

    # Verify repo directory was NOT modified
    assert not (repo_path / "RESUME.md").exists()


@pytest.mark.anyio
async def test_scan_projects_authorized_roots(mcp_setup):
    server, db, settings, tmp_path = mcp_setup
    root_dir = tmp_path / "repos"
    root_dir.mkdir()
    make_repo(root_dir / "scanned_p1")
    make_repo(root_dir / "scanned_p2")

    registry = Registry(db)
    registry.add_root(str(root_dir), max_depth=2)

    scan_res = await server.call_tool("scan_projects", {})
    assert not scan_res.is_error
    scan_data = json.loads(scan_res.content[0].text)
    assert scan_data["status"] == "ok"
    assert scan_data["stats"]["candidates"] == 2
    assert scan_data["stats"]["new_projects"] == 2

    # Verify projects are present
    projects = await server.call_tool("list_projects", {})
    projects_data = json.loads(projects.content[0].text)
    slugs = {p["slug"] for p in projects_data["projects"]}
    assert {"scanned-p1", "scanned-p2"}.issubset(slugs)


@pytest.mark.anyio
async def test_error_handling_not_found_and_invalid_input(mcp_setup):
    server, db, settings, tmp_path = mcp_setup

    # 1. Non-existent project
    with pytest.raises(ToolError):
        await server.call_tool("get_project_context", {"project": "non-existent-project"})

    # 2. Empty title on decision
    registry = Registry(db)
    proj = registry.create_project("Error Test")
    with pytest.raises(ToolError):
        await server.call_tool("record_decision", {"project": proj["slug"], "title": "   "})

    # 3. Invalid supersedes_id
    with pytest.raises(ToolError):
        await server.call_tool(
            "record_decision",
            {"project": proj["slug"], "title": "New", "supersedes_id": 99999},
        )

    # 4. Empty goal on session
    with pytest.raises(ToolError):
        await server.call_tool("log_work_session", {"project": proj["slug"], "goal": ""})


@pytest.mark.anyio
async def test_mcp_stdio_client_roundtrip(tmp_path: Path):
    """End-to-end integration test running the MCP server process over stdio."""
    db_path = tmp_path / "e2e_mcp.db"
    exports_dir = tmp_path / "e2e_exports"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "project_os", "mcp"],
        env={
            # The backend directory, wherever pytest is started from.
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PROJECT_OS_DB": str(db_path),
            "PROJECT_OS_EXPORTS": str(exports_dir),
        },
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # 1. List tools
            tools_resp = await session.list_tools()
            tool_names = [t.name for t in tools_resp.tools]
            assert "list_projects" in tool_names
            assert "record_decision" in tool_names
            assert "log_work_session" in tool_names
            assert "preview_resume" in tool_names
            assert "export_resume" in tool_names

            # 2. Add a project directly in DB so we can test tools over stdio
            db = Database(db_path)
            reg = Registry(db)
            p = reg.create_project("E2E Test Project")
            db.close()

            # 3. Call list_projects
            list_res = await session.call_tool("list_projects", {})
            assert not list_res.is_error
            projects_data = json.loads(list_res.content[0].text)
            assert projects_data["total"] == 1
            assert projects_data["projects"][0]["slug"] == "e2e-test-project"

            # 4. Call record_decision over stdio
            dec_res = await session.call_tool(
                "record_decision",
                {
                    "project": "e2e-test-project",
                    "title": "MCP stdio verified",
                    "rationale": "Direct JSON-RPC roundtrip validated",
                },
            )
            assert not dec_res.is_error
            dec_data = json.loads(dec_res.content[0].text)
            assert dec_data["title"] == "MCP stdio verified"

            # 5. Call preview_resume over stdio
            preview_res = await session.call_tool("preview_resume", {"project": "e2e-test-project"})
            assert not preview_res.is_error
            preview_data = json.loads(preview_res.content[0].text)
            assert preview_data["valid"] is True
            assert preview_data["manifest"]["protocol"]["name"] == "project-resume-protocol"

            # 6. Test error handling over stdio
            err_res = await session.call_tool("get_project_context", {"project": "missing-proj"})
            assert err_res.is_error
            assert "NotFound" in err_res.content[0].text or "not found" in err_res.content[0].text.lower()


@pytest.mark.anyio
async def test_link_projects(mcp_setup):
    server, db, settings, tmp_path = mcp_setup
    registry = Registry(db)
    registry.create_project("Lab")
    registry.create_project("Lab Two")

    res = await server.call_tool("link_projects", {"project": "lab-two", "target": "lab", "kind": "evolves_from"})
    assert not res.is_error
    assert json.loads(res.content[0].text)["kind"] == "evolves_from"
    assert registry.list_events(limit=1)[0]["source"] == "agent"

    with pytest.raises(ToolError, match="already linked"):
        await server.call_tool("link_projects", {"project": "lab-two", "target": "lab", "kind": "evolves_from"})
    with pytest.raises(ToolError, match="not found"):
        await server.call_tool("link_projects", {"project": "lab", "target": "nope"})
