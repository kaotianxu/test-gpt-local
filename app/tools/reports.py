"""MCP tools for project isolation status and workspace acceptance reports."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import get_project
from app.services import artifact_registry, workspace_plan
from app.services.envelope import error_result, ok_result
from app.services.process_manager import ProcessManager
from app.services.workspace_manager import get_workspace, list_workspaces
from app.storage import database as db

_MAX_OUTPUT = 200_000
_TEST_STEP_RE = re.compile(
    r"\b(test|tests|testing|check|checks|pytest|playwright)\b|测试|验收", re.I
)


def _plan_summary(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return report-friendly counts while retaining full step evidence."""
    if plan is None:
        return None
    steps = plan.get("steps", [])
    counts = {
        status: sum(1 for step in steps if step.get("status") == status)
        for status in ("completed", "in_progress", "pending", "blocked", "cancelled")
    }
    evidence_statuses = [
        status
        for step in steps
        for status in step.get("evidence_status", [])
    ]
    return {
        **plan,
        **counts,
        "evidence": {
            "count": sum(len(step.get("evidence", [])) for step in steps),
            "available": sum(item.get("status") == "available" for item in evidence_statuses),
            "stale": sum(item.get("status") == "stale" for item in evidence_statuses),
            "invalid": sum(
                item.get("status") in {"invalid", "cross_workspace", "unresolved"}
                for item in evidence_statuses
            ),
        },
    }


def _test_step_has_successful_evidence(workspace_id: str, step: dict[str, Any]) -> bool:
    """Require completed test/check steps to cite a successful run."""
    for item in step.get("evidence", []):
        if not isinstance(item, dict):
            continue
        evidence_type = item.get("type")
        evidence_id = item.get("id")
        if evidence_type == "process_id" and isinstance(evidence_id, str):
            process = db.get_process(evidence_id)
            if process and process.get("workspace_id") == workspace_id:
                if process.get("status") == "passed":
                    return True
        elif evidence_type == "check_id" and isinstance(evidence_id, str):
            if any(
                process.get("tool_name") == f"run_check:{evidence_id}"
                and process.get("status") == "passed"
                for process in db.list_processes(workspace_id)
            ):
                return True
    return False


def _run_git(path: Path, args: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [os.environ.get("GIT_EXECUTABLE", "git"), *args],
            cwd=str(path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout[:_MAX_OUTPUT],
        "stderr": result.stderr[:_MAX_OUTPUT],
        "truncated": len(result.stdout) > _MAX_OUTPUT or len(result.stderr) > _MAX_OUTPUT,
    }


def _parse_porcelain(output: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in output.splitlines():
        if len(line) >= 3:
            entries.append({"status_code": line[:2], "path": line[3:]})
    return entries


def _get_project_status(project_id: str) -> dict[str, Any]:
    project = get_project(project_id)
    if project is None:
        return error_result("PROJECT_NOT_FOUND", f"project not found: {project_id}")
    repository = Path(project["repository"]).expanduser().resolve()
    if not repository.is_dir():
        return error_result("STALE_WORKSPACE", f"main worktree path missing: {repository}")

    head = _run_git(repository, ["rev-parse", "HEAD"])
    branch = _run_git(repository, ["branch", "--show-current"])
    status = _run_git(repository, ["status", "--porcelain=v1", "--untracked-files=all"])
    registered = _run_git(repository, ["worktree", "list", "--porcelain"])
    status_entries = _parse_porcelain(status.get("stdout", ""))
    return ok_result(
        {
            "project_id": project_id,
            "main_worktree": str(repository),
            "main_head": head.get("stdout", "").strip() or None,
            "main_branch": branch.get("stdout", "").strip() or None,
            "main_working_tree_clean": not status_entries,
            "main_status": status_entries,
            "git_worktree_list": registered.get("stdout", "").strip(),
            "workspaces": list_workspaces(project_id),
            "errors": [
                value["error"] for value in (head, branch, status, registered) if "error" in value
            ],
        }
    )


def _get_workspace_report(workspace_id: str) -> dict[str, Any]:
    workspace = get_workspace(workspace_id)
    if workspace is None:
        return error_result(
            "WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id
        )
    worktree = Path(workspace["worktree_path"])
    if not worktree.is_dir():
        return error_result(
            "STALE_WORKSPACE", f"worktree path missing: {worktree}", workspace_id=workspace_id
        )

    status = _run_git(worktree, ["status", "--porcelain=v1", "--untracked-files=all"])
    diff_check = _run_git(worktree, ["diff", "--check"])
    head = _run_git(worktree, ["rev-parse", "HEAD"])
    commits = _run_git(
        worktree,
        ["log", "--format=%H%x09%s", f"{workspace['base_commit']}..HEAD"],
    )
    status_entries = _parse_porcelain(status.get("stdout", ""))

    process_manager = ProcessManager.get_instance()
    process_results: list[dict[str, Any]] = []
    latest_checks: dict[str, dict[str, Any]] = {}
    for process in db.list_processes(workspace_id):
        result = process_manager.get_result(process["process_id"])
        result["tool_name"] = process["tool_name"]
        result["script_preview"] = process.get("script_preview")
        process_results.append(result)
        tool_name = str(process["tool_name"])
        if tool_name.startswith("run_check:"):
            check_id = tool_name.split(":", 1)[1]
            latest_checks.setdefault(
                check_id,
                {
                    "check_id": check_id,
                    "latest_status": result.get("status"),
                    "exit_code": result.get("exit_code"),
                    "process_id": result.get("process_id"),
                    "completed_at": result.get("completed_at"),
                },
            )

    project_status = _get_project_status(workspace["project_id"])
    project_status_result = project_status.get("result", {}) if project_status.get("ok") else {}
    baseline_head = workspace.get("main_head_at_creation")
    baseline_status_sha256 = workspace.get("main_status_sha256_at_creation")
    current_main_status = "\n".join(
        f"{entry['status_code']} {entry['path']}"
        for entry in project_status_result.get("main_status", [])
    )
    if current_main_status:
        current_main_status += "\n"
    main_unchanged: bool | None = None
    if baseline_head is not None and baseline_status_sha256 is not None:
        main_unchanged = (
            project_status_result.get("main_head") == baseline_head
            and hashlib.sha256(current_main_status.encode("utf-8")).hexdigest()
            == baseline_status_sha256
        )

    check_statuses = [item["latest_status"] for item in latest_checks.values()]
    project = get_project(workspace["project_id"]) or {}
    required_checks = set((project.get("checks") or {}).keys())
    missing_checks = sorted(required_checks - latest_checks.keys())
    if any(state in {"failed", "timed_out", "cancelled"} for state in check_statuses):
        audit_state = "checks_failed"
    elif (
        required_checks
        and not missing_checks
        and all(state == "passed" for state in check_statuses)
    ):
        audit_state = "validated"
    elif latest_checks:
        audit_state = "checks_incomplete"
    elif status_entries:
        audit_state = "dirty"
    else:
        audit_state = workspace["status"]

    active_processes = [
        process
        for process in process_results
        if process.get("status") in {"queued", "running"}
    ]
    raw_plan = workspace_plan.get_plan(workspace_id).get("plan")
    plan = _plan_summary(raw_plan)
    acceptance_blockers: list[str] = []
    if active_processes:
        acceptance_blockers.append("active_processes")
    if diff_check.get("exit_code") != 0:
        acceptance_blockers.append("git_diff_check_failed")
    if main_unchanged is False:
        acceptance_blockers.append("main_repository_changed")
    if missing_checks:
        acceptance_blockers.append("required_checks_missing")
    if any(state != "passed" for state in check_statuses):
        acceptance_blockers.append("checks_not_passed")
    if raw_plan is None:
        acceptance_blockers.append("plan_missing")
    else:
        unfinished = [
            step
            for step in raw_plan.get("steps", [])
            if step.get("status") != "completed"
        ]
        if unfinished:
            acceptance_blockers.append("plan_incomplete")
        missing_test_evidence = [
            step.get("id")
            for step in raw_plan.get("steps", [])
            if step.get("status") == "completed"
            and _TEST_STEP_RE.search(str(step.get("text", "")))
            and not _test_step_has_successful_evidence(workspace_id, step)
        ]
        if missing_test_evidence:
            acceptance_blockers.append("test_step_missing_successful_evidence")

    return ok_result(
        {
            "workspace": workspace,
            "audit_state": audit_state,
            "git": {
                "head": head.get("stdout", "").strip() or None,
                "head_matches_base": head.get("stdout", "").strip() == workspace["base_commit"],
                "working_tree_clean": not status_entries,
                "changed_files": status_entries,
                "diff_check_passed": diff_check.get("exit_code") == 0,
                "diff_check_stdout": diff_check.get("stdout", ""),
                "diff_check_stderr": diff_check.get("stderr", ""),
                "commits_created": [
                    {"commit": line.split("\t", 1)[0], "subject": line.split("\t", 1)[-1]}
                    for line in commits.get("stdout", "").splitlines()
                    if line
                ],
            },
            "checks": list(latest_checks.values()),
            "required_checks": sorted(required_checks),
            "missing_checks": missing_checks,
            "processes": process_results,
            "active_processes": active_processes,
            "operations": db.list_operations(workspace_id),
            "artifacts": artifact_registry.count_artifacts(workspace_id),
            "plan": plan,
            "acceptance_ready": not acceptance_blockers,
            "acceptance_blockers": acceptance_blockers,
            "main_repo_unchanged_since_creation": main_unchanged,
            "project_status": project_status,
        },
        workspace_id=workspace_id,
    )


def register_tools(mcp: FastMCP) -> None:
    """Register read-only audit/reporting tools."""

    @mcp.tool(
        name="get_project_status",
        description=(
            "Return the main worktree HEAD, branch, structured Git status, "
            "registered Git worktrees, and operator workspaces for a project."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_project_status(project_id: str) -> dict[str, object]:
        return _get_project_status(project_id)

    @mcp.tool(
        name="get_workspace_report",
        description=(
            "Return a unified acceptance report for one workspace: latest checks, "
            "process evidence, Git state, commits, audit operations, and whether "
            "the main repository still matches its creation-time baseline."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_workspace_report(workspace_id: str) -> dict[str, object]:
        return _get_workspace_report(workspace_id)
