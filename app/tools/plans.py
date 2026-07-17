"""MCP tools: update_workspace_plan, get_workspace_plan.

Provides structured plan management for workspace tasks so that ChatGPT
can track progress, attach evidence, and detect revision conflicts.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services import workspace_plan as plan_service
from app.services.envelope import (
    audit_event,
    elapsed_ms,
    error_result,
    generate_request_id,
    ok_result,
)
from app.services.workspace_manager import get_workspace

log = logging.getLogger(__name__)


def _get_workspace_plan(workspace_id: str) -> dict[str, Any]:
    """Return the plan for a workspace (or None if no plan exists)."""
    record = get_workspace(workspace_id)
    if record is None:
        return error_result(
            "WORKSPACE_NOT_FOUND",
            f"workspace not found: {workspace_id}",
            workspace_id=workspace_id,
        )

    result = plan_service.get_plan(workspace_id)
    return ok_result(result, workspace_id=workspace_id)


def _update_workspace_plan(
    workspace_id: str,
    explanation: str,
    steps: list[dict[str, Any]],
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Create or update the plan for a workspace.

    See the ``update_workspace_plan`` tool docstring for parameter details.
    """
    started = time.monotonic()
    request_id = generate_request_id()
    record = get_workspace(workspace_id)
    if record is None:
        result = error_result(
            "WORKSPACE_NOT_FOUND",
            f"workspace not found: {workspace_id}",
            workspace_id=workspace_id,
            request_id=request_id,
        )
        audit_event(
            tool_name="update_workspace_plan", request_id=request_id,
            workspace_id=workspace_id, input_summary=f"steps={len(steps)}",
            success=False, duration_ms=elapsed_ms(started), error_code="WORKSPACE_NOT_FOUND",
        )
        return result

    try:
        result = plan_service.update_plan(
            workspace_id,
            explanation,
            steps,
            expected_revision=expected_revision,
            request_id=request_id,
        )
        return ok_result(result, workspace_id=workspace_id, request_id=request_id)
    except ValueError as exc:
        msg = str(exc)
        if "revision conflict" in msg:
            result = error_result(
                "PLAN_REVISION_CONFLICT",
                msg,
                workspace_id=workspace_id,
                request_id=request_id,
                extra={"expected_revision": expected_revision},
            )
            code = "PLAN_REVISION_CONFLICT"
        else:
            result = error_result(
                "INVALID_INPUT", msg, workspace_id=workspace_id, request_id=request_id
            )
            code = "INVALID_INPUT"
        audit_event(
            tool_name="update_workspace_plan", request_id=request_id,
            workspace_id=workspace_id, input_summary=f"steps={len(steps)}",
            success=False, duration_ms=elapsed_ms(started), error_code=code,
        )
        return result


def _update_workspace_plan_step(
    workspace_id: str,
    step_id: str,
    status: str,
    evidence: list[dict[str, Any]] | None = None,
    blocked_reason: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Update one plan step with revision and evidence validation."""
    request_id = generate_request_id()
    started = time.monotonic()
    if get_workspace(workspace_id) is None:
        code = "WORKSPACE_NOT_FOUND"
        result = error_result(
            code, f"workspace not found: {workspace_id}",
            workspace_id=workspace_id, request_id=request_id,
        )
    else:
        try:
            updated = plan_service.update_step_status(
                workspace_id, step_id, status, evidence=evidence,
                blocked_reason=blocked_reason, expected_revision=expected_revision,
                request_id=request_id,
            )
            return ok_result(updated, workspace_id=workspace_id, request_id=request_id)
        except ValueError as exc:
            message = str(exc)
            code = "PLAN_REVISION_CONFLICT" if "revision conflict" in message else "INVALID_INPUT"
            result = error_result(
                code, message, workspace_id=workspace_id, request_id=request_id,
                extra=(
                    {"expected_revision": expected_revision}
                    if code == "PLAN_REVISION_CONFLICT"
                    else None
                ),
            )
    audit_event(
        tool_name="update_workspace_plan_step", request_id=request_id,
        workspace_id=workspace_id,
        input_summary=f"step_id={step_id!r} status={status!r} evidence_count={len(evidence or [])}",
        success=False, duration_ms=elapsed_ms(started), error_code=code,
    )
    return result


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_tools(mcp: FastMCP) -> None:
    """Register plan-related tools on the FastMCP instance."""

    @mcp.tool(
        name="get_workspace_plan",
        description=(
            "Return the current plan for a workspace, including all steps "
            "and their statuses.  Returns ``null`` for ``plan`` if no plan "
            "has been created yet.\n\n"
            "Each step includes:\n"
            "- ``id``: unique step identifier\n"
            "- ``text``: human-readable description\n"
            "- ``status``: one of: pending, in_progress, completed, blocked, cancelled\n"
            "- ``evidence``: optional list of evidence objects\n"
            "- ``created_at`` / ``updated_at``: ISO-8601 timestamps"
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_workspace_plan(workspace_id: str) -> dict[str, object]:
        """Get the current plan for a workspace.

        Args:
            workspace_id: The workspace to get the plan for.
        """
        log.info("get_workspace_plan workspace_id=%s", workspace_id)
        return _get_workspace_plan(workspace_id)

    @mcp.tool(
        name="update_workspace_plan",
        description=(
            "Create or update the step-by-step plan for a workspace. "
            "Each step has an ``id``, ``text``, and optional ``status``.\n\n"
            "Status rules:\n"
            "- At most one step can be ``in_progress`` at a time.\n"
            "- Step IDs must be unique within the plan.\n"
            "- Empty step IDs or text are rejected.\n"
            "- Status must be one of: pending, in_progress, completed, blocked, cancelled.\n\n"
            "Revision control:\n"
            "- When creating a new plan, omit ``expected_revision``.\n"
            "- When updating an existing plan, pass the current revision.\n"
            "- If the revision has changed since you last read it, "
            "the call returns ``PLAN_REVISION_CONFLICT``.\n\n"
            "Evidence:\n"
            "Use ``update_step_status`` to attach evidence to a step. "
            "Supported evidence types: process_id, check_id, artifact_id, "
            "git_commit, git_diff, file_path."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def update_workspace_plan(
        workspace_id: str,
        explanation: str,
        steps: list[dict[str, object]],
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        """Create or update a workspace plan.

        Args:
            workspace_id: The workspace to update the plan for.
            explanation: Human-readable task description.
            steps: Ordered list of steps, each with ``id``, ``text``,
                and optional ``status``.
            expected_revision: Required when updating an existing plan.
                Must match the current plan revision.
        """
        log.info(
            "update_workspace_plan workspace_id=%s steps=%d expected_revision=%s",
            workspace_id,
            len(steps),
            expected_revision,
        )
        return _update_workspace_plan(
            workspace_id,
            explanation,
            [dict(s) for s in steps],
            expected_revision=expected_revision,
        )

    @mcp.tool(
        name="update_workspace_plan_step",
        description=(
            "Update one workspace-plan step status and optionally attach evidence. "
            "Pass expected_revision to detect concurrent updates. At most one step "
            "may be in_progress. Test steps should cite a passed process_id or check_id."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def update_workspace_plan_step(
        workspace_id: str,
        step_id: str,
        status: str,
        evidence: list[dict[str, object]] | None = None,
        blocked_reason: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        return _update_workspace_plan_step(
            workspace_id, step_id, status,
            [dict(item) for item in evidence] if evidence is not None else None,
            blocked_reason, expected_revision,
        )
