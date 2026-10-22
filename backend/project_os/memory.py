"""Operational memory: the decision log and work sessions of each project.

Everything here is declared by the user (or by an agent acting for them),
never inferred by the scanner. Entries are edited or superseded, not deleted,
and every change is written to the event log.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import Database, utcnow
from .errors import Conflict, NotFound, record_event

DECISION_KINDS = ("decision", "constraint")
# 'superseded' is reachable only through supersede(), never set directly.
EDITABLE_STATUSES = ("active", "review")
DECISION_TEXT_FIELDS = ("title", "rationale", "alternatives")
SESSION_TEXT_FIELDS = ("goal", "outcome", "next_step")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _questions(items: list[str] | None) -> list[str]:
    return [q.strip() for q in items or [] if q and q.strip()]


class Memory:
    def __init__(self, db: Database):
        self.db = db

    # -------------------------------------------------------------- decisions

    def list_decisions(self, project_id: int, include_superseded: bool = True) -> list[dict]:
        sql = """SELECT d.*, s.id AS superseded_by_id FROM decisions d
                 LEFT JOIN decisions s ON s.supersedes_id = d.id
                 WHERE d.project_id = ?"""
        if not include_superseded:
            sql += " AND d.status != 'superseded'"
        # Constraints first, then decisions needing review, then newest.
        sql += """ ORDER BY d.status = 'superseded', d.kind = 'decision',
                   d.status = 'active', d.id DESC"""
        return [dict(r) for r in self.db.query(sql, (project_id,))]

    def get_decision(self, decision_id: int) -> dict:
        row = self.db.query_one(
            """SELECT d.*, s.id AS superseded_by_id FROM decisions d
               LEFT JOIN decisions s ON s.supersedes_id = d.id WHERE d.id = ?""",
            (decision_id,),
        )
        if not row:
            raise NotFound(f"decision {decision_id}")
        return dict(row)

    def record_decision(self, project_id: int, title: str, rationale: str | None = None,
                        alternatives: str | None = None, kind: str = "decision",
                        supersedes_id: int | None = None, source: str = "user") -> dict:
        title = _clean(title)
        if not title:
            raise Conflict("title is required")
        if kind not in DECISION_KINDS:
            raise Conflict(f"kind must be one of {DECISION_KINDS}")
        self._require_project(project_id)
        now = utcnow()
        with self.db.transaction() as c:
            if supersedes_id is not None:
                old = c.execute("SELECT * FROM decisions WHERE id = ?", (supersedes_id,)).fetchone()
                if not old or old["project_id"] != project_id:
                    raise Conflict("The superseded decision must belong to the same project")
                if old["status"] == "superseded":
                    raise Conflict("That decision has already been superseded")
            decision_id = c.execute(
                """INSERT INTO decisions (project_id, kind, title, rationale, alternatives,
                                          supersedes_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, kind, title, _clean(rationale), _clean(alternatives), supersedes_id, now, now),
            ).lastrowid
            if supersedes_id is not None:
                c.execute("UPDATE decisions SET status = 'superseded', updated_at = ? WHERE id = ?",
                          (now, supersedes_id))
                record_event(c, project_id, "decision_superseded", source,
                             {"id": supersedes_id, "title": old["title"], "by": decision_id})
            record_event(c, project_id, "decision_recorded", source,
                         {"id": decision_id, "kind": kind, "title": title})
        return self.get_decision(decision_id)

    def update_decision(self, decision_id: int, source: str = "user", **fields: Any) -> dict:
        current = self.get_decision(decision_id)
        allowed = {*DECISION_TEXT_FIELDS, "status", "kind"}
        if unknown := set(fields) - allowed:
            raise Conflict(f"Unknown fields: {', '.join(sorted(unknown))}")
        if current["status"] == "superseded":
            raise Conflict("A superseded decision is part of the history and cannot be edited")
        if "status" in fields and fields["status"] not in EDITABLE_STATUSES:
            raise Conflict(f"status must be one of {EDITABLE_STATUSES}; use supersede to replace a decision")
        if "kind" in fields and fields["kind"] not in DECISION_KINDS:
            raise Conflict(f"kind must be one of {DECISION_KINDS}")
        for f in DECISION_TEXT_FIELDS:
            if f in fields:
                fields[f] = _clean(fields[f])
        if "title" in fields and not fields["title"]:
            raise Conflict("title is required")

        changes = {k: v for k, v in fields.items() if current[k] != v}
        if not changes:
            return current
        with self.db.transaction() as c:
            sets = ", ".join(f"{k} = ?" for k in changes)
            c.execute(f"UPDATE decisions SET {sets}, updated_at = ? WHERE id = ?",
                      (*changes.values(), utcnow(), decision_id))
            event = "decision_status_changed" if set(changes) == {"status"} else "decision_edited"
            record_event(c, current["project_id"], event, source, {
                "id": decision_id, "title": changes.get("title", current["title"]),
                **({"from": current["status"], "to": changes["status"]} if "status" in changes else {}),
                "fields": sorted(changes),
            })
        return self.get_decision(decision_id)

    # --------------------------------------------------------------- sessions

    def list_sessions(self, project_id: int, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM work_sessions WHERE project_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
            (project_id, limit),
        )
        return [_session(r) for r in rows]

    def get_session(self, session_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM work_sessions WHERE id = ?", (session_id,))
        if not row:
            raise NotFound(f"session {session_id}")
        return _session(row)

    def start_session(self, project_id: int, goal: str, source: str = "user") -> dict:
        goal = _clean(goal)
        if not goal:
            raise Conflict("goal is required")
        self._require_project(project_id)
        try:
            with self.db.transaction() as c:
                session_id = c.execute(
                    "INSERT INTO work_sessions (project_id, started_at, goal) VALUES (?, ?, ?)",
                    (project_id, utcnow(), goal),
                ).lastrowid
                record_event(c, project_id, "session_started", source, {"id": session_id, "goal": goal})
        except sqlite3.IntegrityError as exc:
            raise Conflict("A work session is already in progress for this project") from exc
        return self.get_session(session_id)

    def end_session(self, session_id: int, outcome: str | None = None, open_questions: list[str] | None = None,
                    next_step: str | None = None, source: str = "user") -> dict:
        session = self.get_session(session_id)
        if session["ended_at"]:
            raise Conflict("Session already ended")
        questions = _questions(open_questions)
        with self.db.transaction() as c:
            c.execute(
                """UPDATE work_sessions SET ended_at = ?, outcome = ?, open_questions_json = ?, next_step = ?
                   WHERE id = ?""",
                (utcnow(), _clean(outcome), json.dumps(questions), _clean(next_step), session_id),
            )
            record_event(c, session["project_id"], "session_ended", source, {
                "id": session_id, "goal": session["goal"], "next_step": _clean(next_step),
                "open_questions": len(questions),
            })
        return self.get_session(session_id)

    def log_session(self, project_id: int, goal: str, outcome: str | None = None,
                    open_questions: list[str] | None = None, next_step: str | None = None,
                    source: str = "user") -> dict:
        """Record a session that already happened, in one step."""
        session = self.start_session(project_id, goal, source=source)
        return self.end_session(session["id"], outcome, open_questions, next_step, source=source)

    def update_session(self, session_id: int, source: str = "user", **fields: Any) -> dict:
        """Correct a session's notes. Ending a session goes through end_session()."""
        current = self.get_session(session_id)
        allowed = {*SESSION_TEXT_FIELDS, "open_questions"}
        if unknown := set(fields) - allowed:
            raise Conflict(f"Unknown fields: {', '.join(sorted(unknown))}")
        if "goal" in fields and not _clean(fields["goal"]):
            raise Conflict("goal is required")
        columns: dict[str, Any] = {}
        for f in SESSION_TEXT_FIELDS:
            if f in fields and _clean(fields[f]) != current[f]:
                columns[f] = _clean(fields[f])
        if "open_questions" in fields and _questions(fields["open_questions"]) != current["open_questions"]:
            columns["open_questions_json"] = json.dumps(_questions(fields["open_questions"]))
        if not columns:
            return current
        with self.db.transaction() as c:
            sets = ", ".join(f"{k} = ?" for k in columns)
            c.execute(f"UPDATE work_sessions SET {sets} WHERE id = ?", (*columns.values(), session_id))
            record_event(c, current["project_id"], "session_edited", source,
                         {"id": session_id, "fields": sorted(k.removesuffix("_json") for k in columns)})
        return self.get_session(session_id)

    # --------------------------------------------------------------- snapshot

    def snapshot(self, project_id: int) -> dict:
        """What you need to pick a project back up, from declared memory only."""
        open_session = self.db.query_one(
            "SELECT * FROM work_sessions WHERE project_id = ? AND ended_at IS NULL", (project_id,)
        )
        last = self.db.query_one(
            """SELECT * FROM work_sessions WHERE project_id = ? AND ended_at IS NOT NULL
               ORDER BY ended_at DESC, id DESC LIMIT 1""",
            (project_id,),
        )
        counts = {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) AS n FROM decisions WHERE project_id = ? GROUP BY status", (project_id,)
        )}
        return {
            "open_session": _session(open_session) if open_session else None,
            "last_session": _session(last) if last else None,
            "next_step": last["next_step"] if last else None,
            "open_questions": json.loads(last["open_questions_json"]) if last else [],
            "decisions_active": counts.get("active", 0),
            "decisions_review": counts.get("review", 0),
        }

    def _require_project(self, project_id: int) -> None:
        if not self.db.query_one("SELECT 1 FROM projects WHERE id = ?", (project_id,)):
            raise NotFound(f"project {project_id}")


def _session(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["open_questions"] = json.loads(d.pop("open_questions_json"))
    return d
