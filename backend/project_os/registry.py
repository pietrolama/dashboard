"""Project Registry: reconciles scanner observations with the database.

Rules that keep the registry trustworthy:
- A rescan with no changes writes no evidence and no events.
- A failed observation keeps the previous state; the failure is recorded on
  the scan run, never written over the project.
- Repositories that disappear are flagged `missing_since`, never deleted, and
  only when their root was scanned successfully.
- User-declared fields (name, description, lifecycle, phase, priority) are
  never overwritten by the scanner.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import discovery
from .db import Database, utcnow
from .errors import Conflict, NotFound, record_event
from .memory import Memory
from .relations import Relations

STALE_AFTER = timedelta(days=30)
LIFECYCLES = ("active", "paused", "archived")
PHASES = ("idea", "prototype", "development", "stabilization", "release")


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class Registry:
    def __init__(self, db: Database):
        self.db = db
        self.memory = Memory(db)
        self.relations = Relations(db)
        self._scan_lock = threading.Lock()

    # ------------------------------------------------------------------ roots

    def list_roots(self) -> list[dict]:
        rows = self.db.query(
            """SELECT r.*, (SELECT COUNT(*) FROM repositories WHERE root_id = r.id
                            AND missing_since IS NULL) AS project_count
               FROM roots r ORDER BY r.path"""
        )
        return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]

    def add_root(self, path: str, max_depth: int = 3) -> dict:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise Conflict(f"Not a directory: {resolved}")
        if not 1 <= max_depth <= 6:
            raise Conflict("max_depth must be between 1 and 6")
        try:
            with self.db.transaction() as c:
                cur = c.execute(
                    "INSERT INTO roots (path, max_depth, created_at) VALUES (?, ?, ?)",
                    (str(resolved), max_depth, utcnow()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"Root already registered: {resolved}") from exc
        return self._root(cur.lastrowid)

    def update_root(self, root_id: int, *, enabled: bool | None = None, max_depth: int | None = None) -> dict:
        self._root(root_id)
        if max_depth is not None and not 1 <= max_depth <= 6:
            raise Conflict("max_depth must be between 1 and 6")
        with self.db.transaction() as c:
            if enabled is not None:
                c.execute("UPDATE roots SET enabled = ? WHERE id = ?", (int(enabled), root_id))
            if max_depth is not None:
                c.execute("UPDATE roots SET max_depth = ? WHERE id = ?", (max_depth, root_id))
        return self._root(root_id)

    def remove_root(self, root_id: int) -> None:
        """Stop scanning a root. Projects already registered are kept."""
        self._root(root_id)
        with self.db.transaction() as c:
            c.execute("DELETE FROM roots WHERE id = ?", (root_id,))

    def _root(self, root_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM roots WHERE id = ?", (root_id,))
        if not row:
            raise NotFound(f"root {root_id}")
        return dict(row) | {"enabled": bool(row["enabled"])}

    # --------------------------------------------------------------- ignores

    def list_ignores(self) -> list[str]:
        return [r["pattern"] for r in
                self.db.query("SELECT pattern FROM scan_ignores ORDER BY pattern")]

    def add_ignore(self, pattern: str) -> dict:
        """Add a glob to the scan ignore list and forget projects it now covers.

        Returns the pattern and the slugs of projects dropped because of it.
        """
        pattern = pattern.strip().rstrip("/")
        if not pattern:
            raise Conflict("ignore pattern is required")
        try:
            with self.db.transaction() as c:
                c.execute("INSERT INTO scan_ignores (pattern, created_at) VALUES (?, ?)",
                          (pattern, utcnow()))
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"Pattern already ignored: {pattern}") from exc
        dropped = self._forget_ignored([pattern])
        return {"pattern": pattern, "dropped": dropped}

    def remove_ignore(self, pattern: str) -> None:
        with self.db.transaction() as c:
            cur = c.execute("DELETE FROM scan_ignores WHERE pattern = ?", (pattern.strip(),))
        if not cur.rowcount:
            raise NotFound(f"ignore pattern {pattern!r}")

    def _forget_ignored(self, patterns: list[str]) -> list[str]:
        """Delete discovered projects whose repositories all match an ignore glob.

        A project is dropped only when every one of its live repositories is
        ignored, so a folder that also holds real projects is never removed.
        """
        dropped: list[str] = []
        roots = {r["id"]: Path(r["path"]) for r in self.db.query("SELECT id, path FROM roots")}
        projects = self.db.query("SELECT id, slug, origin FROM projects")
        for p in projects:
            if p["origin"] != "discovered":
                continue
            repos = self.db.query(
                "SELECT path, root_id FROM repositories WHERE project_id = ? AND missing_since IS NULL",
                (p["id"],))
            if not repos:
                continue
            if all(r["root_id"] in roots
                   and discovery._ignored(Path(r["path"]), roots[r["root_id"]], patterns)
                   for r in repos):
                with self.db.transaction() as c:
                    c.execute("DELETE FROM projects_fts WHERE rowid = ?", (p["id"],))
                    c.execute("DELETE FROM projects WHERE id = ?", (p["id"],))
                dropped.append(p["slug"])
        return dropped

    # ------------------------------------------------------------------- scan

    def scan(self) -> dict:
        """Scan every enabled root. Only one scan runs at a time."""
        if not self._scan_lock.acquire(blocking=False):
            raise Conflict("A scan is already running")
        try:
            return self._scan()
        finally:
            self._scan_lock.release()

    @property
    def scanning(self) -> bool:
        return self._scan_lock.locked()

    def _scan(self) -> dict:
        with self.db.transaction() as c:
            run_id = c.execute(
                "INSERT INTO scan_runs (started_at, status) VALUES (?, 'running')", (utcnow(),)
            ).lastrowid

        stats = {"roots": 0, "candidates": 0, "new_projects": 0, "changed": 0,
                 "unchanged": 0, "missing": 0, "restored": 0}
        errors: list[dict] = []
        roots = self.db.query("SELECT * FROM roots WHERE enabled = 1 ORDER BY path")
        ignore = self.list_ignores()

        for root in roots:
            try:
                candidates = discovery.find_candidates(Path(root["path"]), root["max_depth"], ignore)
            except OSError as exc:
                errors.append({"root": root["path"], "error": str(exc)})
                continue
            stats["roots"] += 1
            stats["candidates"] += len(candidates)
            seen: set[str] = set()

            for cand in candidates:
                seen.add(str(cand.path))
                try:
                    obs = discovery.observe(cand)
                except Exception as exc:  # one broken repo must not abort the scan
                    errors.append({"path": str(cand.path), "error": f"{type(exc).__name__}: {exc}"})
                    continue
                outcome, restored = self._reconcile(root["id"], obs)
                stats[outcome] += 1
                stats["restored"] += restored

            stats["missing"] += self._mark_missing(root["id"], seen)

        status = "ok" if not errors else ("partial" if stats["roots"] else "failed")
        if not roots:
            status = "ok"
        with self.db.transaction() as c:
            c.execute(
                "UPDATE scan_runs SET finished_at = ?, status = ?, stats_json = ?, errors_json = ? WHERE id = ?",
                (utcnow(), status, json.dumps(stats), json.dumps(errors), run_id),
            )
        return self.get_scan(run_id)

    def _reconcile(self, root_id: int | None, obs: discovery.Observation) -> tuple[str, bool]:
        """Apply one observation. Returns (outcome, restored) where outcome is
        'new_projects', 'changed' or 'unchanged'."""
        now = utcnow()
        path = str(obs.path)
        restored = False
        with self.db.transaction() as c:
            repo = c.execute("SELECT * FROM repositories WHERE path = ?", (path,)).fetchone()
            created = repo is None

            if created:
                name = obs.path.name
                project_id = c.execute(
                    """INSERT INTO projects (name, slug, origin, created_at, updated_at)
                       VALUES (?, ?, 'discovered', ?, ?)""",
                    (name, self._unique_slug(c, name), now, now),
                ).lastrowid
                repo_id = c.execute(
                    "INSERT INTO repositories (project_id, root_id, path, vcs) VALUES (?, ?, ?, ?)",
                    (project_id, root_id, path, obs.vcs),
                ).lastrowid
                self._event(c, project_id, "project_discovered", "scanner",
                            {"path": path, "vcs": obs.vcs})
            else:
                project_id, repo_id = repo["project_id"], repo["id"]
                if repo["missing_since"]:
                    self._event(c, project_id, "repository_restored", "scanner", {"path": path})
                    restored = True
                if repo["vcs"] != obs.vcs:
                    self._event(c, project_id, "vcs_changed", "scanner",
                                {"path": path, "from": repo["vcs"], "to": obs.vcs})

            git = obs.git or {}
            c.execute(
                """UPDATE repositories SET root_id = ?, vcs = ?, remote_url = ?, default_branch = ?,
                   last_scanned_at = ?, missing_since = NULL WHERE id = ?""",
                (root_id, obs.vcs, git.get("remote_url"), git.get("branch"), now, repo_id),
            )

            changed = False
            filesystem = obs.filesystem | {"last_activity_at": obs.last_activity_at}
            for source_type, payload in (("git", obs.git), ("readme", obs.readme), ("filesystem", filesystem)):
                if payload is None:
                    continue
                previous = self._record_evidence(c, project_id, source_type, path, payload, now)
                if previous is False:
                    continue
                changed = True
                if not created:
                    self._change_events(c, project_id, source_type, previous, payload)

            c.execute(
                """UPDATE projects SET last_observed_at = ?,
                   last_activity_at = CASE
                       WHEN last_activity_at IS NULL OR ? > last_activity_at THEN ?
                       ELSE last_activity_at END
                   WHERE id = ?""",
                (now, obs.last_activity_at, obs.last_activity_at, project_id),
            )
            if changed:
                self._index(c, project_id)

        if created:
            return "new_projects", False
        return ("changed" if changed else "unchanged"), restored

    def _record_evidence(self, c, project_id, source_type, source_ref, payload, now):
        """Store `payload` if it differs from the latest observation.

        Returns False when unchanged, otherwise the previous payload (None
        if this is the first observation of this source).
        """
        digest = _hash(payload)
        last = c.execute(
            """SELECT content_hash, payload_json FROM evidence
               WHERE project_id = ? AND source_type = ? AND source_ref = ?
               ORDER BY id DESC LIMIT 1""",
            (project_id, source_type, source_ref),
        ).fetchone()
        if last and last["content_hash"] == digest:
            return False
        c.execute(
            """INSERT INTO evidence (project_id, source_type, source_ref, content_hash, observed_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project_id, source_type, source_ref, digest, now, json.dumps(payload)),
        )
        return json.loads(last["payload_json"]) if last else None

    def _change_events(self, c, project_id, source_type, before: dict | None, after: dict) -> None:
        before = before or {}
        if source_type == "git":
            if before.get("head") != after.get("head") and after.get("head"):
                self._event(c, project_id, "git_head_changed", "git", {
                    "from": before.get("head"), "to": after["head"],
                    "subject": after.get("head_subject"), "date": after.get("head_date"),
                })
            if before.get("branch") != after.get("branch"):
                self._event(c, project_id, "git_branch_changed", "git",
                            {"from": before.get("branch"), "to": after.get("branch")})
            dirty_before = (before.get("dirty_files", 0), before.get("untracked_files", 0))
            dirty_after = (after.get("dirty_files", 0), after.get("untracked_files", 0))
            if dirty_before != dirty_after:
                self._event(c, project_id, "worktree_changed", "git", {
                    "dirty_files": dirty_after[0], "untracked_files": dirty_after[1],
                })
        elif source_type == "readme":
            self._event(c, project_id, "readme_changed", "filesystem", {"file": after.get("file")})
        elif source_type == "filesystem" and before.get("stack") != after.get("stack"):
            self._event(c, project_id, "stack_changed", "filesystem",
                        {"from": before.get("stack"), "to": after.get("stack")})

    def _mark_missing(self, root_id: int, seen: set[str]) -> int:
        now = utcnow()
        count = 0
        with self.db.transaction() as c:
            rows = c.execute(
                "SELECT id, project_id, path FROM repositories WHERE root_id = ? AND missing_since IS NULL",
                (root_id,),
            ).fetchall()
            for r in rows:
                if r["path"] in seen:
                    continue
                c.execute("UPDATE repositories SET missing_since = ? WHERE id = ?", (now, r["id"]))
                self._event(c, r["project_id"], "repository_missing", "scanner", {"path": r["path"]})
                count += 1
        return count

    _event = staticmethod(record_event)

    @staticmethod
    def _unique_slug(c, name: str, exclude_id: int | None = None) -> str:
        base = _slugify(name)
        slug, n = base, 2
        while (row := c.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()) and row["id"] != exclude_id:
            slug, n = f"{base}-{n}", n + 1
        return slug

    def _index(self, c, project_id: int) -> None:
        p = c.execute("SELECT name, description FROM projects WHERE id = ?", (project_id,)).fetchone()
        readme = c.execute(
            """SELECT payload_json FROM evidence WHERE project_id = ? AND source_type = 'readme'
               ORDER BY id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        readme_text = json.loads(readme["payload_json"]).get("excerpt", "") if readme else ""
        c.execute("DELETE FROM projects_fts WHERE rowid = ?", (project_id,))
        c.execute(
            "INSERT INTO projects_fts (rowid, name, description, readme) VALUES (?, ?, ?, ?)",
            (project_id, p["name"], p["description"] or "", readme_text),
        )

    # --------------------------------------------------------------- projects

    def create_project(self, name: str, description: str | None = None, path: str | None = None,
                       **fields: Any) -> dict:
        """Register a project by hand, e.g. a creative work with no repository."""
        name = name.strip()
        if not name:
            raise Conflict("name is required")
        self._validate_fields(fields)
        now = utcnow()
        resolved = str(Path(path).expanduser().resolve()) if path else None
        if resolved and not Path(resolved).is_dir():
            raise Conflict(f"Not a directory: {resolved}")
        try:
            with self.db.transaction() as c:
                project_id = c.execute(
                    """INSERT INTO projects (name, slug, description, lifecycle, phase, priority,
                                             origin, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?)""",
                    (name, self._unique_slug(c, name), description,
                     fields.get("lifecycle", "active"), fields.get("phase"),
                     fields.get("priority", 0), now, now),
                ).lastrowid
                if resolved:
                    vcs = "git" if discovery.gitinfo.is_git_repo(Path(resolved)) else "none"
                    c.execute(
                        "INSERT INTO repositories (project_id, path, vcs) VALUES (?, ?, ?)",
                        (project_id, resolved, vcs),
                    )
                self._event(c, project_id, "project_created", "user", {"name": name})
                self._index(c, project_id)
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"Path already belongs to a project: {resolved}") from exc
        if resolved:
            self._observe_now(project_id, resolved)
        return self.get_project(project_id)

    def refresh_project(self, project_id: int) -> list[dict]:
        """Re-observe one project's repositories now. Returns read errors.

        Repositories already flagged missing are skipped; one that has vanished
        since the last scan is reported as an error, not marked missing — that
        is the scheduled scan's job.
        """
        errors = []
        repos = self.db.query(
            "SELECT * FROM repositories WHERE project_id = ? AND missing_since IS NULL", (project_id,)
        )
        for repo in repos:
            path = Path(repo["path"])
            if not path.is_dir():
                errors.append({"path": repo["path"], "error": "directory not found"})
                continue
            try:
                obs = discovery.observe(discovery.Candidate(path, repo["vcs"]))
            except Exception as exc:
                errors.append({"path": repo["path"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            self._reconcile(repo["root_id"], obs)
        return errors

    def _observe_now(self, project_id: int, path: str) -> None:
        repo = self.db.query_one("SELECT * FROM repositories WHERE path = ?", (path,))
        try:
            obs = discovery.observe(discovery.Candidate(Path(path), repo["vcs"]))
        except Exception:
            return  # the next scheduled scan will report it
        self._reconcile(repo["root_id"], obs)

    def update_project(self, project_id: int, **fields: Any) -> dict:
        current = self._project_row(project_id)
        self._validate_fields(fields)
        changes = {k: v for k, v in fields.items() if current[k] != v}
        if not changes:
            return self.get_project(project_id)
        with self.db.transaction() as c:
            if "name" in changes:
                if not changes["name"].strip():
                    raise Conflict("name is required")
                changes["name"] = changes["name"].strip()
            sets = ", ".join(f"{k} = ?" for k in changes)
            c.execute(f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?",
                      (*changes.values(), utcnow(), project_id))
            self._event(c, project_id, "project_updated", "user",
                        {k: {"from": current[k], "to": v} for k, v in changes.items()})
            if {"name", "description"} & changes.keys():
                self._index(c, project_id)
        return self.get_project(project_id)

    def delete_project(self, project_id: int) -> None:
        """Forget a project. Only allowed for manual projects or missing repos,
        otherwise the next scan would just rediscover it."""
        p = self._project_row(project_id)
        live = self.db.query_one(
            "SELECT 1 FROM repositories WHERE project_id = ? AND missing_since IS NULL AND root_id IS NOT NULL",
            (project_id,),
        )
        if p["origin"] == "discovered" and live:
            raise Conflict("Project is still present on disk; archive it instead or remove its root")
        with self.db.transaction() as c:
            c.execute("DELETE FROM projects_fts WHERE rowid = ?", (project_id,))
            c.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    @staticmethod
    def _validate_fields(fields: dict) -> None:
        allowed = {"name", "description", "lifecycle", "phase", "priority"}
        if unknown := set(fields) - allowed:
            raise Conflict(f"Unknown fields: {', '.join(sorted(unknown))}")
        if "lifecycle" in fields and fields["lifecycle"] not in LIFECYCLES:
            raise Conflict(f"lifecycle must be one of {LIFECYCLES}")
        if fields.get("phase") is not None and fields["phase"] not in PHASES:
            raise Conflict(f"phase must be one of {PHASES}")
        if "priority" in fields and not (isinstance(fields["priority"], int) and 0 <= fields["priority"] <= 3):
            raise Conflict("priority must be an integer between 0 and 3")

    def _project_row(self, project_id: int) -> sqlite3.Row:
        row = self.db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not row:
            raise NotFound(f"project {project_id}")
        return row

    def list_projects(self, q: str | None = None, lifecycle: str | None = None,
                      health: str | None = None) -> list[dict]:
        sql = "SELECT p.* FROM projects p"
        params: list[Any] = []
        where = []
        if q and q.strip():
            sql += " JOIN projects_fts f ON f.rowid = p.id"
            where.append("projects_fts MATCH ?")
            params.append(_fts_query(q))
        if lifecycle:
            where.append("p.lifecycle = ?")
            params.append(lifecycle)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.priority DESC, p.last_activity_at DESC NULLS LAST, p.name"

        rows = self.db.query(sql, params)
        projects = [self._summary(r) for r in rows]
        if health:
            projects = [p for p in projects if p["health"] == health]
        return projects

    def _summary(self, p: sqlite3.Row) -> dict:
        repos = self.db.query("SELECT * FROM repositories WHERE project_id = ? ORDER BY path", (p["id"],))
        latest = self._latest_evidence(p["id"])
        git = latest.get("git", {}).get("payload")
        readme = latest.get("readme", {}).get("payload")
        fs = latest.get("filesystem", {}).get("payload") or {}
        return {
            **dict(p),
            "health": _health(p, repos),
            "readme_title": readme.get("title") if readme else None,
            "readme_summary": readme.get("summary") if readme else None,
            "stack": fs.get("stack", []),
            "git": git,
            "repositories": [dict(r) for r in repos],
            "memory": self.memory.snapshot(p["id"]),
        }

    def _latest_evidence(self, project_id: int) -> dict[str, dict]:
        rows = self.db.query(
            """SELECT e.* FROM evidence e
               JOIN (SELECT source_type, MAX(id) AS id FROM evidence
                     WHERE project_id = ? GROUP BY source_type) last ON last.id = e.id""",
            (project_id,),
        )
        return {r["source_type"]: {**dict(r), "payload": json.loads(r["payload_json"])} for r in rows}

    def get_project(self, project_id: int) -> dict:
        p = self._project_row(project_id)
        summary = self._summary(p)
        evidence = self._latest_evidence(project_id)
        for e in evidence.values():
            e.pop("payload_json", None)
        return {
            **summary,
            "evidence": evidence,
            "events": self.list_events(project_id=project_id, limit=50),
            "decisions": self.memory.list_decisions(project_id),
            "sessions": self.memory.list_sessions(project_id),
            "relations": self.relations.relations(project_id),
        }

    def resolve(self, slug: str) -> int:
        row = self.db.query_one("SELECT id FROM projects WHERE slug = ?", (slug,))
        if not row:
            raise NotFound(f"project {slug}")
        return row["id"]

    def get_project_by_slug(self, slug: str) -> dict:
        return self.get_project(self.resolve(slug))

    def relation_graph(self) -> dict:
        """Every project as a node, with declared and observed relations."""
        nodes = [
            {k: p[k] for k in ("slug", "name", "lifecycle", "phase", "priority", "health",
                               "stack", "last_activity_at")}
            for p in self.list_projects()
        ]
        return {"nodes": nodes, "relations": self.relations.relations()}

    # ----------------------------------------------------------------- events

    def list_events(self, project_id: int | None = None, limit: int = 100) -> list[dict]:
        sql = """SELECT e.*, p.name AS project_name, p.slug AS project_slug
                 FROM events e LEFT JOIN projects p ON p.id = e.project_id"""
        params: list[Any] = []
        if project_id is not None:
            sql += " WHERE e.project_id = ?"
            params.append(project_id)
        sql += " ORDER BY e.id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in self.db.query(sql, params)]

    def list_scans(self, limit: int = 20) -> list[dict]:
        rows = self.db.query("SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [_scan_dict(r) for r in rows]

    def get_scan(self, scan_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM scan_runs WHERE id = ?", (scan_id,))
        if not row:
            raise NotFound(f"scan {scan_id}")
        return _scan_dict(row)

    def overview(self) -> dict:
        projects = self.list_projects()
        last_scan = self.list_scans(limit=1)
        counts: dict[str, dict[str, int]] = {"lifecycle": {}, "health": {}}
        for p in projects:
            counts["lifecycle"][p["lifecycle"]] = counts["lifecycle"].get(p["lifecycle"], 0) + 1
            counts["health"][p["health"]] = counts["health"].get(p["health"], 0) + 1
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        return {
            "total": len(projects),
            "counts": counts,
            "dirty": sum(1 for p in projects if p["git"] and (p["git"]["dirty_files"] or p["git"]["untracked_files"])),
            "active_this_week": sum(1 for p in projects if (_parse(p["last_activity_at"]) or week_ago) > week_ago),
            "last_scan": last_scan[0] if last_scan else None,
            "scanning": self.scanning,
        }


def _scan_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["stats"] = json.loads(d.pop("stats_json"))
    d["errors"] = json.loads(d.pop("errors_json"))
    return d


def _health(p: sqlite3.Row, repos: list[sqlite3.Row]) -> str:
    """Derived, never stored: 'missing' | 'stale' | 'healthy' | 'unknown'."""
    if repos and all(r["missing_since"] for r in repos):
        return "missing"
    activity = _parse(p["last_activity_at"])
    if activity is None:
        return "unknown"
    if p["lifecycle"] == "active" and datetime.now(timezone.utc) - activity > STALE_AFTER:
        return "stale"
    return "healthy"


def _fts_query(q: str) -> str:
    # Quote every term so user input can't inject FTS syntax; prefix-match each.
    terms = [t.replace('"', "") for t in q.split()]
    return " ".join(f'"{t}"*' for t in terms if t)
