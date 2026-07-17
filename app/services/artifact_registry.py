"""Artifact registry service.

Manages the lifecycle of workspace artifacts — files produced by tools,
tests, and processes that ChatGPT may need to inspect (images, reports,
logs, screenshots, etc.).

Provides:
- Registration of artifacts (manual or from process output)
- Auto-discovery of common artifact files in a workspace
- Querying artifacts by workspace, kind, or path prefix
- Staleness detection (file deleted or modified on disk)
- Cleanup on workspace discard
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.storage import database as db

log = logging.getLogger(__name__)

# Well-known file patterns for auto-discovery, grouped by kind.
# Ordered by specificity (more specific patterns first).
_AUTO_DISCOVERY_PATTERNS: list[tuple[str, str, str]] = [
    # (kind, glob_pattern, mime_type_hint)
    ("image", "**/*.png", "image/png"),
    ("image", "**/*.jpg", "image/jpeg"),
    ("image", "**/*.jpeg", "image/jpeg"),
    ("image", "**/*.webp", "image/webp"),
    ("image", "**/*.gif", "image/gif"),
    ("image", "**/*.bmp", "image/bmp"),
    ("html", "**/*.html", "text/html"),
    ("html", "**/*.htm", "text/html"),
    ("json", "**/*.json", "application/json"),
    ("xml", "**/*.xml", "application/xml"),
    ("text", "**/*.log", "text/plain"),
    ("text", "**/*.txt", "text/plain"),
    ("text", "**/*.md", "text/markdown"),
    ("text", "**/*.csv", "text/csv"),
    ("text", "**/*.svg", "image/svg+xml"),
]

# Directories always excluded from auto-discovery.
_EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".claude",
}

# Maximum number of artifacts discovered in a single scan.
_MAX_AUTO_DISCOVER = 100

# Maximum total size (bytes) for auto-discovery.
_MAX_AUTO_DISCOVER_SIZE = 100 * 1024 * 1024  # 100 MB

# Artifact ID prefix.
_ARTIFACT_ID_PREFIX = "artifact_"
_REGISTRY_LOCK = threading.RLock()


def _workspace_root(workspace_id: str) -> Path | None:
    """Return the registered workspace root for *workspace_id*."""
    record = db.get_workspace(workspace_id)
    if record is None:
        return None
    return Path(str(record["worktree_path"])).expanduser().resolve()


def _controlled_store_root() -> Path:
    """Return the only server-owned artifact store outside worktrees."""
    return (BASE_DIR / "data" / "processes").resolve()


def resolve_artifact_path(record: dict[str, Any]) -> Path | None:
    """Resolve a stored artifact path while enforcing workspace isolation."""
    workspace_id = str(record.get("workspace_id", ""))
    root = _workspace_root(workspace_id)
    if root is None:
        return None
    stored = Path(str(record.get("path", "")))
    candidate = (stored if stored.is_absolute() else root / stored).expanduser().resolve()
    for boundary in (root, _controlled_store_root()):
        try:
            candidate.relative_to(boundary)
            return candidate
        except ValueError:
            continue
    return None


def _validate_artifact_path(workspace_id: str, path: str) -> Path:
    """Validate an artifact path and return its resolved on-disk location."""
    root = _workspace_root(workspace_id)
    if root is None:
        raise ValueError(f"workspace not found: {workspace_id}")
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).expanduser().resolve()
    for boundary in (root, _controlled_store_root()):
        try:
            resolved.relative_to(boundary)
            return resolved
        except ValueError:
            continue
    raise ValueError("artifact path must be inside the workspace or controlled artifact store")


def _generate_artifact_id() -> str:
    """Return a new unique artifact identifier."""
    return _ARTIFACT_ID_PREFIX + secrets.token_hex(8)


def _detect_kind(mime_type: str | None, path_str: str) -> str:
    """Detect the artifact kind from MIME type and path."""
    if mime_type:
        if mime_type.startswith("image/"):
            return "image"
        if mime_type == "text/html":
            return "html"
        if mime_type in ("application/json",):
            return "json"
        if mime_type in ("application/xml", "text/xml"):
            return "xml"
        if mime_type.startswith("text/"):
            return "text"

    # Fall back to extension.
    ext = Path(path_str).suffix.lower()
    ext_map = {
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".webp": "image",
        ".gif": "image",
        ".bmp": "image",
        ".svg": "image",
        ".html": "html",
        ".htm": "html",
        ".json": "json",
        ".xml": "xml",
        ".log": "text",
        ".txt": "text",
        ".md": "text",
        ".csv": "text",
    }
    return ext_map.get(ext, "unknown")


def _compute_sha256(file_path: Path) -> str | None:
    """Compute SHA-256 of a file, or None on error."""
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_artifact(
    workspace_id: str,
    path: str,
    *,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    kind: str | None = None,
    source_type: str | None = None,
    source_process_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Register an artifact in the database.

    Args:
        workspace_id: The workspace this artifact belongs to.
        path: Relative path within the workspace (or absolute path in the
            artifact store).
        mime_type: Detected MIME type.
        size_bytes: File size in bytes. Auto-detected if omitted.
        sha256: File SHA-256 hash. Auto-computed if omitted.
        kind: Artifact kind (image, text, html, json, xml, unknown).
            Auto-detected if omitted.
        source_type: How the artifact was created (``"process"``, ``"scan"``).
        source_process_id: The process ID that created this artifact.
        metadata: Optional JSON-serializable metadata dict.

    Returns the artifact record dict.
    """
    started = time.monotonic()
    request_id = request_id or "req_" + secrets.token_hex(8)
    # Validate the workspace and path before touching persistent state.  The
    # original spelling is retained in the record for backwards compatibility
    # (discovery uses workspace-relative paths), while all reads resolve it
    # through ``resolve_artifact_path``.
    resolved_path = _validate_artifact_path(workspace_id, path)

    # Auto-detect size if not provided.
    if size_bytes is None:
        try:
            size_bytes = resolved_path.stat().st_size
        except OSError:
            size_bytes = 0

    # Auto-compute SHA-256 if not provided.
    if sha256 is None:
        sha256 = _compute_sha256(resolved_path)

    # Auto-detect kind if not provided.
    if kind is None:
        kind = _detect_kind(mime_type, path)

    # Registration is idempotent for the same workspace/path/content tuple.
    # The lock also prevents duplicate rows when two process-finalization
    # threads discover the same screenshot concurrently.
    with _REGISTRY_LOCK:
        existing = db.find_artifact(workspace_id, path, sha256)
        if existing is not None:
            db.log_operation(
                operation_id="op_" + secrets.token_hex(8),
                tool_name="register_artifact",
                summary=f"artifact already registered: {existing['artifact_id']}",
                workspace_id=workspace_id,
                success=True,
                request_id=request_id,
                actor="mcp",
                input_summary=f"kind={kind!r} path={path!r}",
                result_status="idempotent_replay",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return existing

        artifact_id = _generate_artifact_id()

        metadata_str = None
        if metadata:
            import json as _json

            try:
                metadata_str = _json.dumps(metadata, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("metadata must be JSON serializable") from exc

        record = db.insert_artifact(
            artifact_id=artifact_id,
            workspace_id=workspace_id,
            kind=kind,
            path=path,
            mime_type=mime_type,
            size_bytes=size_bytes or 0,
            sha256=sha256,
            source_type=source_type,
            source_process_id=source_process_id,
            metadata=metadata_str,
        )

    log.info(
        "register_artifact id=%s workspace=%s kind=%s path=%s",
        artifact_id,
        workspace_id,
        kind,
        path,
    )
    db.log_operation(
        operation_id="op_" + secrets.token_hex(8),
        tool_name="register_artifact",
        summary=f"registered artifact {artifact_id}",
        workspace_id=workspace_id,
        success=True,
        request_id=request_id,
        actor="mcp",
        input_summary=f"kind={kind!r} path={path!r}",
        result_status="success",
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return record


def get_artifact(artifact_id: str) -> dict[str, Any] | None:
    """Return an artifact record, or None if not found."""
    return db.get_artifact(artifact_id)


def list_artifacts(
    workspace_id: str,
    *,
    kind: str | None = None,
    path_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """List artifacts for a workspace, optionally filtered.

    Each record includes a ``status`` field indicating whether the
    underlying file still exists and has the same SHA-256.
    """
    records = db.list_artifacts(workspace_id, kind=kind, path_prefix=path_prefix)
    return [_enrich_artifact(r) for r in records]


def _enrich_artifact(record: dict[str, Any]) -> dict[str, Any]:
    """Add status information to an artifact record.

    Checks if the underlying file still exists and has the same SHA-256.
    """
    sha256 = record.get("sha256")

    status = "available"
    file_path = resolve_artifact_path(record)
    if file_path is None:
        status = "invalid"
    elif not file_path.is_file():
        status = "stale"
    elif sha256:
        current_sha = _compute_sha256(file_path)
        if current_sha != sha256:
            status = "modified"

    result = dict(record)
    result["status"] = status
    if file_path is not None:
        result["resolved_path"] = str(file_path)
    return result


def delete_artifact(artifact_id: str) -> bool:
    """Delete an artifact record. Returns True if deleted."""
    record = db.get_artifact(artifact_id)
    deleted = db.delete_artifact(artifact_id)
    if deleted:
        db.log_operation(
            operation_id="op_" + secrets.token_hex(8),
            tool_name="delete_artifact",
            summary=f"artifact_id={artifact_id!r}",
            workspace_id=record.get("workspace_id") if record else None,
            success=True,
            actor="mcp",
            input_summary=f"artifact_id={artifact_id!r}",
            result_status="success",
        )
    return deleted


def delete_artifacts_for_workspace(workspace_id: str) -> int:
    """Delete all artifacts for a workspace."""
    deleted = db.delete_artifacts_for_workspace(workspace_id)
    if deleted:
        db.log_operation(
            operation_id="op_" + secrets.token_hex(8),
            tool_name="delete_artifact",
            summary=f"workspace cleanup: artifacts={deleted}",
            workspace_id=workspace_id,
            success=True,
            actor="mcp",
            input_summary=f"workspace_id={workspace_id!r} count={deleted}",
            result_status="success",
        )
    return deleted


def count_artifacts(workspace_id: str) -> dict[str, Any]:
    """Return artifact count summary for a workspace."""
    return db.count_artifacts(workspace_id)


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


def discover_artifacts(
    workspace_path: Path,
    workspace_id: str,
    *,
    process_id: str | None = None,
) -> list[dict[str, Any]]:
    """Scan a workspace directory for new artifacts and register them.

    Only scans files that match well-known patterns.  Skips excluded
    directories.  Respects the maximum discovery limits.

    Returns a list of newly registered artifact records.
    """
    if not workspace_path.is_dir():
        log.warning("discover_artifacts: path not a directory: %s", workspace_path)
        return []

    discovered: list[dict[str, Any]] = []
    total_size = 0

    for kind, glob_pattern, mime_hint in _AUTO_DISCOVERY_PATTERNS:
        if len(discovered) >= _MAX_AUTO_DISCOVER:
            break

        for file_path in sorted(workspace_path.glob(glob_pattern)):
            if len(discovered) >= _MAX_AUTO_DISCOVER:
                break

            # Skip excluded directories.
            rel = file_path.relative_to(workspace_path)
            if any(part in _EXCLUDED_DIRS for part in rel.parts):
                continue

            # Skip if already registered.
            existing = db.list_artifacts(
                workspace_id,
                path_prefix=str(rel).replace("\\", "/"),
            )
            if existing:
                continue

            # Check file size.
            try:
                if not file_path.is_file():
                    continue
                fsize = file_path.stat().st_size
            except OSError:
                continue
            if fsize <= 0:
                continue

            total_size += fsize
            if total_size > _MAX_AUTO_DISCOVER_SIZE:
                log.warning("discover_artifacts: total size limit reached")
                break

            # Compute SHA-256.
            sha256 = _compute_sha256(file_path)
            if sha256 is None:
                continue

            # Use POSIX-style relative path.
            rel_path = str(rel).replace("\\", "/")

            # Detect MIME type more precisely.
            mime_type = mime_hint
            if kind == "image":
                try:
                    import mimetypes

                    guessed = mimetypes.guess_type(str(file_path))[0]
                    if guessed:
                        mime_type = guessed
                except Exception:
                    pass

            record = register_artifact(
                workspace_id=workspace_id,
                path=rel_path,
                mime_type=mime_type,
                size_bytes=fsize,
                sha256=sha256,
                kind=kind,
                source_type="scan",
                source_process_id=process_id,
            )
            discovered.append(record)

    if discovered:
        log.info(
            "discover_artifacts workspace=%s discovered=%d",
            workspace_id,
            len(discovered),
        )

    return discovered


def register_process_artifact(
    workspace_id: str,
    worktree_path: Path,
    process_id: str,
    artifact_path: str,
) -> dict[str, Any] | None:
    """Register a single artifact produced by a process.

    This is called after a process finishes to register any output files
    that the process may have created.

    Returns the artifact record, or None if the path is not a recognised
    artifact type.
    """
    candidate = Path(artifact_path)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        log.warning("register_process_artifact: path escapes worktree: %s", artifact_path)
        return None
    abs_path = (worktree_path / candidate).resolve()
    try:
        abs_path.relative_to(worktree_path.resolve())
    except ValueError:
        log.warning("register_process_artifact: path escapes worktree: %s", artifact_path)
        return None
    if not abs_path.is_file():
        log.warning("register_process_artifact: not a file: %s", abs_path)
        return None

    try:
        fsize = abs_path.stat().st_size
    except OSError:
        return None
    if fsize <= 0:
        return None

    sha256 = _compute_sha256(abs_path)
    if sha256 is None:
        return None

    # Detect kind from extension.
    kind = _detect_kind(None, artifact_path)
    if kind == "unknown":
        return None

    import mimetypes

    mime_type = mimetypes.guess_type(artifact_path)[0]

    return register_artifact(
        workspace_id=workspace_id,
        path=artifact_path,
        mime_type=mime_type,
        size_bytes=fsize,
        sha256=sha256,
        kind=kind,
        source_type="process",
        source_process_id=process_id,
    )


def register_process_output_artifacts(
    workspace_id: str,
    process_id: str,
    stdout_path: Path,
    stderr_path: Path,
) -> list[dict[str, Any]]:
    """Register complete stdout/stderr captures in the controlled artifact store."""
    records: list[dict[str, Any]] = []
    for stream, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        records.append(
            register_artifact(
                workspace_id,
                str(path),
                mime_type="text/plain",
                kind="text",
                source_type="process_output",
                source_process_id=process_id,
                metadata={"stream": stream, "complete": True},
            )
        )
    return records
