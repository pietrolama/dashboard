"""Exceptions and the event writer shared by the registry and the memory log."""

from __future__ import annotations

import json
import sqlite3

from .db import utcnow


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


def record_event(c: sqlite3.Connection, project_id: int | None, event_type: str, source: str, payload: dict) -> None:
    c.execute(
        "INSERT INTO events (project_id, event_type, occurred_at, source, payload_json) VALUES (?, ?, ?, ?, ?)",
        (project_id, event_type, utcnow(), source, json.dumps(payload)),
    )
