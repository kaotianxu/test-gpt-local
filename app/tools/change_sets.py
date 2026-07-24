"""MCP tools for atomic, isolated workspace change sets."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services import change_set_store as store
from app.services.envelope import error_result, ok_result
from app.services.event_store import EventStore


def _emit(event_type: str, result: dict[str, Any], **extra: Any) -> None:
    """Append a redacted lifecycle event; event delivery never breaks the edit."""
    if result.get("idempotent_replay"):
        return
    try:
        EventStore().append(
            event_type,
            workspace_id=str(result["workspace_id"]),
            payload={
                "change_set_id": str(result["change_set_id"]),
                "revision": int(result.get("revision", 0)),
                **extra,
            },
        )
    except Exception:
        pass


def _invoke(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        result = action()
        return ok_result(result, workspace_id=str(result.get("workspace_id", "")) or None)
    except store.ChangeSetError as exc:
        workspace_id = exc.details.pop("workspace_id", None)
        return error_result(
            exc.code,
            str(exc),
            workspace_id=str(workspace_id) if workspace_id else None,
            extra=exc.details or None,
        )


async def begin_change_set(
    workspace_id: str,
    explanation: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Capture the workspace into an isolated staging tree."""
    response = _invoke(lambda: store.begin(workspace_id, explanation))
    if response["ok"]:
        _emit("change_set.begun", response["result"])
    return response


async def stage_patch(
    change_set_id: str,
    patch: str,
    explanation: str,
    expected_sha256: dict[str, str] | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Apply one unified diff to the isolated staging tree."""
    response = _invoke(
        lambda: store.stage_patch(
            change_set_id,
            patch,
            explanation,
            expected_sha256,
            operation_id,
        )
    )
    if response["ok"]:
        result = response["result"]
        _emit(
            "change_set.staged",
            result,
            operation_type="patch",
            file_count=len(result["changed_files"]),
            staged_digest=result["staged_digest"],
        )
    return response


async def stage_replace(
    change_set_id: str,
    path: str,
    old_text: str,
    new_text: str,
    explanation: str,
    expected_sha256: str | None = None,
    replace_all: bool = False,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Perform an exact UTF-8 replacement in the isolated staging tree."""
    response = _invoke(
        lambda: store.stage_replace(
            change_set_id,
            path,
            old_text,
            new_text,
            explanation,
            expected_sha256,
            replace_all,
            operation_id,
        )
    )
    if response["ok"]:
        result = response["result"]
        _emit(
            "change_set.staged",
            result,
            operation_type="replace",
            file_count=len(result["changed_files"]),
            staged_digest=result["staged_digest"],
        )
    return response


async def validate_change_set(
    change_set_id: str,
    expected_revision: int,
    validation_profile: str = "default",
) -> dict[str, Any]:
    """Validate structure and the final whole-tree patch."""
    response = _invoke(
        lambda: store.validate(change_set_id, expected_revision, validation_profile)
    )
    if response["ok"]:
        result = response["result"]
        _emit(
            "change_set.validated",
            result,
            file_count=len(result["changed_files"]),
            validated_digest=result["validated_digest"],
        )
    else:
        workspace_id = store.workspace_id_for(change_set_id)
        if workspace_id:
            _emit(
                "change_set.validation_failed",
                {
                    "workspace_id": workspace_id,
                    "change_set_id": change_set_id,
                    "revision": expected_revision,
                },
                error_code=response["error"]["code"],
            )
    return response


async def commit_change_set(
    change_set_id: str,
    validated_digest: str,
    expected_workspace_tree: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Atomically apply one validated final patch to the real worktree."""
    response = _invoke(
        lambda: store.commit(change_set_id, validated_digest, expected_workspace_tree)
    )
    if response["ok"]:
        result = response["result"]
        _emit(
            "change_set.committed",
            result,
            file_count=len(result["changed_files"]),
            after_tree_hash=result["after_tree_hash"],
        )
    elif response["error"]["code"] == "CHANGE_SET_RECOVERY_REQUIRED":
        workspace_id = store.workspace_id_for(change_set_id)
        if workspace_id:
            _emit(
                "change_set.recovery_required",
                {
                    "workspace_id": workspace_id,
                    "change_set_id": change_set_id,
                    "revision": 0,
                },
                error_code="CHANGE_SET_RECOVERY_REQUIRED",
            )
    return response


async def rollback_change_set(
    change_set_id: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Discard an uncommitted staging tree; never reverse a committed edit."""
    response = _invoke(lambda: store.rollback(change_set_id))
    if response["ok"]:
        _emit("change_set.rolled_back", response["result"])
    return response


async def get_change_set(change_set_id: str) -> dict[str, Any]:
    """Return lifecycle state, hashes, operation summaries, manifest, and preview."""
    return _invoke(lambda: store.get_summary(change_set_id))


def register_tools(mcp: FastMCP) -> None:
    """Register all change-set tools with their public signatures intact."""
    write_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
    read_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(
        name="begin_change_set",
        description="Begin an isolated, atomic workspace change set.",
        annotations=write_annotations,
    )
    async def _begin_change_set(
        workspace_id: str,
        explanation: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await begin_change_set(workspace_id, explanation, idempotency_key)

    @mcp.tool(
        name="stage_patch",
        description="Stage a unified diff without modifying the real workspace.",
        annotations=write_annotations,
    )
    async def _stage_patch(
        change_set_id: str,
        patch: str,
        explanation: str,
        expected_sha256: dict[str, str] | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await stage_patch(
            change_set_id,
            patch,
            explanation,
            expected_sha256,
            operation_id,
            idempotency_key,
        )

    @mcp.tool(
        name="stage_replace",
        description="Stage an exact text replacement without modifying the workspace.",
        annotations=write_annotations,
    )
    async def _stage_replace(
        change_set_id: str,
        path: str,
        old_text: str,
        new_text: str,
        explanation: str,
        expected_sha256: str | None = None,
        replace_all: bool = False,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await stage_replace(
            change_set_id,
            path,
            old_text,
            new_text,
            explanation,
            expected_sha256,
            replace_all,
            operation_id,
            idempotency_key,
        )

    @mcp.tool(
        name="validate_change_set",
        description="Validate the staged final patch and produce a digest.",
        annotations=write_annotations,
    )
    async def _validate_change_set(
        change_set_id: str,
        expected_revision: int,
        validation_profile: str = "default",
    ) -> dict[str, Any]:
        return await validate_change_set(
            change_set_id,
            expected_revision,
            validation_profile,
        )

    @mcp.tool(
        name="commit_change_set",
        description="Commit a validated change set with conflict detection and rollback.",
        annotations=write_annotations,
    )
    async def _commit_change_set(
        change_set_id: str,
        validated_digest: str,
        expected_workspace_tree: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await commit_change_set(
            change_set_id,
            validated_digest,
            expected_workspace_tree,
            idempotency_key,
        )

    @mcp.tool(
        name="rollback_change_set",
        description="Discard an uncommitted change set safely and idempotently.",
        annotations=write_annotations,
    )
    async def _rollback_change_set(
        change_set_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await rollback_change_set(change_set_id, idempotency_key)

    @mcp.tool(
        name="get_change_set",
        description="Inspect a change set without returning source operation payloads.",
        annotations=read_annotations,
    )
    async def _get_change_set(change_set_id: str) -> dict[str, Any]:
        return await get_change_set(change_set_id)
