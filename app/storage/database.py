"""SQLite state store for gpt-local-code-operator.

Provides database initialisation and CRUD helpers for workspaces, processes,
and operations as defined in the plan (section 14).
"""

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

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
    return cast(sqlite3.Connection, _local.conn)


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


def delete_workspace(workspace_id: str) -> None:
    """Remove a workspace record and its associated processes/operations (used when discarding)."""
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM processes WHERE workspace_id = ?", (workspace_id,))
        conn.execute("DELETE FROM operations WHERE workspace_id = ?", (workspace_id,))
        conn.execute("DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,))
        conn.commit()
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
        (operation_id, workspace_id, tool_name, summary, int(success), _now_iso()),
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


def list_operations(workspace_id: str) -> list[dict[str, Any]]:
    """List audit operations for a workspace, oldest first."""
    conn = _get_connection()
    rows = conn.execute(
        """SELECT * FROM operations WHERE workspace_id = ?
           ORDER BY started_at ASC, operation_id ASC""",
        (workspace_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---- Internal ----


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
