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

    server = MCPServer(
        name="project-os",
        version="0.3.0",
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
            "linked repositories, observed git status, stack, latest evidence, and recent events."
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
