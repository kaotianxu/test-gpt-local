"""Durable workspace event stream with bounded local long polling."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any, TypeAlias, cast

from app.storage import database as default_database

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

EVENT_TYPES = frozenset(
    {
        "tool.queued",
        "tool.started",
        "process.output",
        "process.exited",
        "artifact.created",
        "check.completed",
    }
)
TERMINAL_PROCESS_STATUSES = frozenset(
    {
        "passed",
        "failed",
        "timed_out",
        "cancelled",
        "resource_exhausted",
        "interrupted",
        "lost",
        "recovery_required",
    }
)
_CURSOR_RE = re.compile(r"^evt1_([0-9a-f]{1,16})$")
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "authorization_header",
        "api_key",
        "apikey",
        "cookie",
        "environment",
        "env",
        "script",
        "command",
    }
)


class EventStoreError(RuntimeError):
    """Base error raised by event stream operations."""


class InvalidCursorError(EventStoreError):
    """Raised when a cursor cannot be decoded."""


class CursorExpiredError(EventStoreError):
    """Raised when retention removed events required by a cursor."""

    def __init__(self, recovery_cursor: str) -> None:
        super().__init__("event cursor points to history removed by retention")
        self.recovery_cursor = recovery_cursor


class EventWaitLimitError(EventStoreError):
    """Raised when the bounded long-poll waiter limit is exhausted."""


@dataclass
class _Notifier:
    condition: threading.Condition
    generation: int = 0
    waiters: int = 0
    shutting_down: bool = False


_NOTIFIERS: dict[str, _Notifier] = {}
_NOTIFIERS_LOCK = threading.Lock()


def encode_cursor(event_id: int) -> str:
    """Encode an event id as an opaque, versioned cursor."""
    if event_id < 0:
        raise ValueError("event_id must not be negative")
    return f"evt1_{event_id:x}"


def decode_cursor(cursor: str) -> int:
    """Decode a cursor, rejecting unknown versions and malformed values."""
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise InvalidCursorError("invalid event cursor")
    return int(match.group(1), 16)


class EventStore:
    """SQLite-backed append-only event stream.

    SQLite is the source of truth. A process-local condition only wakes
    long-poll readers early; restart-safe resume is provided by cursors.
    """

    def __init__(
        self,
        database: Any | None = None,
        *,
        max_payload_bytes: int = 16_384,
        max_page_size: int = 500,
        max_wait_seconds: float = 25.0,
        max_waiters: int = 32,
    ) -> None:
        self._database = database if database is not None else default_database
        self.max_payload_bytes = max_payload_bytes
        self.max_page_size = max_page_size
        self.max_wait_seconds = max_wait_seconds
        self.max_waiters = max_waiters
        if min(max_payload_bytes, max_page_size, max_waiters) < 1:
            raise ValueError("event store limits must be positive")
        if max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")
        identity = self._database_identity()
        with _NOTIFIERS_LOCK:
            self._notifier = _NOTIFIERS.setdefault(
                identity, _Notifier(threading.Condition(threading.RLock()))
            )
            self._notifier.shutting_down = False

    def append(
        self,
        event_type: str,
        *,
        request_id: str | None = None,
        workspace_id: str | None = None,
        process_id: str | None = None,
        payload: Mapping[str, JSONValue] | None = None,
    ) -> dict[str, Any]:
        """Append and return one event."""
        self._validate_event(event_type, workspace_id, process_id, payload)
        connection = self._database.connect()
        with self._notifier.condition:
            try:
                connection.execute("BEGIN IMMEDIATE")
                event = self._insert_locked(
                    connection,
                    event_type,
                    request_id=request_id,
                    workspace_id=workspace_id,
                    process_id=process_id,
                    payload=payload,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._notify_locked()
        return event

    def transition_process(
        self,
        process_id: str,
        status: str,
        *,
        exit_code: int | None = None,
        completed_at: str | None = None,
        recovery_status: str | None = None,
        reason: str | None = None,
        request_id: str | None = None,
        output_offsets: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Atomically update a process terminal status and append its event."""
        if status not in TERMINAL_PROCESS_STATUSES:
            raise ValueError(f"not a terminal process status: {status}")
        connection = self._database.connect()
        with self._notifier.condition:
            try:
                connection.execute("BEGIN IMMEDIATE")
                record = connection.execute(
                    "SELECT workspace_id, status FROM processes WHERE process_id = ?",
                    (process_id,),
                ).fetchone()
                if record is None:
                    raise KeyError(f"process not found: {process_id}")
                existing_status = str(record["status"])
                if existing_status in TERMINAL_PROCESS_STATUSES:
                    connection.rollback()
                    existing = self._last_terminal_event(connection, process_id)
                    return existing or {
                        "process_id": process_id,
                        "type": "process.exited",
                        "payload": {"status": existing_status},
                    }
                fields: dict[str, Any] = {
                    "status": status,
                    "completed_at": completed_at or _now_iso(),
                }
                if exit_code is not None:
                    fields["exit_code"] = exit_code
                if recovery_status is not None:
                    fields["recovery_status"] = recovery_status
                set_clause = ", ".join(f"{name} = ?" for name in fields)
                connection.execute(
                    f"UPDATE processes SET {set_clause} WHERE process_id = ?",
                    [*fields.values(), process_id],
                )
                payload: dict[str, JSONValue] = {
                    "status": status,
                    "exit_code": exit_code,
                    "reason": reason or recovery_status,
                }
                if output_offsets:
                    payload["stdout_offset"] = int(output_offsets.get("stdout", 0))
                    payload["stderr_offset"] = int(output_offsets.get("stderr", 0))
                event = self._insert_locked(
                    connection,
                    "process.exited",
                    request_id=request_id,
                    workspace_id=str(record["workspace_id"]),
                    process_id=process_id,
                    payload=payload,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._notify_locked()
        return event

    def start_process(
        self,
        process_id: str,
        *,
        pid: int,
        tool_name: str,
        request_id: str | None = None,
        recovered: bool = False,
    ) -> dict[str, Any]:
        """Atomically mark a process running and append ``tool.started``."""
        connection = self._database.connect()
        with self._notifier.condition:
            try:
                connection.execute("BEGIN IMMEDIATE")
                record = connection.execute(
                    "SELECT workspace_id FROM processes WHERE process_id = ?",
                    (process_id,),
                ).fetchone()
                if record is None:
                    raise KeyError(f"process not found: {process_id}")
                connection.execute(
                    "UPDATE processes SET status = 'running', pid = ?, started_at = ? "
                    "WHERE process_id = ?",
                    (pid, _now_iso(), process_id),
                )
                event = self._insert_locked(
                    connection,
                    "tool.started",
                    request_id=request_id,
                    workspace_id=str(record["workspace_id"]),
                    process_id=process_id,
                    payload={
                        "tool_name": tool_name,
                        "pid": pid,
                        "working_directory_redacted": ".",
                        "recovered": recovered,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._notify_locked()
        return event

    def list_after(
        self,
        cursor: str | None,
        *,
        workspace_id: str,
        process_id: str | None = None,
        event_types: Sequence[str] | None = None,
        limit: int = 100,
        from_beginning: bool = False,
    ) -> dict[str, Any]:
        """Return one page after a cursor without waiting."""
        validated_types = self._validate_filters(event_types)
        page_limit = max(1, min(int(limit), self.max_page_size))
        after = self._resolve_after(cursor, workspace_id, from_beginning)
        self._check_retention(after, workspace_id)
        connection = self._database.connect()
        clauses = ["workspace_id = ?", "event_id > ?"]
        params: list[Any] = [workspace_id, after]
        if process_id is not None:
            clauses.append("process_id = ?")
            params.append(process_id)
        if validated_types:
            placeholders = ", ".join("?" for _ in validated_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(validated_types)
        rows = connection.execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} "
            "ORDER BY event_id ASC LIMIT ?",
            [*params, page_limit + 1],
        ).fetchall()
        has_more = len(rows) > page_limit
        selected = rows[:page_limit]
        events = [self._row_to_event(row) for row in selected]
        observed = int(selected[-1]["event_id"]) if selected else after
        return {
            "events": events,
            "cursor": encode_cursor(observed),
            "has_more": has_more,
            "timed_out": False,
        }

    def wait_after(
        self,
        cursor: str | None,
        *,
        workspace_id: str,
        process_id: str | None = None,
        event_types: Sequence[str] | None = None,
        limit: int = 100,
        timeout_seconds: float = 25.0,
        from_beginning: bool = False,
    ) -> dict[str, Any]:
        """Wait until a matching event exists, shutdown begins, or timeout."""
        timeout = max(0.0, min(float(timeout_seconds), self.max_wait_seconds))
        first = self.list_after(
            cursor,
            workspace_id=workspace_id,
            process_id=process_id,
            event_types=event_types,
            limit=limit,
            from_beginning=from_beginning,
        )
        if first["events"] or first["has_more"] or timeout == 0:
            return first
        stable_cursor = cast(str, first["cursor"])
        deadline = time.monotonic() + timeout
        notifier = self._notifier
        with notifier.condition:
            if notifier.waiters >= self.max_waiters:
                raise EventWaitLimitError("maximum event long-poll waiters reached")
            notifier.waiters += 1
            try:
                generation = notifier.generation
                while True:
                    page = self.list_after(
                        stable_cursor,
                        workspace_id=workspace_id,
                        process_id=process_id,
                        event_types=event_types,
                        limit=limit,
                    )
                    if page["events"] or page["has_more"]:
                        return page
                    if notifier.shutting_down:
                        page["shutdown"] = True
                        return page
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        page["timed_out"] = True
                        return page
                    if generation == notifier.generation:
                        notifier.condition.wait(timeout=remaining)
                    generation = notifier.generation
            finally:
                notifier.waiters -= 1

    def cleanup(
        self,
        *,
        retention_days: int,
        max_events_per_workspace: int,
    ) -> int:
        """Delete expired/excess events and persist per-workspace watermarks."""
        if retention_days < 0 or max_events_per_workspace < 1:
            raise ValueError("invalid event retention settings")
        connection = self._database.connect()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        deleted = 0
        with self._notifier.condition:
            try:
                connection.execute("BEGIN IMMEDIATE")
                workspaces = connection.execute(
                    "SELECT DISTINCT workspace_id FROM events WHERE workspace_id IS NOT NULL"
                ).fetchall()
                for row in workspaces:
                    workspace_id = str(row["workspace_id"])
                    ids = connection.execute(
                        """SELECT event_id FROM events
                           WHERE workspace_id = ?
                           ORDER BY event_id DESC LIMIT -1 OFFSET ?""",
                        (workspace_id, max_events_per_workspace),
                    ).fetchall()
                    count_cutoff = max((int(item["event_id"]) for item in ids), default=0)
                    time_row = connection.execute(
                        """SELECT COALESCE(MAX(event_id), 0) AS event_id FROM events
                           WHERE workspace_id = ? AND created_at < ?""",
                        (workspace_id, cutoff),
                    ).fetchone()
                    expired_through = max(count_cutoff, int(time_row["event_id"]))
                    if expired_through <= 0:
                        continue
                    cursor = connection.execute(
                        "DELETE FROM events WHERE workspace_id = ? AND event_id <= ?",
                        (workspace_id, expired_through),
                    )
                    deleted += int(cursor.rowcount)
                    connection.execute(
                        """INSERT INTO event_retention_state(workspace_id, expired_through)
                           VALUES (?, ?)
                           ON CONFLICT(workspace_id) DO UPDATE SET expired_through =
                           MAX(expired_through, excluded.expired_through)""",
                        (workspace_id, expired_through),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            if deleted:
                self._notify_locked()
        return deleted

    def shutdown(self) -> None:
        """Wake all local long-poll waiters for service shutdown."""
        with self._notifier.condition:
            self._notifier.shutting_down = True
            self._notify_locked()

    def _resolve_after(
        self, cursor: str | None, workspace_id: str, from_beginning: bool
    ) -> int:
        if cursor is not None:
            return decode_cursor(cursor)
        if from_beginning:
            return 0
        row = self._database.connect().execute(
            "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM events WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return int(row["event_id"])

    def _check_retention(self, after: int, workspace_id: str) -> None:
        row = self._database.connect().execute(
            "SELECT expired_through FROM event_retention_state WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        expired_through = int(row["expired_through"]) if row is not None else 0
        if after < expired_through:
            raise CursorExpiredError(encode_cursor(expired_through))

    def _insert_locked(
        self,
        connection: Any,
        event_type: str,
        *,
        request_id: str | None,
        workspace_id: str | None,
        process_id: str | None,
        payload: Mapping[str, JSONValue] | None,
    ) -> dict[str, Any]:
        sequence: int | None = None
        if process_id is not None:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM events WHERE process_id = ?",
                (process_id,),
            ).fetchone()
            sequence = int(row["sequence"])
        created_at = _now_iso()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        cursor = connection.execute(
            """INSERT INTO events
               (request_id, workspace_id, process_id, event_type, sequence,
                payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id,
                workspace_id,
                process_id,
                event_type,
                sequence,
                payload_json,
                created_at,
            ),
        )
        return {
            "event_id": int(cursor.lastrowid),
            "request_id": request_id,
            "workspace_id": workspace_id,
            "process_id": process_id,
            "type": event_type,
            "sequence": sequence,
            "created_at": created_at,
            "payload": dict(payload or {}),
        }

    def _validate_event(
        self,
        event_type: str,
        workspace_id: str | None,
        process_id: str | None,
        payload: Mapping[str, JSONValue] | None,
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {event_type}")
        if not workspace_id:
            raise ValueError("workspace_id is required")
        if process_id is not None and not process_id:
            raise ValueError("process_id must be non-empty")
        body = dict(payload or {})
        sensitive = _find_sensitive_key(body)
        if sensitive is not None:
            raise ValueError(f"sensitive event payload key is not allowed: {sensitive}")
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_payload_bytes:
            raise ValueError("event payload exceeds max_payload_bytes")

    def _validate_filters(self, event_types: Sequence[str] | None) -> tuple[str, ...]:
        if event_types is None:
            return ()
        values = tuple(dict.fromkeys(event_types))
        unknown = set(values) - EVENT_TYPES
        if unknown:
            raise ValueError(f"unknown event types: {sorted(unknown)}")
        return values

    def _last_terminal_event(self, connection: Any, process_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT * FROM events
               WHERE process_id = ? AND event_type = 'process.exited'
               ORDER BY event_id DESC LIMIT 1""",
            (process_id,),
        ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def _row_to_event(self, row: Any) -> dict[str, Any]:
        return {
            "event_id": int(row["event_id"]),
            "request_id": row["request_id"],
            "workspace_id": row["workspace_id"],
            "process_id": row["process_id"],
            "type": row["event_type"],
            "sequence": row["sequence"],
            "created_at": row["created_at"],
            "payload": json.loads(row["payload_json"]),
        }

    def _database_identity(self) -> str:
        path = getattr(self._database, "path", None)
        if path is not None:
            return str(path)
        identity = getattr(self._database, "database_identity", None)
        return str(identity()) if callable(identity) else f"database:{id(self._database)}"

    def _notify_locked(self) -> None:
        self._notifier.generation += 1
        self._notifier.condition.notify_all()


def _find_sensitive_key(value: Mapping[str, Any]) -> str | None:
    for key, nested in value.items():
        if key.casefold() in _SENSITIVE_KEYS:
            return key
        if (
            isinstance(nested, str)
            and any(token in key.casefold() for token in ("path", "directory"))
            and PurePath(nested).is_absolute()
        ):
            return key
        if isinstance(nested, Mapping):
            found = _find_sensitive_key(nested)
            if found is not None:
                return found
        elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            for item in nested:
                if isinstance(item, Mapping):
                    found = _find_sensitive_key(item)
                    if found is not None:
                        return found
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
