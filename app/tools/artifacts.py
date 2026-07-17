"""MCP tools: list_artifacts, read_artifact, view_artifact.

Provides unified access to workspace artifacts (images, reports, logs,
screenshots, etc.) so ChatGPT can discover and inspect them without
manually guessing file paths.

Tools
-----
- ``list_artifacts`` — discover registered artifacts, optionally filtered
- ``read_artifact`` — read text artifact content with offset/max_chars
- ``view_artifact`` — view an image artifact (routes to view_image)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services import artifact_registry as registry
from app.services.envelope import (
    audit_event,
    elapsed_ms,
    error_result,
    generate_request_id,
    ok_result,
)
from app.services.workspace_manager import get_workspace

log = logging.getLogger(__name__)

_MAX_READ_CHARS = 100_000


def _ensure_workspace_exists(workspace_id: str) -> dict[str, Any] | None:
    """Return the workspace record if it exists, or None (and log an error)."""
    record = get_workspace(workspace_id)
    if record is None:
        return None
    return record


def _list_artifacts(
    workspace_id: str,
    kind: str | None = None,
    path_prefix: str | None = None,
) -> dict[str, Any]:
    """List artifacts for a workspace, optionally filtered by kind or path prefix."""
    start = time.monotonic()
    request_id = generate_request_id()
    record = _ensure_workspace_exists(workspace_id)
    if record is None:
        result = error_result(
            "WORKSPACE_NOT_FOUND",
            f"workspace not found: {workspace_id}",
            workspace_id=workspace_id,
            request_id=request_id,
        )
        audit_event(
            tool_name="list_artifacts",
            request_id=request_id,
            workspace_id=workspace_id,
            input_summary=f"kind={kind!r} path_prefix={path_prefix!r}",
            success=False,
            duration_ms=elapsed_ms(start),
            error_code="WORKSPACE_NOT_FOUND",
        )
        return result

    if kind is not None and kind not in ("image", "text", "html", "json", "xml", "unknown"):
        result = error_result(
            "INVALID_INPUT",
            f"kind must be one of: image, text, html, json, xml, unknown. Got: {kind!r}",
            workspace_id=workspace_id,
            request_id=request_id,
        )
        audit_event(
            tool_name="list_artifacts",
            request_id=request_id,
            workspace_id=workspace_id,
            input_summary=f"kind={kind!r}",
            success=False,
            duration_ms=elapsed_ms(start),
            error_code="INVALID_INPUT",
        )
        return result

    try:
        registry.discover_artifacts(Path(str(record["worktree_path"])), workspace_id)
    except (OSError, ValueError) as exc:
        log.warning("artifact discovery failed for %s: %s", workspace_id, exc)
    artifacts = registry.list_artifacts(workspace_id, kind=kind, path_prefix=path_prefix)

    result = ok_result(
        {
            "workspace_id": workspace_id,
            "count": len(artifacts),
            "artifacts": artifacts,
        },
        workspace_id=workspace_id,
        request_id=request_id,
    )
    audit_event(
        tool_name="list_artifacts",
        request_id=request_id,
        workspace_id=workspace_id,
        input_summary=f"kind={kind!r} path_prefix={path_prefix!r}",
        success=True,
        duration_ms=elapsed_ms(start),
    )
    return result


def _read_artifact(
    artifact_id: str,
    offset: int = 0,
    max_chars: int = 50000,
) -> dict[str, Any]:
    """Read content from a text artifact.

    Supports pagination via offset and max_chars.  Only works for text-based
    artifact kinds (text, json, html, xml).  Image artifacts return an error
    — use ``view_artifact`` for images.
    """
    start = time.monotonic()
    request_id = generate_request_id()

    def fail(code: str, message: str, *, workspace_id: str | None = None) -> dict[str, Any]:
        result = error_result(
            code,
            message,
            workspace_id=workspace_id,
            request_id=request_id,
            extra={"artifact_id": artifact_id},
        )
        audit_event(
            tool_name="read_artifact",
            request_id=request_id,
            workspace_id=workspace_id,
            input_summary=f"artifact_id={artifact_id!r} offset={offset} max_chars={max_chars}",
            success=False,
            duration_ms=elapsed_ms(start),
            error_code=code,
        )
        return result

    record = registry.get_artifact(artifact_id)
    if record is None:
        return fail("ARTIFACT_NOT_FOUND", f"artifact not found: {artifact_id}")

    workspace_id = record.get("workspace_id")
    kind = str(record.get("kind", ""))
    if kind == "image":
        return fail(
            "ARTIFACT_IS_IMAGE",
            f"artifact {artifact_id} is an image; use view_artifact instead",
            workspace_id=workspace_id,
        )
    if kind not in {"text", "json", "html", "xml"}:
        return fail(
            "ARTIFACT_NOT_TEXT",
            f"artifact {artifact_id} has unsupported binary kind: {kind!r}",
            workspace_id=workspace_id,
        )

    file_path = registry.resolve_artifact_path(record)
    path = str(record.get("path", ""))
    if file_path is None:
        return fail(
            "PATH_DENIED",
            f"artifact path is outside its workspace or controlled store: {path}",
            workspace_id=workspace_id,
        )
    if not file_path.is_file():
        return fail(
            "ARTIFACT_STALE",
            f"artifact file no longer exists on disk: {path}",
            workspace_id=workspace_id,
        )

    import hashlib as _hashlib

    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        result = error_result(
            "INTERNAL_ERROR",
            f"failed to read artifact: {exc}",
            retryable=True,
            workspace_id=workspace_id,
            request_id=request_id,
            extra={"artifact_id": artifact_id},
        )
        audit_event(
            tool_name="read_artifact",
            request_id=request_id,
            workspace_id=workspace_id,
            input_summary=f"artifact_id={artifact_id!r}",
            success=False,
            duration_ms=elapsed_ms(start),
            error_code="INTERNAL_ERROR",
        )
        return result

    current_sha = _hashlib.sha256(raw).hexdigest()
    if record.get("sha256") and current_sha != record["sha256"]:
        return fail(
            "ARTIFACT_MODIFIED",
            f"artifact file has been modified since registration: {path}",
            workspace_id=workspace_id,
        )

    try:
        # Match Python's normal text-file semantics on every platform so
        # pagination offsets are stable for artifacts written with CRLF.
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        return fail(
            "ARTIFACT_NOT_TEXT",
            f"artifact {artifact_id} is not a UTF-8 text file: {path}",
            workspace_id=workspace_id,
        )

    if offset < 0 or max_chars < 1:
        return fail(
            "INVALID_INPUT",
            "offset must be non-negative and max_chars must be positive",
            workspace_id=workspace_id,
        )
    total = len(text)
    offset = min(offset, total)
    max_chars = min(max_chars, _MAX_READ_CHARS)
    content = text[offset : offset + max_chars]
    truncated = (offset + len(content)) < total
    result = ok_result(
        {
            "artifact_id": artifact_id,
            "path": path,
            "mime_type": record.get("mime_type"),
            "kind": kind,
            "offset": offset,
            "content": content,
            "total_chars": total,
            "truncated": truncated,
        },
        workspace_id=workspace_id,
        request_id=request_id,
        truncated=truncated,
        next_cursor=str(offset + len(content)) if truncated else None,
    )
    audit_event(
        tool_name="read_artifact",
        request_id=request_id,
        workspace_id=workspace_id,
        input_summary=f"artifact_id={artifact_id!r} offset={offset} max_chars={max_chars}",
        success=True,
        duration_ms=elapsed_ms(start),
    )
    return result


def _view_artifact(
    artifact_id: str,
    detail: str = "high",
) -> dict[str, Any] | list[Any]:
    """View an image artifact.

    For image artifacts, this routes to the ``view_image`` tool internally.
    For non-image artifacts, returns an error.

    The response includes both the image (as MCP ImageContent) and metadata.
    """
    start = time.monotonic()
    request_id = generate_request_id()

    def fail(code: str, message: str, *, workspace_id: str | None = None) -> dict[str, Any]:
        result = error_result(
            code,
            message,
            workspace_id=workspace_id,
            request_id=request_id,
            extra={"artifact_id": artifact_id},
        )
        audit_event(
            tool_name="view_artifact",
            request_id=request_id,
            workspace_id=workspace_id,
            input_summary=f"artifact_id={artifact_id!r} detail={detail!r}",
            success=False,
            duration_ms=elapsed_ms(start),
            error_code=code,
        )
        return result

    record = registry.get_artifact(artifact_id)
    if record is None:
        return fail("ARTIFACT_NOT_FOUND", f"artifact not found: {artifact_id}")

    workspace_id = record.get("workspace_id")
    kind = str(record.get("kind", ""))
    if kind != "image":
        return fail(
            "ARTIFACT_NOT_IMAGE",
            f"artifact {artifact_id} is {kind!r}, not an image. "
            "Use read_artifact for text artifacts.",
            workspace_id=workspace_id,
        )

    path = str(record.get("path", ""))
    file_path = registry.resolve_artifact_path(record)
    if file_path is None:
        return fail(
            "PATH_DENIED",
            f"artifact path is outside its workspace or controlled store: {path}",
            workspace_id=workspace_id,
        )
    if not file_path.is_file():
        return fail(
            "ARTIFACT_STALE",
            f"artifact file no longer exists on disk: {path}",
            workspace_id=workspace_id,
        )

    import hashlib as _hashlib

    try:
        current_sha = _hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError as exc:
        return fail("INTERNAL_ERROR", f"failed to read artifact: {exc}", workspace_id=workspace_id)
    if record.get("sha256") and current_sha != record["sha256"]:
        return fail(
            "ARTIFACT_MODIFIED",
            f"artifact file has been modified since registration: {path}",
            workspace_id=workspace_id,
        )

    if not workspace_id:
        return fail("INTERNAL_ERROR", "artifact has no workspace_id")
    ws_record = get_workspace(workspace_id)
    if ws_record is None:
        return fail(
            "WORKSPACE_NOT_FOUND",
            f"workspace not found: {workspace_id}",
            workspace_id=workspace_id,
        )
    worktree = Path(str(ws_record["worktree_path"])).resolve()
    try:
        rel_path = str(file_path.relative_to(worktree)).replace("\\", "/")
    except ValueError:
        return fail(
            "PATH_DENIED",
            "image artifacts in the controlled process store cannot be viewed as workspace images",
            workspace_id=workspace_id,
        )

    from app.tools.view_image import _view_image as _view_image_internal

    result = _view_image_internal(workspace_id, rel_path, detail=detail)
    audit_event(
        tool_name="view_artifact",
        request_id=request_id,
        workspace_id=workspace_id,
        input_summary=f"artifact_id={artifact_id!r} detail={detail!r}",
        success=not (isinstance(result, dict) and result.get("ok") is False),
        duration_ms=elapsed_ms(start),
        error_code=(
            str(result.get("error", {}).get("code"))
            if isinstance(result, dict) and result.get("ok") is False
            else None
        ),
    )
    return result


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_tools(mcp: FastMCP) -> None:
    """Register artifact tools on the FastMCP instance."""

    @mcp.tool(
        name="list_artifacts",
        description=(
            "List registered artifacts for a workspace.  Artifacts are files "
            "produced by processes (test screenshots, coverage reports, logs, "
            "etc.) that have been registered in the artifact registry.\n\n"
            "Optionally filter by ``kind`` (image, text, html, json, xml) or "
            "``path_prefix`` to find specific artifacts.\n\n"
            "Each artifact includes a ``status`` field:\n"
            '- ``"available"`` — file exists on disk\n'
            '- ``"stale"`` — file no longer exists\n'
            '- ``"modified"`` — file has changed since registration\n\n'
            "Use ``read_artifact`` to read text artifacts and "
            "``view_artifact`` to view image artifacts."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_artifacts(
        workspace_id: str,
        kind: str | None = None,
        path_prefix: str | None = None,
    ) -> dict[str, object]:
        """List artifacts for a workspace.

        Args:
            workspace_id: The workspace to list artifacts for.
            kind: Optional filter by artifact kind (image, text, html, json, xml).
            path_prefix: Optional filter by path prefix.
        """
        log.info(
            "list_artifacts workspace_id=%s kind=%s path_prefix=%s",
            workspace_id,
            kind,
            path_prefix,
        )
        return _list_artifacts(workspace_id, kind, path_prefix)

    @mcp.tool(
        name="read_artifact",
        description=(
            "Read the content of a text artifact (text, json, html, xml). "
            "Supports pagination via ``offset`` and ``max_chars``.\n\n"
            "Returns an error for image artifacts — use ``view_artifact`` "
            "for images.\n\n"
            "Errors:\n"
            '- ``ARTIFACT_NOT_FOUND`` — artifact_id does not exist\n'
            '- ``ARTIFACT_IS_IMAGE`` — artifact is an image, not text\n'
            '- ``ARTIFACT_STALE`` — file no longer exists on disk\n'
            '- ``ARTIFACT_MODIFIED`` — file changed since registration\n'
            '- ``ARTIFACT_NOT_TEXT`` — file is not valid UTF-8 text'
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def read_artifact(
        artifact_id: str,
        offset: int = 0,
        max_chars: int = 50000,
    ) -> dict[str, object]:
        """Read a text artifact with pagination.

        Args:
            artifact_id: The artifact ID to read.
            offset: Character offset from the start of the file.
            max_chars: Maximum characters to return (default 50000, max 100000).
        """
        log.info(
            "read_artifact artifact_id=%s offset=%d max_chars=%d",
            artifact_id,
            offset,
            max_chars,
        )
        return _read_artifact(artifact_id, offset, max_chars)

    @mcp.tool(
        name="view_artifact",
        description=(
            "View an image artifact.  The artifact's image is returned as "
            "MCP ImageContent so the model can inspect it visually.\n\n"
            "Only works for image artifacts.  For text artifacts, use "
            "``read_artifact``.\n\n"
            "Parameters:\n"
            '- ``detail="high"`` (default) — scales the image for model viewing\n'
            '- ``detail="original"`` — returns the image at original resolution\n\n'
            "Errors:\n"
            '- ``ARTIFACT_NOT_FOUND`` — artifact_id does not exist\n'
            '- ``ARTIFACT_NOT_IMAGE`` — artifact is not an image\n'
            '- ``ARTIFACT_STALE`` — file no longer exists on disk\n'
            '- ``ARTIFACT_MODIFIED`` — file changed since registration'
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def view_artifact(
        artifact_id: str,
        detail: str = "high",
    ) -> dict[str, object] | list[object]:
        """View an image artifact.

        Args:
            artifact_id: The artifact ID to view.
            detail: ``"high"`` (default) or ``"original"``.
        """
        log.info("view_artifact artifact_id=%s detail=%s", artifact_id, detail)
        return _view_artifact(artifact_id, detail)
