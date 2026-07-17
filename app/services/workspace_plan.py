"""Workspace Plan service.

Manages the step-by-step plan for a workspace task, including plan
creation, step updates, revision tracking, and evidence attachment.

Each workspace has at most one plan.  A plan consists of a human-readable
``explanation`` and an ordered list of ``steps``.

Allowed step statuses:
- ``pending`` — not yet started
- ``in_progress`` — currently being worked on (at most one step at a time)
- ``completed`` — finished successfully
- ``blocked`` — waiting on something
- ``cancelled`` — abandoned

Evidence types
--------------
Each step can carry evidence in JSON format:
- ``{"type": "process_id", "id": "pr_xxx"}``
- ``{"type": "check_id", "id": "unit_tests"}``
- ``{"type": "artifact_id", "id": "artifact_xxx"}``
- ``{"type": "git_commit", "id": "abc123"}``
- ``{"type": "git_diff", "id": "...", "summary": "..."}``
- ``{"type": "file_path", "id": "src/main.py"}``
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from app.storage import database as db

log = logging.getLogger(__name__)

_VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "blocked", "cancelled"})
_VALID_EVIDENCE_TYPES = frozenset(
    {"process_id", "check_id", "artifact_id", "git_commit", "git_diff", "file_path"}
)
_CANONICAL_PROCESS_ID = re.compile(r"^pr-[0-9a-f]{8}$")
_CANONICAL_ARTIFACT_ID = re.compile(r"^artifact-[0-9a-f]{16}$|^artifact_[0-9a-f]{16}$")
_PLAN_LOCK = threading.RLock()


def _validate_step(step: dict[str, Any]) -> str | None:
    """Validate a step dict. Returns an error message or None."""
    if not isinstance(step, dict):
        return "each step must be a dict"
    step_id = step.get("id", "")
    if not step_id or not isinstance(step_id, str):
        return "step.id is required and must be a non-empty string"
    text = step.get("text", "")
    if not text or not isinstance(text, str):
        return "step.text is required and must be a non-empty string"
    status = step.get("status", "pending")
    if status not in _VALID_STATUSES:
        return f"step.status must be one of {sorted(_VALID_STATUSES)}, got: {status!r}"
    return None


def _build_plan_result(workspace_id: str) -> dict[str, Any]:
    """Build the full plan result from the database."""
    plan = db.get_plan(workspace_id)
    if plan is None:
        return {"workspace_id": workspace_id, "plan": None}

    steps = db.get_plan_steps(workspace_id)
    parsed_steps = []
    for s in steps:
        step = {
            "id": s["step_id"],
            "text": s["text"],
            "status": s["status"],
            "evidence": [],
            "created_at": s["created_at"],
            "updated_at": s["updated_at"],
        }
        if s.get("evidence"):
            try:
                step["evidence"] = json.loads(s["evidence"])
            except (json.JSONDecodeError, TypeError):
                step["evidence"] = s["evidence"]
            step["evidence_status"] = _evidence_statuses(workspace_id, step["evidence"])
        if s.get("blocked_reason"):
            step["blocked_reason"] = s["blocked_reason"]
        parsed_steps.append(step)

    return {
        "workspace_id": workspace_id,
        "plan": {
            "plan_id": plan["plan_id"],
            "explanation": plan["explanation"],
            "revision": plan["revision"],
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
            "steps": parsed_steps,
        },
    }


def _evidence_statuses(workspace_id: str, evidence: Any) -> list[dict[str, Any]]:
    """Return non-destructive freshness/status information for evidence."""
    if not isinstance(evidence, list):
        return [{"status": "invalid", "reason": "evidence must be a list"}]
    statuses: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            statuses.append({"status": "invalid", "reason": "evidence must be an object"})
            continue
        evidence_type = item.get("type")
        evidence_id = item.get("id")
        status: dict[str, Any] = {"type": evidence_type, "id": evidence_id}
        if evidence_type not in _VALID_EVIDENCE_TYPES or not isinstance(evidence_id, str):
            status["status"] = "invalid"
            statuses.append(status)
            continue

        if evidence_type == "process_id":
            process = db.get_process(evidence_id)
            if process is None:
                status["status"] = (
                    "unresolved" if not _CANONICAL_PROCESS_ID.match(evidence_id) else "stale"
                )
            elif process.get("workspace_id") != workspace_id:
                status["status"] = "cross_workspace"
            else:
                status["status"] = "available"
        elif evidence_type == "artifact_id":
            artifact = db.get_artifact(evidence_id)
            if artifact is None:
                status["status"] = (
                    "unresolved" if not _CANONICAL_ARTIFACT_ID.match(evidence_id) else "stale"
                )
            elif artifact.get("workspace_id") != workspace_id:
                status["status"] = "cross_workspace"
            else:
                from app.services import artifact_registry

                status["status"] = artifact_registry._enrich_artifact(artifact).get("status")
        elif evidence_type == "check_id":
            check_processes = [
                process
                for process in db.list_processes(workspace_id)
                if process.get("tool_name") == f"run_check:{evidence_id}"
            ]
            if not check_processes:
                status["status"] = "unresolved"
            elif any(process.get("status") == "passed" for process in check_processes):
                status["status"] = "available"
            else:
                status["status"] = "stale"
        elif evidence_type == "file_path":
            workspace = db.get_workspace(workspace_id)
            if workspace is None:
                status["status"] = "stale"
            else:
                from app.services.path_guard import resolve_within

                try:
                    status["status"] = (
                        "available"
                        if resolve_within(
                            Path(str(workspace["worktree_path"])), evidence_id
                        ).is_file()
                        else "stale"
                    )
                except ValueError:
                    status["status"] = "invalid"
        else:
            # Git commit/diff evidence is immutable caller-provided evidence;
            # presence of a non-empty id is enough for freshness reporting.
            status["status"] = "available" if evidence_id.strip() else "invalid"
        statuses.append(status)
    return statuses


def _validate_evidence(workspace_id: str, evidence: Any) -> None:
    """Validate evidence shape and reject known cross-workspace references.

    Older clients used opaque evidence IDs in unit-level plans.  Those IDs are
    retained as ``unresolved`` evidence for compatibility, while canonical
    server-generated IDs are checked strictly and can never cross a workspace.
    """
    if evidence is None:
        return
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("each evidence item must be an object")
        evidence_type = item.get("type")
        evidence_id = item.get("id")
        if evidence_type not in _VALID_EVIDENCE_TYPES or not isinstance(evidence_id, str):
            raise ValueError(
                "evidence items require a supported type and non-empty string id"
            )
        if evidence_type == "process_id" and _CANONICAL_PROCESS_ID.match(evidence_id):
            process = db.get_process(evidence_id)
            if process is None:
                raise ValueError(f"process evidence not found: {evidence_id}")
            if process.get("workspace_id") != workspace_id:
                raise ValueError(f"process evidence belongs to another workspace: {evidence_id}")
        if evidence_type == "artifact_id" and _CANONICAL_ARTIFACT_ID.match(evidence_id):
            artifact = db.get_artifact(evidence_id)
            if artifact is None:
                raise ValueError(f"artifact evidence not found: {evidence_id}")
            if artifact.get("workspace_id") != workspace_id:
                raise ValueError(
                    f"artifact evidence belongs to another workspace: {evidence_id}"
                )
        if evidence_type == "file_path":
            workspace = db.get_workspace(workspace_id)
            if workspace is None:
                raise ValueError(f"workspace not found: {workspace_id}")
            from app.services.path_guard import resolve_within

            resolve_within(Path(str(workspace["worktree_path"])), evidence_id, must_exist=False)


def get_plan(workspace_id: str) -> dict[str, Any]:
    """Return the full plan for a workspace.

    Returns a dict with ``workspace_id`` and ``plan`` (or None if no plan exists).
    """
    return _build_plan_result(workspace_id)


def update_plan(
    workspace_id: str,
    explanation: str,
    steps: list[dict[str, Any]],
    *,
    expected_revision: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Create or update the plan for a workspace.

    Args:
        workspace_id: The workspace to update the plan for.
        explanation: Human-readable task explanation.
        steps: Ordered list of step dicts, each with ``id``, ``text``,
            and optional ``status``.
        expected_revision: Required for updates.  If the plan's current
            revision does not match, a ``PLAN_REVISION_CONFLICT`` error
            is returned.

    Returns the updated plan result.

    Raises:
        ValueError: on validation errors or revision conflicts.
    """
    started = time.monotonic()
    # ---- Validate inputs ----
    if not explanation or not explanation.strip():
        raise ValueError("explanation must be a non-empty string")

    if not steps:
        raise ValueError("steps must be a non-empty list")

    if len(steps) > 50:
        raise ValueError("steps must have at most 50 entries")

    # Validate each step.
    step_ids = set()
    for i, step in enumerate(steps):
        err = _validate_step(step)
        if err:
            raise ValueError(f"step {i}: {err}")
        _validate_evidence(workspace_id, step.get("evidence"))
        sid = step["id"]
        if sid in step_ids:
            raise ValueError(f"duplicate step id: {sid!r}")
        step_ids.add(sid)

    # Check at most one step is in_progress.
    in_progress_count = sum(1 for s in steps if s.get("status", "pending") == "in_progress")
    if in_progress_count > 1:
        raise ValueError(
            f"at most one step can be 'in_progress', found {in_progress_count}"
        )

    # ---- Create or update plan atomically ----
    with _PLAN_LOCK:
        existing = db.get_plan(workspace_id)
        new_revision = db.replace_plan(
            workspace_id,
            explanation,
            steps,
            expected_revision=expected_revision,
        )
        log.info(
            "%s_plan workspace=%s revision=%d steps=%d",
            "update" if existing is not None else "create",
            workspace_id,
            new_revision,
            len(steps),
        )

    # ---- Log audit operation ----
    summary = (
        f"plan {existing and 'updated' or 'created'}: {len(steps)} steps"
    )
    db.log_operation(
        operation_id=__import__("secrets").token_hex(6),
        tool_name="update_workspace_plan",
        summary=summary,
        workspace_id=workspace_id,
        success=True,
        request_id=request_id,
        input_summary=f"steps={len(steps)}",
        result_status="success",
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    return _build_plan_result(workspace_id)


def update_step_status(
    workspace_id: str,
    step_id: str,
    status: str,
    *,
    evidence: list[dict[str, Any]] | None = None,
    blocked_reason: str | None = None,
    expected_revision: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Update a single step's status without replacing the entire plan.

    This is a convenience for small status transitions.  For bulk updates,
    use ``update_plan``.

    Raises ValueError on validation errors.
    """
    started = time.monotonic()
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_STATUSES)}, got: {status!r}"
        )

    _validate_evidence(workspace_id, evidence)

    with _PLAN_LOCK:
        plan = db.get_plan(workspace_id)
        if plan is None:
            raise ValueError("plan not found")
        steps = db.get_plan_steps(workspace_id)
        step_found = next((s for s in steps if s["step_id"] == step_id), None)
        if step_found is None:
            raise ValueError(f"step not found: {step_id!r}")
        evidence_str = (
            json.dumps(evidence, ensure_ascii=False)
            if evidence is not None
            else step_found.get("evidence")
        )
        reason = blocked_reason if blocked_reason is not None else step_found.get("blocked_reason")
        new_revision = db.update_plan_step_with_revision(
            workspace_id,
            step_id,
            status=status,
            evidence=evidence_str,
            blocked_reason=reason,
            expected_revision=expected_revision,
        )

    log.info(
        "update_step_status workspace=%s step=%s status=%s",
        workspace_id,
        step_id,
        status,
    )
    db.log_operation(
        operation_id=__import__("secrets").token_hex(6),
        tool_name="update_workspace_plan_step",
        summary=f"plan step {step_id} changed to {status}",
        workspace_id=workspace_id,
        success=True,
        request_id=request_id,
        input_summary=f"step_id={step_id!r} status={status!r} evidence_count={len(evidence or [])}",
        result_status="success",
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    result = _build_plan_result(workspace_id)
    result["revision"] = new_revision
    return result
