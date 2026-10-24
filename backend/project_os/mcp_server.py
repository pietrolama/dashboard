"""MCP server for Project OS.

Exposes Project OS capabilities to AI agents using the official MCP Python SDK
over stdio transport, reusing Settings, Database, Registry, Memory, and Resume.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import resume
from .team import Team
from .config import Settings
from .db import Database
from .errors import Conflict, NotFound
from .registry import Registry

AGENT_SOURCE = "agent"


def _resolve_project_id(registry: Registry, project: str) -> int:
    """Resolve project slug or numeric ID string to project integer ID."""
    project_str = str(project).strip()
    if not project_str:
        raise ToolError("project identifier is required")
    if project_str.isdigit():
        row = registry.db.query_one("SELECT id FROM projects WHERE id = ?", (int(project_str),))
        if row:
            return int(row["id"])
    try:
        return registry.resolve(project_str)
    except NotFound:
        raise ToolError(f"Project not found: {project_str}")
    except Conflict as exc:
        raise ToolError(str(exc)) from exc


def create_mcp_server(settings: Settings | None = None, db: Database | None = None) -> tuple[MCPServer, Database]:
    """Create and configure the Project OS MCP Server.

    Returns the server instance and the Database instance so callers (or test suites)
    can manage lifecycle and close connections cleanly.
    """
    settings = settings or Settings.from_env()
    db = db or Database(settings.db_path)
    registry = Registry(db)
    team = Team(db, settings)

    server = MCPServer(
        name="project-os",
        version="0.4.0",
        instructions="MCP server for Project OS - local multi-project dashboard and memory.",
    )

    @server.tool(
        name="list_projects",
        description=(
            "List projects managed by Project OS with optional filtering by search query, "
            "lifecycle ('active', 'paused', 'archived'), or health ('healthy', 'stale', 'missing', 'unknown')."
        ),
    )
    def list_projects(
        q: str | None = None,
        lifecycle: Literal["active", "paused", "archived"] | None = None,
        health: Literal["healthy", "stale", "missing", "unknown"] | None = None,
    ) -> dict[str, Any]:
        projects = registry.list_projects(q=q, lifecycle=lifecycle, health=health)
        return {
            "total": len(projects),
            "projects": projects,
        }

    @server.tool(
        name="get_project_context",
        description=(
            "Get detailed project context including metadata, declared lifecycle/phase, "
            "linked repositories, observed git status, stack, latest evidence, recent events, and relations to other projects."
        ),
    )
    def get_project_context(project: str) -> dict[str, Any]:
        project_id = _resolve_project_id(registry, project)
        try:
            return registry.get_project(project_id)
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="get_project_memory",
        description=(
            "Get the operational memory of a project: active and past work sessions, "
            "next steps, open questions, and active/review architectural decisions and constraints."
        ),
    )
    def get_project_memory(
        project: str,
        include_superseded_decisions: bool = False,
    ) -> dict[str, Any]:
        project_id = _resolve_project_id(registry, project)
        try:
            return {
                "snapshot": registry.memory.snapshot(project_id),
                "decisions": registry.memory.list_decisions(
                    project_id, include_superseded=include_superseded_decisions
                ),
                "sessions": registry.memory.list_sessions(project_id),
            }
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="record_decision",
        description=(
            "Record an architectural decision or constraint for a project. "
            "Entries are never deleted; if superseding an older decision, pass its ID in supersedes_id."
        ),
    )
    def record_decision(
        project: str,
        title: str,
        rationale: str | None = None,
        alternatives: str | None = None,
        kind: Literal["decision", "constraint"] = "decision",
        supersedes_id: int | None = None,
    ) -> dict[str, Any]:
        project_id = _resolve_project_id(registry, project)
        try:
            return registry.memory.record_decision(
                project_id=project_id,
                title=title,
                rationale=rationale,
                alternatives=alternatives,
                kind=kind,
                supersedes_id=supersedes_id,
                source=AGENT_SOURCE,
            )
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="link_projects",
        description=(
            "Declare a relation between two projects, shown on the radial map: 'depends_on' "
            "(project uses target), 'part_of' (project is a component of target), 'evolves_from' "
            "(project is the successor of target) or 'related'."
        ),
    )
    def link_projects(
        project: str,
        target: str,
        kind: Literal["depends_on", "part_of", "evolves_from", "related"] = "related",
        note: str | None = None,
    ) -> dict[str, Any]:
        project_id = _resolve_project_id(registry, project)
        target_id = _resolve_project_id(registry, target)
        try:
            return registry.relations.add_link(project_id, target_id, kind, note, source=AGENT_SOURCE)
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="log_work_session",
        description=(
            "Log a completed work session for a project in one step, recording goal, "
            "outcome, open questions, and the next concrete step."
        ),
    )
    def log_work_session(
        project: str,
        goal: str,
        outcome: str | None = None,
        open_questions: list[str] | None = None,
        next_step: str | None = None,
    ) -> dict[str, Any]:
        project_id = _resolve_project_id(registry, project)
        try:
            return registry.memory.log_session(
                project_id=project_id,
                goal=goal,
                outcome=outcome,
                open_questions=open_questions,
                next_step=next_step,
                source=AGENT_SOURCE,
            )
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="preview_resume",
        description=(
            "Generate an in-memory preview of the Project Resume Protocol (PRP 1.0) brief "
            "without writing any files to disk. Returns manifest, markdown, and schema validation errors."
        ),
    )
    def preview_resume(
        project: str,
        mode: Literal["implementation_within_scope", "analysis_only"] = "implementation_within_scope",
        refresh: bool = False,
    ) -> dict[str, Any]:
        project_id = _resolve_project_id(registry, project)
        try:
            brief = resume.build_brief(registry, project_id, mode=mode, refresh=refresh)
            return {
                "brief_id": brief.brief_id,
                "manifest": brief.manifest,
                "evidence": brief.evidence,
                "markdown": brief.markdown,
                "errors": brief.errors,
                "valid": len(brief.errors) == 0,
            }
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="export_resume",
        description=(
            "Export the Project Resume Protocol (PRP 1.0) bundle to the configured exports directory. "
            "Writes manifest.json, evidence.json, RESUME.md, and SHA256SUMS. "
            "Records an export event attributed to the agent."
        ),
    )
    def export_resume(
        project: str,
        mode: Literal["implementation_within_scope", "analysis_only"] = "implementation_within_scope",
    ) -> dict[str, Any]:
        project_id = _resolve_project_id(registry, project)
        try:
            brief, path = resume.export(
                registry=registry,
                project_id=project_id,
                exports_dir=settings.exports_dir,
                mode=mode,
                source=AGENT_SOURCE,
            )
            return {
                "brief_id": brief.brief_id,
                "export_path": str(path),
                "manifest": brief.manifest,
                "errors": brief.errors,
                "valid": len(brief.errors) == 0,
            }
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="scan_projects",
        description=(
            "Scan all currently enabled root directories configured in Project OS. "
            "Discovers new repositories, records evidence changes, and marks missing repositories. "
            "Operates strictly on already registered roots."
        ),
    )
    def scan_projects() -> dict[str, Any]:
        try:
            return registry.scan()
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    # ---- team runs: called by the agents of a run, from their terminals

    def _team_call(fn, *args):
        try:
            return fn(*args)
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="team_board",
        description=(
            "Team run board: the user's request, the approved plan, every task with its status, "
            "the team members with their branches and worktrees, and your recent messages. "
            "Use run_id and agent ('claude' or 'agy') from your team prompt."
        ),
    )
    def team_board(run_id: int, agent: Literal["claude", "agy"]) -> dict[str, Any]:
        return _team_call(team.board, run_id, agent)

    @server.tool(
        name="team_task",
        description=(
            "Update one of your tasks in a team run: status 'doing' when you start it, 'done' when it is "
            "finished and checked, 'blocked' when you cannot continue. The summary says what you did or what "
            "blocks you. Marking a task done tells the other agent if their tasks depend on it."
        ),
    )
    def team_task(run_id: int, agent: Literal["claude", "agy"], task: int,
                  status: Literal["doing", "done", "blocked", "todo"], summary: str | None = None) -> dict[str, Any]:
        return _team_call(team.update_task, run_id, agent, task, status, summary)

    @server.tool(
        name="team_message",
        description=(
            "Send a message to the other agent of your team run (it is typed into their terminal) "
            "or to the user (shown in the dashboard). to: 'claude', 'agy' or 'user'."
        ),
    )
    def team_message(run_id: int, agent: Literal["claude", "agy"], to: Literal["claude", "agy", "user"],
                     text: str) -> dict[str, Any]:
        return _team_call(team.post_message, run_id, agent, to, text)

    @server.tool(
        name="team_done",
        description=(
            "Declare that your part of the team run is finished: all your tasks must be 'done'. "
            "When every agent is done, Project OS merges the branches and reviews the result."
        ),
    )
    def team_done(run_id: int, agent: Literal["claude", "agy"], summary: str) -> dict[str, Any]:
        return _team_call(team.agent_done, run_id, agent, summary)

    return server, db


async def run_server() -> None:
    settings = Settings.from_env()
    server, db = create_mcp_server(settings)
    try:
        await server.run_stdio_async()
    finally:
        db.close()


def main() -> None:
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
