"""Idempotency key store for mutation tools.

Provides a simple SQLite-backed deduplication layer so that tools
such as ``apply_patch``, ``run_pwsh``, and ``create_workspace``
can safely be retried without side effects.

Usage
-----
When a mutation tool receives an ``idempotency_key``:

1. Call ``get_idempotent_result(key, tool_name, input_hash)``.
2. If a result is returned, the tool should return it immediately
   without performing the operation.
3. Otherwise execute the operation, then call
   ``store_idempotent_result(key, tool_name, input_hash, result_json)``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, cast

# Import shared connection from the database module.
from app.storage.database import _get_connection

_IDEMPOTENCY_TTL_HOURS = 24

# Thread-local reload guard.
_local = threading.local()


def _ensure_table() -> None:
    """Create the idempotency_keys table if it does not exist."""
    conn = _get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            idempotency_key TEXT PRIMARY KEY,
            tool_name       TEXT NOT NULL,
            input_hash      TEXT NOT NULL,
            result_json     TEXT NOT NULL,
            created_at      TEXT NOT NULL
        )
    """)
    conn.commit()


def _input_hash(tool_name: str, **kwargs: Any) -> str:
    """Return a deterministic SHA-256 of the tool name and its inputs."""
    raw = json.dumps({"tool": tool_name, "inputs": kwargs}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_idempotent_result(
    idempotency_key: str,
    tool_name: str,
    input_hash: str,
) -> dict[str, Any] | None:
    """Return the stored result if the key exists and input_hash matches.

    Returns ``None`` when:
    - The key does not exist.
    - The key exists but the input_hash differs (caller should check for
      ``IDEMPOTENCY_KEY_MISMATCH``).
    """
    _ensure_table()
    conn = _get_connection()
    row = conn.execute(
        "SELECT input_hash, result_json FROM idempotency_keys WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if row["input_hash"] != input_hash:
        return {"_mismatch": True, "stored_hash": row["input_hash"]}
    return cast(dict[str, Any], json.loads(row["result_json"]))


def store_idempotent_result(
    idempotency_key: str,
    tool_name: str,
    input_hash: str,
    result_json: str,
) -> None:
    """Persist the result of an idempotent operation."""
    _ensure_table()
    conn = _get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO idempotency_keys
           (idempotency_key, tool_name, input_hash, result_json, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (idempotency_key, tool_name, input_hash, result_json, now),
    )
    conn.commit()


def expire_old_keys(hours: int = _IDEMPOTENCY_TTL_HOURS) -> int:
    """Remove idempotency keys older than *hours*.

    Returns the number of deleted rows.
    """
    _ensure_table()
    conn = _get_connection()
    from datetime import timedelta

    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_iso = cutoff_dt.isoformat()
    cursor = conn.execute(
        "DELETE FROM idempotency_keys WHERE created_at < ?",
        (cutoff_iso,),
    )
    conn.commit()
    return cursor.rowcount


def with_idempotency(
    idempotency_key: str | None,
    tool_name: str,
    inputs: dict[str, Any],
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Execute *fn* with idempotency key deduplication."""
    if idempotency_key is None:
        return fn()

    input_hash = _input_hash(tool_name, **inputs)

    cached = get_idempotent_result(idempotency_key, tool_name, input_hash)
    if cached is not None:
        if isinstance(cached, dict) and cached.get("_mismatch"):
            from app.services.envelope import error_result

            return error_result(
                "IDEMPOTENCY_KEY_MISMATCH",
                f"idempotency_key {idempotency_key!r} was used with different inputs",
                extra={
                    "idempotency_key": idempotency_key,
                    "stored_hash": cached.get("stored_hash"),
                },
            )
        return cached

    result = fn()
    store_idempotent_result(
        idempotency_key,
        tool_name,
        input_hash,
        json.dumps(result, ensure_ascii=False, default=str),
    )
    return result
