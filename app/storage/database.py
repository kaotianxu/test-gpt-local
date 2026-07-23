"""SQLite state store for gpt-local-code-operator.

Provides database initialisation and CRUD helpers for workspaces, processes,
and operations as defined in the plan (section 14).
"""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.config import BASE_DIR

_DB_PATH: Path = BASE_DIR / "data" / "operator.db"

# Thread-local connections so multiple MCP tool calls don't share a cursor.
_local = threading.local()
_DB_LOCK = threading.RLock()


def _effective_db_path() -> Path:
    """Return the database selected for the current call/thread."""
    return cast(Path, getattr(_local, "override_path", _DB_PATH))


def _get_connection() -> sqlite3.Connection:
    """Get a thread-local database connection."""
    db_path = _effective_db_path()
    existing = getattr(_local, "conn", None)
    existing_path = getattr(_local, "db_path", None)
    if existing is not None and existing_path != str(db_path):
        try:
            existing.close()
        finally:
            _local.conn = None
            _local.db_path = None

    if not hasattr(_local, "conn") or _local.conn is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(db_path), timeout=30.0)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=30000")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _local.db_path = str(db_path)
    return cast(sqlite3.Connection, _local.conn)


def connect() -> sqlite3.Connection:
    """Return the active database connection for the current thread."""
    return _get_connection()


def database_identity() -> str:
    """Return a stable identity for the active database used by local notifiers."""
    return str(_effective_db_path().resolve())


class Database:
    """An isolated database handle suitable for dependency injection.

    The legacy module-level functions remain the production default.  A
    ``Database`` instance selects its own path for every operation, including
    calls made by process-manager watchdog threads, so tests and independent
    managers cannot leak connections or state into one another.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    @contextmanager
    def _activated(self) -> Any:
        previous = getattr(_local, "override_path", None)
        _local.override_path = self.path
        try:
            yield
        finally:
            if previous is None:
                try:
                    del _local.override_path
                except AttributeError:
                    pass
            else:
                _local.override_path = previous

    def connect(self) -> sqlite3.Connection:
        """Return this database's connection for the current thread."""
        with self._activated():
            return _get_connection()

    def init_db(self) -> None:
        """Initialize this database without changing the process default."""
        with self._activated():
            init_db()

    def close(self) -> None:
        """Close this database's connection on the current thread, if open."""
        if getattr(_local, "db_path", None) != str(self.path):
            return
        connection = getattr(_local, "conn", None)
        if connection is not None:
            connection.close()
        _local.conn = None
        _local.db_path = None

    def __getattr__(self, name: str) -> Any:
        """Bind a legacy CRUD helper to this instance's database path."""
        helper = globals().get(name)
        if not callable(helper) or name.startswith("_"):
            raise AttributeError(name)

        def bound(*args: Any, **kwargs: Any) -> Any:
            with self._activated():
                return helper(*args, **kwargs)

        return bound


def init_db(db_path: str | Path | None = None) -> None:
    """Create tables if they don't exist."""
    if db_path is not None:
        global _DB_PATH  # noqa: PLW0603
        _DB_PATH = Path(db_path)
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = _get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id    TEXT PRIMARY KEY,
            project_id      TEXT NOT NULL,
            task_name       TEXT NOT NULL,
            worktree_path   TEXT NOT NULL,
            base_commit     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            created_at      TEXT NOT NULL,
            last_accessed_at TEXT NOT NULL,
            closed_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS processes (
            process_id       TEXT PRIMARY KEY,
            workspace_id     TEXT NOT NULL,
            tool_name        TEXT NOT NULL,
            script_sha256    TEXT,
            script_preview   TEXT,
            working_directory TEXT,
            status           TEXT NOT NULL DEFAULT 'queued',
            pid              INTEGER,
            exit_code        INTEGER,
            started_at       TEXT,
            completed_at     TEXT,
            stdout_path      TEXT,
            stderr_path      TEXT,
            process_creation_identity TEXT,
            heartbeat        TEXT,
            last_output_offset INTEGER NOT NULL DEFAULT 0,
            job_object_identity TEXT,
            recovery_status  TEXT,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
        );

        CREATE TABLE IF NOT EXISTS operations (
            operation_id   TEXT PRIMARY KEY,
            workspace_id   TEXT,
            tool_name      TEXT NOT NULL,
            summary        TEXT,
            success        INTEGER NOT NULL DEFAULT 1,
            started_at     TEXT NOT NULL,
            completed_at   TEXT,
            request_id     TEXT,
            actor          TEXT,
            input_summary  TEXT,
            result_status  TEXT,
            duration_ms    INTEGER,
            error_code     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_workspace_project
            ON workspaces(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_process_workspace
            ON processes(workspace_id, status);
        CREATE INDEX IF NOT EXISTS idx_operation_workspace
            ON operations(workspace_id);

        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id       TEXT PRIMARY KEY,
            workspace_id      TEXT NOT NULL,
            kind              TEXT NOT NULL,
            path              TEXT NOT NULL,
            mime_type         TEXT,
            size_bytes        INTEGER NOT NULL DEFAULT 0,
            sha256            TEXT,
            source_type       TEXT,
            source_process_id TEXT,
            created_at        TEXT NOT NULL,
            metadata          TEXT,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
        );
        CREATE INDEX IF NOT EXISTS idx_artifact_workspace
            ON artifacts(workspace_id, kind);

        CREATE TABLE IF NOT EXISTS plans (
            plan_id        TEXT PRIMARY KEY,
            workspace_id   TEXT NOT NULL UNIQUE,
            explanation    TEXT NOT NULL,
            revision       INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
        );

        CREATE TABLE IF NOT EXISTS plan_steps (
            step_id        TEXT NOT NULL,
            plan_id        TEXT NOT NULL,
            step_index     INTEGER NOT NULL,
            text           TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending',
            evidence       TEXT,
            blocked_reason TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            PRIMARY KEY (plan_id, step_id),
            FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
        );
        CREATE INDEX IF NOT EXISTS idx_plan_workspace
            ON plans(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_plan_step_status
            ON plan_steps(plan_id, status);

        CREATE TABLE IF NOT EXISTS events (
            event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id     TEXT,
            workspace_id   TEXT,
            process_id     TEXT,
            event_type     TEXT NOT NULL,
            sequence       INTEGER,
            payload_json   TEXT NOT NULL DEFAULT '{}',
            created_at     TEXT NOT NULL,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
        );
        CREATE INDEX IF NOT EXISTS idx_events_workspace_event
            ON events(workspace_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_events_process_event
            ON events(process_id, event_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_process_sequence
            ON events(process_id, sequence)
            WHERE process_id IS NOT NULL AND sequence IS NOT NULL;

        CREATE TABLE IF NOT EXISTS event_retention_state (
            workspace_id   TEXT PRIMARY KEY,
            expired_through INTEGER NOT NULL DEFAULT 0
        );
    """)
    workspace_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(workspaces)").fetchall()
    }
    if "main_head_at_creation" not in workspace_columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN main_head_at_creation TEXT")
    if "main_status_at_creation" not in workspace_columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN main_status_at_creation TEXT")
    if "main_status_sha256_at_creation" not in workspace_columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN main_status_sha256_at_creation TEXT")
    if "revision" not in workspace_columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    if "current_head" not in workspace_columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN current_head TEXT")
    if "last_patch_at" not in workspace_columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN last_patch_at TEXT")
    if "last_check" not in workspace_columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN last_check TEXT")
    if "changed_files" not in workspace_columns:
        conn.execute("ALTER TABLE workspaces ADD COLUMN changed_files TEXT")

    process_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(processes)").fetchall()
    }
    process_migrations = {
        "process_creation_identity": "TEXT",
        "heartbeat": "TEXT",
        "last_output_offset": "INTEGER NOT NULL DEFAULT 0",
        "job_object_identity": "TEXT",
        "recovery_status": "TEXT",
    }
    for column, column_type in process_migrations.items():
        if column not in process_columns:
            conn.execute(f"ALTER TABLE processes ADD COLUMN {column} {column_type}")

    operation_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(operations)").fetchall()
    }
    operation_migrations = {
        "request_id": "TEXT",
        "actor": "TEXT",
        "input_summary": "TEXT",
        "result_status": "TEXT",
        "duration_ms": "INTEGER",
        "error_code": "TEXT",
    }
    for column, column_type in operation_migrations.items():
        if column not in operation_columns:
            conn.execute(f"ALTER TABLE operations ADD COLUMN {column} {column_type}")
    legacy_statuses = conn.execute(
        """SELECT workspace_id, main_status_at_creation FROM workspaces
           WHERE main_status_at_creation IS NOT NULL
             AND main_status_sha256_at_creation IS NULL"""
    ).fetchall()
    for row in legacy_statuses:
        digest = hashlib.sha256(row["main_status_at_creation"].encode("utf-8")).hexdigest()
        conn.execute(
            """UPDATE workspaces
               SET main_status_sha256_at_creation = ?, main_status_at_creation = NULL
               WHERE workspace_id = ?""",
            (digest, row["workspace_id"]),
        )
    conn.commit()


# ---- Workspace helpers ----


def insert_workspace(
    workspace_id: str,
    project_id: str,
    task_name: str,
    worktree_path: str,
    base_commit: str,
    main_head_at_creation: str | None = None,
    main_status_sha256_at_creation: str | None = None,
) -> dict[str, Any]:
    """Insert a new workspace record."""
    now = _now_iso()
    conn = _get_connection()
    conn.execute(
        """INSERT INTO workspaces
           (workspace_id, project_id, task_name, worktree_path, base_commit,
            status, created_at, last_accessed_at, main_head_at_creation,
            main_status_sha256_at_creation, revision, current_head)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, 1, ?)""",
        (
            workspace_id,
            project_id,
            task_name,
            worktree_path,
            base_commit,
            now,
            now,
            main_head_at_creation,
            main_status_sha256_at_creation,
            base_commit,
        ),
    )
    conn.commit()
    record = get_workspace(workspace_id)
    if record is None:
        raise RuntimeError(f"failed to insert workspace: {workspace_id}")
    return record


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    """Return a workspace dict or None."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result.pop("main_status_at_creation", None)
    return result


def list_workspaces(project_id: str | None = None) -> list[dict[str, Any]]:
    """List workspaces, optionally filtered by project."""
    conn = _get_connection()
    if project_id:
        rows = conn.execute(
            "SELECT * FROM workspaces WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM workspaces ORDER BY created_at DESC").fetchall()
    results = [dict(r) for r in rows]
    for result in results:
        result.pop("main_status_at_creation", None)
    return results


def update_workspace_status(workspace_id: str, status: str) -> None:
    """Update workspace status (e.g. 'active' → 'closed')."""
    conn = _get_connection()
    now = _now_iso()
    if status == "closed":
        conn.execute(
            "UPDATE workspaces SET status = ?, closed_at = ? WHERE workspace_id = ?",
            (status, now, workspace_id),
        )
    else:
        conn.execute(
            "UPDATE workspaces SET status = ? WHERE workspace_id = ?",
            (status, workspace_id),
        )
    conn.commit()


def touch_workspace(workspace_id: str) -> None:
    """Update last_accessed_at to now."""
    conn = _get_connection()
    conn.execute(
        "UPDATE workspaces SET last_accessed_at = ? WHERE workspace_id = ?",
        (_now_iso(), workspace_id),
    )
    conn.commit()


def delete_workspace(workspace_id: str) -> bool:
    """Remove a workspace and all associated state atomically."""
    conn = _get_connection()
    with _DB_LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM plan_steps WHERE plan_id IN "
                "(SELECT plan_id FROM plans WHERE workspace_id = ?)",
                (workspace_id,),
            )
            conn.execute("DELETE FROM plans WHERE workspace_id = ?", (workspace_id,))
            conn.execute("DELETE FROM artifacts WHERE workspace_id = ?", (workspace_id,))
            conn.execute("DELETE FROM events WHERE workspace_id = ?", (workspace_id,))
            conn.execute(
                "DELETE FROM event_retention_state WHERE workspace_id = ?",
                (workspace_id,),
            )
            conn.execute("DELETE FROM processes WHERE workspace_id = ?", (workspace_id,))
            conn.execute("DELETE FROM operations WHERE workspace_id = ?", (workspace_id,))
            cursor = conn.execute(
                "DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise


def increment_workspace_revision(workspace_id: str) -> int:
    """Increment the revision counter and return the new value."""
    conn = _get_connection()
    conn.execute(
        "UPDATE workspaces SET revision = revision + 1 WHERE workspace_id = ?",
        (workspace_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT revision FROM workspaces WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    return int(row["revision"]) if row else 0


def update_workspace_metadata(workspace_id: str, **kwargs: str | None) -> None:
    """Update arbitrary workspace metadata fields."""
    if not kwargs:
        return
    conn = _get_connection()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [workspace_id]
    conn.execute(f"UPDATE workspaces SET {set_clause} WHERE workspace_id = ?", values)
    conn.commit()


# ---- Process helpers ----


def insert_process(
    process_id: str,
    workspace_id: str,
    tool_name: str,
    script_sha256: str | None = None,
    script_preview: str | None = None,
    working_directory: str | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> dict[str, Any]:
    """Insert a new process record."""
    conn = _get_connection()
    conn.execute(
        """INSERT INTO processes
           (process_id, workspace_id, tool_name, script_sha256,
            script_preview, working_directory, status, stdout_path, stderr_path)
           VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
        (
            process_id,
            workspace_id,
            tool_name,
            script_sha256,
            script_preview,
            working_directory,
            stdout_path,
            stderr_path,
        ),
    )
    conn.commit()
    record = get_process(process_id)
    if record is None:
        raise RuntimeError(f"failed to insert process: {process_id}")
    return record


def get_process(process_id: str) -> dict[str, Any] | None:
    """Return a process dict or None."""
    conn = _get_connection()
    row = conn.execute("SELECT * FROM processes WHERE process_id = ?", (process_id,)).fetchone()
    if row is None:
        return None
    return dict(row)


def list_processes(workspace_id: str) -> list[dict[str, Any]]:
    """List all recorded processes for a workspace, newest first."""
    conn = _get_connection()
    rows = conn.execute(
        """SELECT * FROM processes WHERE workspace_id = ?
           ORDER BY COALESCE(started_at, '') DESC, process_id DESC""",
        (workspace_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def interrupt_incomplete_processes() -> int:
    """Mark records orphaned by a previous service exit as interrupted."""
    conn = _get_connection()
    cursor = conn.execute(
        """UPDATE processes
           SET status = 'interrupted', completed_at = ?
           WHERE status IN ('queued', 'running')""",
        (_now_iso(),),
    )
    conn.commit()
    return int(cursor.rowcount)


def list_incomplete_processes() -> list[dict[str, Any]]:
    """Return process records that have not reached a terminal state."""
    conn = _get_connection()
    rows = conn.execute("SELECT * FROM processes WHERE status IN ('queued', 'running')").fetchall()
    return [dict(row) for row in rows]


def update_process_status(
    process_id: str,
    status: str,
    pid: int | None = None,
    exit_code: int | None = None,
    completed_at: str | None = None,
) -> None:
    """Update process status and optional fields."""
    conn = _get_connection()
    fields: dict[str, object] = {"status": status}
    if pid is not None:
        fields["pid"] = pid
    if exit_code is not None:
        fields["exit_code"] = exit_code
    if completed_at is not None:
        fields["completed_at"] = completed_at
    if status == "running":
        fields["started_at"] = _now_iso()

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [process_id]
    conn.execute(f"UPDATE processes SET {set_clause} WHERE process_id = ?", values)
    conn.commit()


def update_process_runtime(
    process_id: str,
    *,
    pid: int | None = None,
    process_creation_identity: str | None = None,
    heartbeat: str | None = None,
    last_output_offset: int | None = None,
    job_object_identity: str | None = None,
    recovery_status: str | None = None,
) -> None:
    """Update persisted runtime identity and recovery metadata."""
    fields: dict[str, object] = {}
    if pid is not None:
        fields["pid"] = pid
    if process_creation_identity is not None:
        fields["process_creation_identity"] = process_creation_identity
    if heartbeat is not None:
        fields["heartbeat"] = heartbeat
    if last_output_offset is not None:
        if last_output_offset < 0:
            raise ValueError("last_output_offset must not be negative")
        fields["last_output_offset"] = last_output_offset
    if job_object_identity is not None:
        fields["job_object_identity"] = job_object_identity
    if recovery_status is not None:
        fields["recovery_status"] = recovery_status
    if not fields:
        return
    conn = _get_connection()
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE processes SET {set_clause} WHERE process_id = ?",
        [*fields.values(), process_id],
    )
    conn.commit()


# ---- Operation helpers ----


def log_operation(
    operation_id: str,
    tool_name: str,
    summary: str,
    workspace_id: str | None = None,
    success: bool = True,
    request_id: str | None = None,
    actor: str | None = "mcp",
    input_summary: str | None = None,
    result_status: str | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> None:
    """Record a completed, audit-friendly operation log entry.

    ``input_summary`` is deliberately caller-supplied so tools can record a
    redacted summary instead of raw prompts, credentials, or process input.
    """
    conn = _get_connection()
    values = (
        operation_id,
        workspace_id,
        tool_name,
        summary,
        int(success),
        _now_iso(),
        _now_iso(),
        request_id,
        actor,
        input_summary,
        result_status or ("success" if success else "error"),
        duration_ms,
        error_code,
    )
    try:
        conn.execute(
            """INSERT INTO operations
               (operation_id, workspace_id, tool_name, summary, success, started_at,
                completed_at, request_id, actor, input_summary, result_status,
                duration_ms, error_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
    except sqlite3.OperationalError as exc:
        # Process-manager unit tests and upgrades can briefly encounter a
        # legacy audit table. Audit logging must never break process cleanup.
        if "no column named" not in str(exc).lower():
            raise
        conn.execute(
            """INSERT INTO operations
               (operation_id, workspace_id, tool_name, summary, success,
                started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            values[:7],
        )
    conn.commit()


def complete_operation(
    operation_id: str,
    *,
    duration_ms: int | None = None,
    result_status: str | None = None,
    error_code: str | None = None,
) -> None:
    """Mark an operation as completed."""
    conn = _get_connection()
    fields: dict[str, object] = {"completed_at": _now_iso()}
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
    if result_status is not None:
        fields["result_status"] = result_status
    if error_code is not None:
        fields["error_code"] = error_code
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE operations SET {set_clause} WHERE operation_id = ?",
        [*fields.values(), operation_id],
    )
    conn.commit()


def list_operations(workspace_id: str) -> list[dict[str, Any]]:
    """List audit operations for a workspace, oldest first."""
    conn = _get_connection()
    rows = conn.execute(
        """SELECT * FROM operations WHERE workspace_id = ?
           ORDER BY started_at ASC, operation_id ASC""",
        (workspace_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---- Artifact helpers --------------------------------------------------------


def insert_artifact(
    artifact_id: str,
    workspace_id: str,
    kind: str,
    path: str,
    *,
    mime_type: str | None = None,
    size_bytes: int = 0,
    sha256: str | None = None,
    source_type: str | None = None,
    source_process_id: str | None = None,
    metadata: str | None = None,
) -> dict[str, Any]:
    """Insert a new artifact record."""
    conn = _get_connection()
    conn.execute(
        """INSERT INTO artifacts
           (artifact_id, workspace_id, kind, path, mime_type, size_bytes,
            sha256, source_type, source_process_id, created_at, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            artifact_id,
            workspace_id,
            kind,
            path,
            mime_type,
            size_bytes,
            sha256,
            source_type,
            source_process_id,
            _now_iso(),
            metadata,
        ),
    )
    conn.commit()
    record = get_artifact(artifact_id)
    if record is None:
        raise RuntimeError(f"failed to insert artifact: {artifact_id}")
    return record


def get_artifact(artifact_id: str) -> dict[str, Any] | None:
    """Return an artifact dict or None."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def find_artifact(
    workspace_id: str,
    path: str,
    sha256: str | None,
) -> dict[str, Any] | None:
    """Return an existing artifact with the same workspace path and hash."""
    conn = _get_connection()
    if sha256 is None:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE workspace_id = ? AND path = ? "
            "AND sha256 IS NULL ORDER BY created_at DESC LIMIT 1",
            (workspace_id, path),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE workspace_id = ? AND path = ? "
            "AND sha256 = ? ORDER BY created_at DESC LIMIT 1",
            (workspace_id, path, sha256),
        ).fetchone()
    return dict(row) if row is not None else None


def list_artifacts(
    workspace_id: str,
    *,
    kind: str | None = None,
    path_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """List artifacts for a workspace, optionally filtered."""
    conn = _get_connection()
    query = "SELECT * FROM artifacts WHERE workspace_id = ?"
    params: list[str] = [workspace_id]
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind)
    if path_prefix is not None:
        query += " AND path LIKE ?"
        params.append(path_prefix + "%")
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def list_artifacts_for_process(
    workspace_id: str,
    process_id: str,
) -> list[dict[str, Any]]:
    """Return artifacts produced by one process in one workspace."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE workspace_id = ? AND source_process_id = ? "
            "ORDER BY created_at ASC",
            (workspace_id, process_id),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return []
    return [dict(row) for row in rows]


def delete_artifact(artifact_id: str) -> bool:
    """Delete an artifact record. Returns True if a row was deleted."""
    conn = _get_connection()
    cursor = conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
    conn.commit()
    return cursor.rowcount > 0


def delete_artifacts_for_workspace(workspace_id: str) -> int:
    """Delete all artifacts for a workspace. Returns the number deleted."""
    conn = _get_connection()
    cursor = conn.execute(
        "DELETE FROM artifacts WHERE workspace_id = ?", (workspace_id,)
    )
    conn.commit()
    return cursor.rowcount


def count_artifacts(workspace_id: str) -> dict[str, Any]:
    """Return artifact count summary for a workspace."""
    conn = _get_connection()
    total = conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()[0]
    kinds = conn.execute(
        """SELECT kind, COUNT(*) as cnt FROM artifacts
           WHERE workspace_id = ? GROUP BY kind""",
        (workspace_id,),
    ).fetchall()
    return {
        "count": total,
        "kinds": {row["kind"]: row["cnt"] for row in kinds},
    }


# ---- Plan helpers ------------------------------------------------------------


def get_plan(workspace_id: str) -> dict[str, Any] | None:
    """Return the plan for a workspace, or None if not found."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM plans WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def insert_plan(
    workspace_id: str,
    explanation: str,
) -> dict[str, Any]:
    """Insert a new plan record."""
    import secrets

    plan_id = "plan_" + secrets.token_hex(8)
    now = _now_iso()
    conn = _get_connection()
    conn.execute(
        """INSERT INTO plans
           (plan_id, workspace_id, explanation, revision, created_at, updated_at)
           VALUES (?, ?, ?, 1, ?, ?)""",
        (plan_id, workspace_id, explanation, now, now),
    )
    conn.commit()
    record = get_plan(workspace_id)
    if record is None:
        raise RuntimeError(f"failed to insert plan for workspace: {workspace_id}")
    return record


def replace_plan(
    workspace_id: str,
    explanation: str,
    steps: list[dict[str, Any]],
    *,
    expected_revision: int | None = None,
) -> int:
    """Create or atomically replace a workspace plan.

    The plan row, revision, and all step rows are committed as one SQLite
    transaction.  A stale ``expected_revision`` therefore cannot leave a
    plan with a new revision and old or partially replaced steps.
    """
    import secrets

    conn = _get_connection()
    with _DB_LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT plan_id, revision FROM plans WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            now = _now_iso()
            if existing is None:
                if expected_revision is not None:
                    raise ValueError("expected_revision must be omitted when creating a plan")
                plan_id = "plan_" + secrets.token_hex(8)
                revision = 1
                conn.execute(
                    """INSERT INTO plans
                       (plan_id, workspace_id, explanation, revision, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (plan_id, workspace_id, explanation, revision, now, now),
                )
            else:
                current_revision = int(existing["revision"])
                if expected_revision is None:
                    raise ValueError(
                        "expected_revision is required when updating an existing plan"
                    )
                if current_revision != expected_revision:
                    raise ValueError(
                        f"plan revision conflict: expected {expected_revision}, "
                        f"current is {current_revision}"
                    )
                plan_id = str(existing["plan_id"])
                revision = current_revision + 1
                conn.execute(
                    "UPDATE plans SET explanation = ?, revision = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND revision = ?",
                    (explanation, revision, now, workspace_id, expected_revision),
                )

            conn.execute("DELETE FROM plan_steps WHERE plan_id = ?", (plan_id,))
            for index, step in enumerate(steps):
                evidence = step.get("evidence")
                evidence_json = json.dumps(evidence, ensure_ascii=False) if evidence else None
                conn.execute(
                    """INSERT INTO plan_steps
                       (step_id, plan_id, step_index, text, status, evidence,
                        blocked_reason, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        step["id"],
                        plan_id,
                        index,
                        step["text"],
                        step.get("status", "pending"),
                        evidence_json,
                        step.get("blocked_reason"),
                        now,
                        now,
                    ),
                )
            conn.commit()
            return revision
        except Exception:
            conn.rollback()
            raise


def update_plan_revision(workspace_id: str, expected_revision: int) -> int:
    """Atomically increment the plan revision.

    Returns the new revision, or raises ValueError on conflict.
    """
    conn = _get_connection()
    cursor = conn.execute(
        """UPDATE plans SET revision = revision + 1, updated_at = ?
           WHERE workspace_id = ? AND revision = ?""",
        (_now_iso(), workspace_id, expected_revision),
    )
    conn.commit()
    if cursor.rowcount == 0:
        plan = get_plan(workspace_id)
        if plan is None:
            raise ValueError("plan not found")
        raise ValueError(
            f"plan revision conflict: expected {expected_revision}, "
            f"current is {plan['revision']}"
        )
    return expected_revision + 1


def update_plan_explanation(workspace_id: str, explanation: str) -> None:
    """Update the plan explanation text."""
    conn = _get_connection()
    conn.execute(
        "UPDATE plans SET explanation = ?, updated_at = ? WHERE workspace_id = ?",
        (explanation, _now_iso(), workspace_id),
    )
    conn.commit()


def delete_plan(workspace_id: str) -> bool:
    """Delete a plan and its steps. Returns True if deleted."""
    conn = _get_connection()
    conn.execute(
        "DELETE FROM plan_steps WHERE plan_id IN "
        "(SELECT plan_id FROM plans WHERE workspace_id = ?)",
        (workspace_id,),
    )
    cursor = conn.execute("DELETE FROM plans WHERE workspace_id = ?", (workspace_id,))
    conn.commit()
    return cursor.rowcount > 0


def get_plan_steps(workspace_id: str) -> list[dict[str, Any]]:
    """Return all steps for a workspace plan, ordered by step_index."""
    conn = _get_connection()
    rows = conn.execute(
        """SELECT s.* FROM plan_steps s
           JOIN plans p ON s.plan_id = p.plan_id
           WHERE p.workspace_id = ?
           ORDER BY s.step_index ASC""",
        (workspace_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def insert_plan_step(
    plan_id: str,
    step_id: str,
    step_index: int,
    text: str,
    status: str = "pending",
) -> dict[str, Any]:
    """Insert a new plan step."""
    now = _now_iso()
    conn = _get_connection()
    conn.execute(
        """INSERT INTO plan_steps
           (step_id, plan_id, step_index, text, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (step_id, plan_id, step_index, text, status, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM plan_steps WHERE plan_id = ? AND step_id = ?",
        (plan_id, step_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"failed to insert step: {step_id}")
    return dict(row)


def update_plan_step(
    plan_id: str,
    step_id: str,
    *,
    status: str | None = None,
    evidence: str | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any] | None:
    """Update a plan step's status, evidence, or blocked_reason."""
    fields: dict[str, object] = {}
    if status is not None:
        fields["status"] = status
    if evidence is not None:
        fields["evidence"] = evidence
    if blocked_reason is not None:
        fields["blocked_reason"] = blocked_reason
    if not fields:
        return None

    fields["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [plan_id, step_id]

    conn = _get_connection()
    conn.execute(
        f"UPDATE plan_steps SET {set_clause} WHERE plan_id = ? AND step_id = ?",
        values,
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM plan_steps WHERE plan_id = ? AND step_id = ?",
        (plan_id, step_id),
    ).fetchone()
    return dict(row) if row else None


def update_plan_step_with_revision(
    workspace_id: str,
    step_id: str,
    *,
    status: str,
    evidence: str | None = None,
    blocked_reason: str | None = None,
    expected_revision: int | None = None,
) -> int:
    """Update one step and optionally bump the plan revision atomically."""
    conn = _get_connection()
    with _DB_LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = conn.execute(
                "SELECT plan_id, revision FROM plans WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if plan is None:
                raise ValueError("plan not found")

            step = conn.execute(
                "SELECT status FROM plan_steps WHERE plan_id = ? AND step_id = ?",
                (plan["plan_id"], step_id),
            ).fetchone()
            if step is None:
                raise ValueError(f"step not found: {step_id!r}")

            if status == "in_progress":
                other = conn.execute(
                    "SELECT step_id FROM plan_steps WHERE plan_id = ? "
                    "AND status = 'in_progress' AND step_id <> ? LIMIT 1",
                    (plan["plan_id"], step_id),
                ).fetchone()
                if other is not None:
                    raise ValueError(
                        f"step {other['step_id']!r} is already 'in_progress'; "
                        "mark it completed or blocked first"
                    )

            current_revision = int(plan["revision"])
            new_revision = current_revision
            if expected_revision is not None:
                if current_revision != expected_revision:
                    raise ValueError(
                        f"plan revision conflict: expected {expected_revision}, "
                        f"current is {current_revision}"
                    )
                new_revision += 1
                conn.execute(
                    "UPDATE plans SET revision = ?, updated_at = ? WHERE workspace_id = ? "
                    "AND revision = ?",
                    (new_revision, _now_iso(), workspace_id, expected_revision),
                )

            conn.execute(
                """UPDATE plan_steps
                   SET status = ?, evidence = ?, blocked_reason = ?, updated_at = ?
                   WHERE plan_id = ? AND step_id = ?""",
                (
                    status,
                    evidence,
                    blocked_reason,
                    _now_iso(),
                    plan["plan_id"],
                    step_id,
                ),
            )
            conn.commit()
            return new_revision
        except Exception:
            conn.rollback()
            raise


def delete_plan_steps(plan_id: str) -> int:
    """Delete all steps for a plan. Returns the number deleted."""
    conn = _get_connection()
    cursor = conn.execute("DELETE FROM plan_steps WHERE plan_id = ?", (plan_id,))
    conn.commit()
    return cursor.rowcount


# ---- Internal ----


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
