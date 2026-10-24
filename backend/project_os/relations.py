"""Relations between projects, for the radial map.

Two kinds of edges, kept apart like declared and observed state elsewhere:
- declared links, written by the user or an agent and stored in
  `project_links` (every change goes to the event log);
- observed relations, derived on the fly from the repositories and never
  stored: two projects sharing a Git remote, a project folder inside another.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import PurePosixPath

from .db import Database, utcnow
from .errors import Conflict, NotFound, record_event

LINK_KINDS = ("depends_on", "part_of", "evolves_from", "related")
# Only 'related' has no direction: A related B is the same link as B related A.
SYMMETRIC_KINDS = ("related",)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def normalize_remote(url: str | None) -> str | None:
    """Reduce a remote URL to host/owner/name so the same repository matches
    across https/ssh spellings, letter case and a trailing `.git`."""
    if not url:
        return None
    url = url.strip().lower()
    url = re.sub(r"^[a-z+]+://", "", url)  # https://, ssh://, git+ssh://
    url = re.sub(r"^[^@/]+@", "", url)  # user@ (credentials are already stripped)
    url = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", url)  # scp-like host:owner/name
    url = url.rstrip("/").removesuffix(".git")
    return url or None


class Relations:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- links

    def add_link(self, source_id: int, target_id: int, kind: str, note: str | None = None,
                 source: str = "user") -> dict:
        if kind not in LINK_KINDS:
            raise Conflict(f"kind must be one of {LINK_KINDS}")
        if source_id == target_id:
            raise Conflict("A project cannot be linked to itself")
        names = {r["id"]: r["name"] for r in self.db.query(
            "SELECT id, name FROM projects WHERE id IN (?, ?)", (source_id, target_id))}
        for pid in (source_id, target_id):
            if pid not in names:
                raise NotFound(f"project {pid}")
        if kind in SYMMETRIC_KINDS and self.db.query_one(
                "SELECT 1 FROM project_links WHERE source_id = ? AND target_id = ? AND kind = ?",
                (target_id, source_id, kind)):
            raise Conflict("These projects are already linked")
        try:
            with self.db.transaction() as c:
                link_id = c.execute(
                    """INSERT INTO project_links (source_id, target_id, kind, note, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (source_id, target_id, kind, _clean(note), utcnow()),
                ).lastrowid
                payload = {"id": link_id, "kind": kind, "from": names[source_id], "to": names[target_id]}
                record_event(c, source_id, "link_added", source, payload)
                record_event(c, target_id, "link_added", source, payload)
        except sqlite3.IntegrityError as exc:
            raise Conflict("These projects are already linked") from exc
        return self.get_link(link_id)

    def remove_link(self, link_id: int, source: str = "user") -> None:
        link = self.get_link(link_id)
        with self.db.transaction() as c:
            c.execute("DELETE FROM project_links WHERE id = ?", (link_id,))
            payload = {"id": link_id, "kind": link["kind"], "from": link["source_name"], "to": link["target_name"]}
            record_event(c, link["source_id"], "link_removed", source, payload)
            record_event(c, link["target_id"], "link_removed", source, payload)

    def get_link(self, link_id: int) -> dict:
        rows = self._links("WHERE l.id = ?", (link_id,))
        if not rows:
            raise NotFound(f"link {link_id}")
        return rows[0]

    def _links(self, where: str = "", params: tuple = ()) -> list[dict]:
        rows = self.db.query(
            f"""SELECT l.*, s.slug AS source_slug, s.name AS source_name,
                       t.slug AS target_slug, t.name AS target_name
                FROM project_links l
                JOIN projects s ON s.id = l.source_id
                JOIN projects t ON t.id = l.target_id
                {where} ORDER BY l.id""",
            params,
        )
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- graph

    def relations(self, project_id: int | None = None) -> list[dict]:
        """Declared links and observed relations, optionally only those
        touching one project. Endpoints are given as slugs."""
        edges = [
            {"id": l["id"], "origin": "declared", "kind": l["kind"], "note": l["note"],
             "source": l["source_slug"], "target": l["target_slug"],
             "source_name": l["source_name"], "target_name": l["target_name"], "created_at": l["created_at"]}
            for l in self._links()
        ]
        edges += self._observed()
        if project_id is not None:
            slug = self.db.query_one("SELECT slug FROM projects WHERE id = ?", (project_id,))
            if not slug:
                raise NotFound(f"project {project_id}")
            edges = [e for e in edges if slug["slug"] in (e["source"], e["target"])]
        return edges

    def _observed(self) -> list[dict]:
        repos = self.db.query(
            """SELECT r.path, r.remote_url, p.slug, p.name FROM repositories r
               JOIN projects p ON p.id = r.project_id
               WHERE r.missing_since IS NULL ORDER BY p.slug, r.path"""
        )
        names = {r["slug"]: r["name"] for r in repos}
        edges: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        def add(kind: str, a: str, b: str, note: str) -> None:
            key = (kind, a, b)
            if a != b and key not in seen:
                seen.add(key)
                edges.append({"id": None, "origin": "observed", "kind": kind, "note": note,
                              "source": a, "target": b, "source_name": names[a], "target_name": names[b],
                              "created_at": None})

        by_remote: dict[str, list[str]] = {}
        for r in repos:
            if remote := normalize_remote(r["remote_url"]):
                by_remote.setdefault(remote, [])
                if r["slug"] not in by_remote[remote]:
                    by_remote[remote].append(r["slug"])
        for remote, slugs in by_remote.items():
            for i, a in enumerate(slugs):
                for b in slugs[i + 1:]:
                    add("same_remote", a, b, remote)

        paths = [(PurePosixPath(r["path"]), r["slug"]) for r in repos]
        for outer, a in paths:
            for inner, b in paths:
                if inner != outer and inner.is_relative_to(outer):
                    add("contains", a, b, str(inner.relative_to(outer)))
        return edges
