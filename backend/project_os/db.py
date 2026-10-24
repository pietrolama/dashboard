"""SQLite persistence: connection handling and schema migrations."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Each entry is applied once, in order. Never edit an applied migration:
# append a new one instead.
MIGRATIONS: list[str] = [
    # 1 — Project Registry
    """
    CREATE TABLE roots (
        id          INTEGER PRIMARY KEY,
        path        TEXT NOT NULL UNIQUE,
        max_depth   INTEGER NOT NULL DEFAULT 3,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE projects (
        id                INTEGER PRIMARY KEY,
        name              TEXT NOT NULL,
        slug              TEXT NOT NULL UNIQUE,
        description       TEXT,
        -- declared by the user
        lifecycle         TEXT NOT NULL DEFAULT 'active'
                          CHECK (lifecycle IN ('active', 'paused', 'archived')),
        phase             TEXT
                          CHECK (phase IS NULL OR phase IN
                                 ('idea', 'prototype', 'development', 'stabilization', 'release')),
        priority          INTEGER NOT NULL DEFAULT 0,
        origin            TEXT NOT NULL CHECK (origin IN ('discovered', 'manual')),
        -- observed by the scanner
        last_activity_at  TEXT,
        last_observed_at  TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );

    CREATE TABLE repositories (
        id               INTEGER PRIMARY KEY,
        project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        root_id          INTEGER REFERENCES roots(id) ON DELETE SET NULL,
        path             TEXT NOT NULL UNIQUE,
        vcs              TEXT NOT NULL CHECK (vcs IN ('git', 'none')),
        remote_url       TEXT,
        default_branch   TEXT,
        last_scanned_at  TEXT,
        missing_since    TEXT
    );

    -- Append-only observations. A new row is written only when the observed
    -- payload changes (content_hash), so rescans are idempotent.
    CREATE TABLE evidence (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        source_type   TEXT NOT NULL,
        source_ref    TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        observed_at   TEXT NOT NULL,
        payload_json  TEXT NOT NULL
    );
    CREATE INDEX evidence_latest ON evidence (project_id, source_type, source_ref, id DESC);

    CREATE TABLE events (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        event_type    TEXT NOT NULL,
        occurred_at   TEXT NOT NULL,
        source        TEXT NOT NULL,
        payload_json  TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX events_by_project ON events (project_id, id DESC);

    CREATE TABLE scan_runs (
        id           INTEGER PRIMARY KEY,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        status       TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
        stats_json   TEXT NOT NULL DEFAULT '{}',
        errors_json  TEXT NOT NULL DEFAULT '[]'
    );

    CREATE VIRTUAL TABLE projects_fts USING fts5 (
        name, description, readme
    );
    """,
    # 2 — Decision log and work sessions
    """
    -- Decisions are never deleted: a change of mind supersedes the old entry,
    -- so the reasoning behind past choices stays readable.
    CREATE TABLE decisions (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind           TEXT NOT NULL DEFAULT 'decision'
                       CHECK (kind IN ('decision', 'constraint')),
        title          TEXT NOT NULL,
        rationale      TEXT,
        alternatives   TEXT,
        status         TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'review', 'superseded')),
        supersedes_id  INTEGER REFERENCES decisions(id),
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE INDEX decisions_by_project ON decisions (project_id, status);

    CREATE TABLE work_sessions (
        id                   INTEGER PRIMARY KEY,
        project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        started_at           TEXT NOT NULL,
        ended_at             TEXT,
        goal                 TEXT NOT NULL,
        outcome              TEXT,
        open_questions_json  TEXT NOT NULL DEFAULT '[]',
        next_step            TEXT
    );
    CREATE INDEX sessions_by_project ON work_sessions (project_id, started_at DESC);
    -- At most one session in progress per project.
    CREATE UNIQUE INDEX one_open_session ON work_sessions (project_id) WHERE ended_at IS NULL;
    """,
    # 3 — Scan ignore list
    """
    -- Glob patterns, matched against each candidate's path relative to its
    -- root (and its absolute path). A matching directory is never descended
    -- into, so nested repositories under it are skipped too. Used to keep
    -- vendored dependencies and archived trees out of the registry.
    CREATE TABLE scan_ignores (
        id          INTEGER PRIMARY KEY,
        pattern     TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    );
    """,
    # 4 — Team runs: Claude Code and Antigravity working on one request
    """
    CREATE TABLE team_runs (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        request        TEXT NOT NULL,
        status         TEXT NOT NULL CHECK (status IN
                       ('planning', 'plan_ready', 'running', 'integrating', 'conflict',
                        'review', 'applied', 'discarded', 'failed')),
        repo_path      TEXT NOT NULL,
        base_branch    TEXT,
        base_commit    TEXT NOT NULL,
        plan_summary   TEXT,
        tmux_session   TEXT,
        diffstat       TEXT,
        review         TEXT,
        error          TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE INDEX team_runs_by_project ON team_runs (project_id, id DESC);

    -- One row per agent taking part in a run, created when the run starts.
    CREATE TABLE team_members (
        run_id    INTEGER NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
        agent     TEXT NOT NULL CHECK (agent IN ('claude', 'agy')),
        worktree  TEXT NOT NULL,
        branch    TEXT NOT NULL,
        pane      TEXT,
        status    TEXT NOT NULL DEFAULT 'working' CHECK (status IN ('working', 'done')),
        summary   TEXT,
        PRIMARY KEY (run_id, agent)
    );

    CREATE TABLE team_tasks (
        id               INTEGER PRIMARY KEY,
        run_id           INTEGER NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
        position         INTEGER NOT NULL,
        agent            TEXT NOT NULL CHECK (agent IN ('claude', 'agy')),
        title            TEXT NOT NULL,
        details          TEXT,
        -- task ids of the same run
        depends_on_json  TEXT NOT NULL DEFAULT '[]',
        status           TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'doing', 'done', 'blocked')),
        summary          TEXT,
        updated_at       TEXT NOT NULL
    );
    CREATE INDEX team_tasks_by_run ON team_tasks (run_id, position);

    CREATE TABLE team_messages (
        id          INTEGER PRIMARY KEY,
        run_id      INTEGER NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
        sender      TEXT NOT NULL,
        recipient   TEXT NOT NULL,
        body        TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX team_messages_by_run ON team_messages (run_id, id);
    """,
    # 5 — Declared relations between projects (the radial map)
    """
    CREATE TABLE project_links (
        id          INTEGER PRIMARY KEY,
        source_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        target_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK (kind IN ('depends_on', 'part_of', 'evolves_from', 'related')),
        note        TEXT,
        created_at  TEXT NOT NULL,
        CHECK (source_id != target_id),
        UNIQUE (source_id, target_id, kind)
    );
    CREATE INDEX project_links_by_target ON project_links (target_id);
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper around a single SQLite file.

    A lock serialises writers: the API and the background scheduler share
    one database, and SQLite allows one writer at a time anyway.
    """

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode: transactions are always explicit (see transaction()).
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        self.migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        with self.lock:
            current = self.schema_version()
            for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                self._conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            try:
                self._conn.execute("BEGIN")
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self._conn.close()
