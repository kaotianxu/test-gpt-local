"""SQLite state store for gpt-local-code-operator.

Provides database initialisation and CRUD helpers for workspaces, processes,
and operations as defined in the plan (section 14).
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

_DB_PATH: Path = BASE_DIR / "data" / "operator.db"

# Thread-local connections so multiple MCP tool calls don't share a cursor.
_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    """Get a thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(_DB_PATH))
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


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
            FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
        );

        CREATE TABLE IF NOT EXISTS operations (
            operation_id   TEXT PRIMARY KEY,
            workspace_id   TEXT,
            tool_name      TEXT NOT NULL,
            summary        TEXT,
            success        INTEGER NOT NULL DEFAULT 1,
            started_at     TEXT NOT NULL,
            completed_at   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_workspace_project
            ON workspaces(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_process_workspace
            ON processes(workspace_id, status);
        CREATE INDEX IF NOT EXISTS idx_operation_workspace
            ON operations(workspace_id);
    """)
    conn.commit()


# ---- Workspace helpers ----

def insert_workspace(
    workspace_id: str,
    project_id: str,
    task_name: str,
    worktree_path: str,
    base_commit: str,
) -> dict[str, Any]:
    """Insert a new workspace record."""
    now = _now_iso()
    conn = _get_connection()
    conn.execute(
        """INSERT INTO workspaces
           (workspace_id, project_id, task_name, worktree_path, base_commit,
            status, created_at, last_accessed_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
        (workspace_id, project_id, task_name, worktree_path,
         base_commit, now, now),
    )
    conn.commit()
    return get_workspace(workspace_id)


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    """Return a workspace dict or None."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_workspaces(project_id: str | None = None) -> list[dict[str, Any]]:
    """List workspaces, optionally filtered by project."""
    conn = _get_connection()
    if project_id:
        rows = conn.execute(
            "SELECT * FROM workspaces WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM workspaces ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


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


def delete_workspace(workspace_id: str) -> None:
    """Remove a workspace record (used when discarding)."""
    conn = _get_connection()
    conn.execute("DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,))
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
        (process_id, workspace_id, tool_name, script_sha256,
         script_preview, working_directory, stdout_path, stderr_path),
    )
    conn.commit()
    return get_process(process_id)


def get_process(process_id: str) -> dict[str, Any] | None:
    """Return a process dict or None."""
    conn = _get_connection()
    row = conn.execute(
        "SELECT * FROM processes WHERE process_id = ?", (process_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def update_process_status(
    process_id: str,
    status: str,
    pid: int | None = None,
    exit_code: int | None = None,
    completed_at: str | None = None,
) -> None:
    """Update process status and optional fields."""
    conn = _get_connection()
    fields = {"status": status}
    if pid is not None:
        fields["pid"] = pid
    if exit_code is not None:
        fields["exit_code"] = exit_code
    if completed_at is not None:
        fields["completed_at"] = completed_at
    if status == "running" and pid is not None:
        fields["started_at"] = _now_iso()

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [process_id]
    conn.execute(f"UPDATE processes SET {set_clause} WHERE process_id = ?", values)
    conn.commit()


# ---- Operation helpers ----

def log_operation(
    operation_id: str,
    tool_name: str,
    summary: str,
    workspace_id: str | None = None,
    success: bool = True,
) -> None:
    """Record an operation log entry."""
    conn = _get_connection()
    conn.execute(
        """INSERT INTO operations
           (operation_id, workspace_id, tool_name, summary, success, started_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (operation_id, workspace_id, tool_name, summary,
         int(success), _now_iso()),
    )
    conn.commit()


def complete_operation(operation_id: str) -> None:
    """Mark an operation as completed."""
    conn = _get_connection()
    conn.execute(
        "UPDATE operations SET completed_at = ? WHERE operation_id = ?",
        (_now_iso(), operation_id),
    )
    conn.commit()


# ---- Internal ----

def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()