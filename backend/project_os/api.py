"""Local HTTP API and static frontend."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import resume
from .config import Settings
from .db import Database
from .registry import Conflict, NotFound, Registry

log = logging.getLogger("project_os")

Lifecycle = Literal["active", "paused", "archived"]
Phase = Literal["idea", "prototype", "development", "stabilization", "release"]


DecisionKind = Literal["decision", "constraint"]


class DecisionIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    rationale: str | None = Field(None, max_length=10000)
    alternatives: str | None = Field(None, max_length=10000)
    kind: DecisionKind = "decision"
    supersedes_id: int | None = None


class DecisionPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    rationale: str | None = Field(None, max_length=10000)
    alternatives: str | None = Field(None, max_length=10000)
    kind: DecisionKind | None = None
    status: Literal["active", "review"] | None = None


class SessionIn(BaseModel):
    goal: str = Field(min_length=1, max_length=1000)
    # When `ended` is true the session is logged as already finished.
    ended: bool = False
    outcome: str | None = Field(None, max_length=10000)
    open_questions: list[str] = Field(default_factory=list, max_length=50)
    next_step: str | None = Field(None, max_length=2000)


class SessionEnd(BaseModel):
    outcome: str | None = Field(None, max_length=10000)
    open_questions: list[str] = Field(default_factory=list, max_length=50)
    next_step: str | None = Field(None, max_length=2000)


class SessionPatch(BaseModel):
    goal: str | None = Field(None, min_length=1, max_length=1000)
    outcome: str | None = Field(None, max_length=10000)
    open_questions: list[str] | None = Field(None, max_length=50)
    next_step: str | None = Field(None, max_length=2000)


ResumeMode = Literal["implementation_within_scope", "analysis_only"]


class ResumeIn(BaseModel):
    mode: ResumeMode = "implementation_within_scope"


class RootIn(BaseModel):
    path: str
    max_depth: int = Field(3, ge=1, le=6)


class RootPatch(BaseModel):
    enabled: bool | None = None
    max_depth: int | None = Field(None, ge=1, le=6)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    path: str | None = None
    lifecycle: Lifecycle = "active"
    phase: Phase | None = None
    priority: int = Field(0, ge=0, le=3)


class ProjectPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    lifecycle: Lifecycle | None = None
    phase: Phase | None = None
    priority: int | None = Field(None, ge=0, le=3)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    registry = Registry(db)
    memory = registry.memory

    async def scheduler() -> None:
        while True:
            await asyncio.sleep(settings.scan_interval_s)
            try:
                await asyncio.to_thread(registry.scan)
            except Conflict:
                pass  # a manual scan is already running
            except Exception:
                log.exception("scheduled scan failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.scan_interval_s > 0:
            task = asyncio.create_task(scheduler())
        yield
        if task:
            task.cancel()
        db.close()

    app = FastAPI(title="Project OS", version="0.1.0", lifespan=lifespan)
    app.state.registry = registry

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound):
        return JSONResponse({"detail": f"Not found: {exc}"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_: Request, exc: Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # ---- overview / projects

    @app.get("/api/overview")
    def overview():
        return registry.overview()

    @app.get("/api/projects")
    def list_projects(q: str | None = None, lifecycle: Lifecycle | None = None,
                      health: Literal["healthy", "stale", "missing", "unknown"] | None = None):
        return registry.list_projects(q=q, lifecycle=lifecycle, health=health)

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn):
        return registry.create_project(**body.model_dump())

    @app.get("/api/projects/{slug}")
    def get_project(slug: str):
        return registry.get_project_by_slug(slug)

    @app.patch("/api/projects/{slug}")
    def update_project(slug: str, body: ProjectPatch):
        project = registry.get_project_by_slug(slug)
        return registry.update_project(project["id"], **body.model_dump(exclude_unset=True))

    @app.delete("/api/projects/{slug}", status_code=204)
    def delete_project(slug: str):
        registry.delete_project(registry.get_project_by_slug(slug)["id"])

    # ---- decision log / work sessions

    @app.get("/api/projects/{slug}/decisions")
    def list_decisions(slug: str, include_superseded: bool = True):
        return memory.list_decisions(registry.resolve(slug), include_superseded)

    @app.post("/api/projects/{slug}/decisions", status_code=201)
    def record_decision(slug: str, body: DecisionIn):
        return memory.record_decision(registry.resolve(slug), **body.model_dump())

    @app.patch("/api/decisions/{decision_id}")
    def update_decision(decision_id: int, body: DecisionPatch):
        return memory.update_decision(decision_id, **body.model_dump(exclude_unset=True))

    @app.get("/api/projects/{slug}/sessions")
    def list_sessions(slug: str):
        return memory.list_sessions(registry.resolve(slug))

    @app.post("/api/projects/{slug}/sessions", status_code=201)
    def create_session(slug: str, body: SessionIn):
        project_id = registry.resolve(slug)
        if body.ended:
            return memory.log_session(project_id, body.goal, body.outcome, body.open_questions, body.next_step)
        return memory.start_session(project_id, body.goal)

    @app.post("/api/sessions/{session_id}/end")
    def end_session(session_id: int, body: SessionEnd):
        return memory.end_session(session_id, **body.model_dump())

    @app.patch("/api/sessions/{session_id}")
    def update_session(session_id: int, body: SessionPatch):
        return memory.update_session(session_id, **body.model_dump(exclude_unset=True))

    # ---- Riprendi progetto (PRP)

    @app.get("/api/projects/{slug}/resume")
    def preview_resume(slug: str, mode: ResumeMode = "implementation_within_scope"):
        """Build a brief without writing it anywhere."""
        brief = resume.build_brief(registry, registry.resolve(slug), mode)
        return {"markdown": brief.markdown, "manifest": brief.manifest, "evidence": brief.evidence,
                "errors": brief.errors}

    @app.post("/api/projects/{slug}/resume", status_code=201)
    def export_resume(slug: str, body: ResumeIn):
        brief, path = resume.export(registry, registry.resolve(slug), settings.exports_dir, body.mode)
        return {"brief_id": brief.brief_id, "path": str(path), "latest": str(path.parent / "latest"),
                "markdown": brief.markdown, "manifest": brief.manifest}

    @app.get("/api/projects/{slug}/resume/{brief_id}.zip")
    def download_resume(slug: str, brief_id: str):
        registry.resolve(slug)
        if not re.fullmatch(r"resume_[0-9A-Za-z_]+", brief_id):
            raise HTTPException(404)
        bundle = settings.exports_dir / slug / brief_id
        if not bundle.is_dir():
            raise HTTPException(404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(bundle.iterdir()):
                zf.write(f, f"{slug}-{brief_id}/{f.name}")
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{slug}-{brief_id}.zip"'})

    @app.get("/api/events")
    def list_events(limit: int = Query(100, ge=1, le=500)):
        return registry.list_events(limit=limit)

    # ---- roots / scans

    @app.get("/api/roots")
    def list_roots():
        return registry.list_roots()

    @app.post("/api/roots", status_code=201)
    def add_root(body: RootIn):
        return registry.add_root(body.path, body.max_depth)

    @app.patch("/api/roots/{root_id}")
    def update_root(root_id: int, body: RootPatch):
        return registry.update_root(root_id, **body.model_dump(exclude_unset=True))

    @app.delete("/api/roots/{root_id}", status_code=204)
    def remove_root(root_id: int):
        registry.remove_root(root_id)

    @app.post("/api/scans")
    def run_scan():
        # Runs in FastAPI's threadpool; the registry serialises concurrent scans.
        return registry.scan()

    @app.get("/api/scans")
    def list_scans(limit: int = Query(20, ge=1, le=200)):
        return registry.list_scans(limit)

    @app.get("/api/health")
    def health():
        return {"ok": True, "schema_version": db.schema_version()}

    _mount_frontend(app, settings.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA; unknown non-API paths fall back to index.html."""
    index = dist / "index.html"
    if not index.is_file():
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)
