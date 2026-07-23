"""MCP tool: apply_patch.

Applies a unified diff (patch) to a workspace worktree.  The patch is
validated before being applied — absolute paths, ``../`` traversal, and
non-workspace paths are rejected.  After a successful application the
caller receives the resulting ``git status --short`` and ``git diff
--stat`` so GPT can inspect what actually changed.

Wrap‑aware input
    Common wrapper markers such as ``*** Begin Patch`` / ``*** End Patch``
    are stripped automatically.  Only the unified diff section is passed to
    ``git apply``.

Error classification
    When ``git apply --check`` rejects a patch, the error is classified as
    one of:

    - ``format_error`` — the patch text is malformed (corrupt hunk header,
      missing diff metadata, etc.)
    - ``conflict`` — the patch syntax is correct but the target file's
      content does not match (wrong context lines, file already modified)
    - ``file_not_found`` — the target file does not exist in the worktree
    - ``unknown`` — could not be classified
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services.envelope import error_result, ok_result
from app.services.path_guard import is_denied, resolve_within
from app.services.subprocess_utils import no_window_creationflags
from app.services.workspace_manager import get_workspace
from app.storage import database as db
from app.storage.idempotency import with_idempotency

log = logging.getLogger(__name__)

# Maximum patch size as a safety guard (10 MB of text).
_MAX_PATCH_CHARS = 10_000_000

# Regex to match unified-diff header lines: ---/+++ followed by a path.
_PATCH_HEADER_RE = re.compile(r"^(?P<marker>---|\+\+\+)\s+(?P<path>.+)$")

# Regex to detect the start of a unified diff hunk.
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")

# A line that looks like a unified-diff content line (context, addition,
# removal, or hunk header).  Anything else is preamble or postamble.
_DIFF_LINE = re.compile(r"^[ @+-]")


# ── Wrapper stripping ──────────────────────────────────────────────────


def _strip_patch_wrappers(raw: str) -> str:
    """Strip common wrapper markers and non-diff preamble/postamble.

    Handles:
    - ``*** Begin Patch`` / ``*** End Patch``
    - ``--- Begin Patch`` / ``--- End Patch``
    - Arbitrary leading text before the first ``---`` or ``diff --git`` line
    - Arbitrary trailing text after the last diff hunk

    The return value is normalised to LF line endings.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")

    # ---------- Phase 1: remove explicit wrapper lines ----------
    wrapper_start = re.compile(
        r"^\*{3,}\s*begin\s+patch\s*\*{3,}$",
        re.IGNORECASE,
    )
    wrapper_end = re.compile(
        r"^\*{3,}\s*end\s+patch\s*\*{3,}$",
        re.IGNORECASE,
    )
    dashed_start = re.compile(
        r"^---+\s*begin\s+patch\s*---*$",
        re.IGNORECASE,
    )
    dashed_end = re.compile(
        r"^---+\s*end\s+patch\s*---*$",
        re.IGNORECASE,
    )

    def _is_wrapper(line: str) -> bool:
        return bool(
            wrapper_start.match(line)
            or wrapper_end.match(line)
            or dashed_start.match(line)
            or dashed_end.match(line)
        )

    lines = text.split("\n")
    lines = [line for line in lines if not _is_wrapper(line)]
    text = "\n".join(lines)

    # ---------- Phase 2: snip to the first diff-looking line ----------
    # A unified diff starts with either "--- " or "diff --git ".
    lines = text.split("\n")
    diff_start = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("--- ") or stripped.startswith("diff --git "):
            diff_start = i
            break

    if diff_start < 0:
        # No diff header found — return the raw text as-is so the caller
        # can produce a meaningful error.
        return text

    # ---------- Phase 3: trim trailing non-diff lines ----------
    # Walk forward tracking the last line that is definitely diff
    # content (hunk header, context/addition/removal, or ---/+++
    # header).  Blank lines are not diff content — they are either
    # separators within the diff or part of the postamble.
    last_diff = diff_start - 1
    for i in range(diff_start, len(lines)):
        stripped = lines[i].strip()
        if (
            _DIFF_LINE.match(stripped)
            or _PATCH_HEADER_RE.match(stripped)
            or _HUNK_HEADER_RE.match(stripped)
        ):
            last_diff = i

    # If nothing looked like diff content after the header, keep
    # the header itself.
    if last_diff < diff_start:
        last_diff = diff_start

    trimmed = lines[diff_start : last_diff + 1]
    return "\n".join(trimmed) + "\n"


# ── Path validation ────────────────────────────────────────────────────


def _validate_patch_paths(patch: str) -> None:
    """Reject patches that contain absolute paths or ``..`` traversal.

    Scans every ``---`` / ``+++`` header line in the patch.  Standard
    unified-diff prefixes ``a/`` and ``b/`` are stripped before checking.
    Raises ``ValueError`` with a description of the first violation.
    """
    for line in patch.splitlines():
        match = _PATCH_HEADER_RE.match(line)
        if not match:
            continue

        raw_path = match.group("path").strip().strip('"')

        # Strip the standard unified-diff prefixes a/ and b/.
        if raw_path.startswith(("a/", "b/")):
            raw_path = raw_path[2:]

        # Allow /dev/null (new/deleted file indicator).
        if raw_path == "/dev/null":
            continue

        # Reject absolute paths on any platform.
        p = Path(raw_path)
        if p.is_absolute():
            raise ValueError(f"patch contains absolute path: {raw_path!r}")

        # Windows drive-letter absolute (e.g. C:\foo).
        if re.match(r"^[A-Za-z]:[/\\]", raw_path):
            raise ValueError(f"patch contains absolute path: {raw_path!r}")

        # Unix-style absolute path (starts with / or \).
        # On Windows Path.is_absolute() returns False for /etc/passwd,
        # but such a path would escape the worktree, so reject it.
        if raw_path.startswith("/") or raw_path.startswith("\\"):
            raise ValueError(f"patch contains absolute path: {raw_path!r}")

        # Reject directory traversal.
        parts_normalised = raw_path.replace("\\", "/").split("/")
        if ".." in parts_normalised:
            raise ValueError(f"patch contains path traversal: {raw_path!r}")


# ── File extraction ────────────────────────────────────────────────────


def _normalise_header_path(raw_path: str) -> str:
    """Return a path from a unified-diff header without Git prefixes."""
    path = raw_path.strip().split("\t", 1)[0].strip('"')
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _extract_file_changes(patch: str) -> list[dict[str, str]]:
    """Return all added, modified, deleted, and renamed patch targets."""
    changes: list[dict[str, str]] = []
    lines = patch.splitlines()
    index = 0
    while index < len(lines) - 1:
        old_match = _PATCH_HEADER_RE.match(lines[index])
        new_match = _PATCH_HEADER_RE.match(lines[index + 1])
        if (
            old_match
            and new_match
            and old_match.group("marker") == "---"
            and new_match.group("marker") == "+++"
        ):
            old_path = _normalise_header_path(old_match.group("path"))
            new_path = _normalise_header_path(new_match.group("path"))
            if old_path == "/dev/null":
                changes.append({"path": new_path, "status": "added"})
            elif new_path == "/dev/null":
                changes.append({"path": old_path, "status": "deleted"})
            elif old_path != new_path:
                changes.append(
                    {
                        "path": new_path,
                        "old_path": old_path,
                        "status": "renamed",
                    }
                )
            else:
                changes.append({"path": new_path, "status": "modified"})
            index += 2
            continue
        index += 1

    deduplicated: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for change in changes:
        key = (change["path"], change["status"])
        if key not in seen:
            seen.add(key)
            deduplicated.append(change)
    return deduplicated


def _extract_changed_files(patch: str) -> list[str]:
    """Return every relative path changed by the patch, including deletions."""
    return [change["path"] for change in _extract_file_changes(patch)]


def _validate_unified_diff(patch: str) -> dict[str, Any] | None:
    """Validate hunk line counts and return a precise diagnostic on failure."""
    lines = patch.splitlines()
    saw_hunk = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("@@"):
            index += 1
            continue
        saw_hunk = True
        match = _HUNK_HEADER_RE.match(line)
        if match is None:
            return {
                "line": index + 1,
                "line_content": line,
                "parser_message": "invalid unified-diff hunk header",
            }

        expected_old = int(match.group(2) or "1")
        expected_new = int(match.group(4) or "1")
        actual_old = 0
        actual_new = 0
        cursor = index + 1
        while cursor < len(lines):
            content = lines[cursor]
            if content.startswith("@@") or content.startswith("diff --git "):
                break
            if content.startswith("--- ") and cursor + 1 < len(lines):
                if lines[cursor + 1].startswith("+++ "):
                    break
            if content.startswith("\\ No newline at end of file"):
                cursor += 1
                continue
            if not content or content[0] not in " +-":
                return {
                    "line": cursor + 1,
                    "line_content": content,
                    "parser_message": "hunk content line must start with space, +, or -",
                }
            if content[0] in " -":
                actual_old += 1
            if content[0] in " +":
                actual_new += 1
            cursor += 1

        if (actual_old, actual_new) != (expected_old, expected_new):
            return {
                "line": index + 1,
                "line_content": line,
                "parser_message": (
                    "hunk line count does not match header: "
                    f"expected old/new {expected_old}/{expected_new}, "
                    f"found {actual_old}/{actual_new}"
                ),
            }
        index = cursor

    if not saw_hunk:
        return {
            "line": 1,
            "line_content": lines[0] if lines else "",
            "parser_message": "patch contains no unified-diff hunks",
        }
    return None


def _diagnostic_preview(patch: str, line_number: int, radius: int = 2) -> str:
    """Return a small numbered patch excerpt around a diagnostic line."""
    lines = patch.splitlines()
    start = max(0, line_number - radius - 1)
    end = min(len(lines), line_number + radius)
    return "\n".join(f"{i + 1}: {lines[i]}" for i in range(start, end))


# ── Classification of change types ─────────────────────────────────────


def _classify_change_kind(diff_stdout: str) -> str:
    """Classify the overall change as ``code``, ``docs``, ``config``, or ``mixed``."""
    if not diff_stdout:
        return "unknown"
    code_extensions = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
    }
    docs_extensions = {".md", ".rst", ".txt", ".adoc", ".tex"}
    config_extensions = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env.example"}

    kinds: set[str] = set()
    for line in diff_stdout.splitlines():
        # git diff --stat output: "path/file.ext | N ++++++---"
        parts = line.split(" | ")
        if len(parts) < 2:
            continue
        path = parts[0].strip()
        ext = Path(path).suffix.lower()
        if ext in code_extensions:
            kinds.add("code")
        elif ext in docs_extensions:
            kinds.add("docs")
        elif ext in config_extensions:
            kinds.add("config")
        else:
            kinds.add("other")

    if len(kinds) == 1:
        return kinds.pop()
    if len(kinds) > 1:
        return "mixed"
    return "unknown"


# ── Error classification ───────────────────────────────────────────────


def _classify_patch_error(stderr: str) -> str:
    """Classify git apply --check stderr into a machine-readable category.

    Returns one of: ``format_error``, ``conflict``, ``file_not_found``, ``unknown``.
    """
    if not stderr:
        return "unknown"
    err_lower = stderr.lower()

    if "corrupt patch" in err_lower:
        return "format_error"
    if "malformed patch" in err_lower or "invalid hunk header" in err_lower:
        return "format_error"
    if "no such file" in err_lower or "does not exist" in err_lower:
        return "file_not_found"
    if "does not match" in err_lower or "patch failed" in err_lower:
        return "conflict"
    if "already exists" in err_lower:
        return "conflict"

    # Common git apply error patterns.
    if "error:" in stderr:
        return "format_error"

    return "unknown"


# ── Core implementation ────────────────────────────────────────────────


def _apply_patch(
    workspace_id: str,
    patch: str,
    explanation: str,
    check_only: bool = False,
    expected_sha256: dict[str, str] | None = None,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Validate and apply a unified diff patch to a workspace.

    1. Look up the workspace.
    2. Strip wrapper markers and normalise line endings.
    3. Validate patch safety (path checks).
    4. ``git apply --check`` — dry-run validation.
    5. (if not check_only) ``git apply`` — apply the patch via stdin.
    6. Return ``git status --short``, ``git diff --stat``, and the
       resulting diff.
    """
    # ---- 1. Look up workspace ----
    record = get_workspace(workspace_id)
    if record is None:
        return error_result(
            "WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id
        )
    worktree = Path(record["worktree_path"])
    if not worktree.exists():
        return error_result(
            "STALE_WORKSPACE",
            f"worktree path missing on disk: {worktree}",
            workspace_id=workspace_id,
        )

    # Basic input validation.
    if not patch or not patch.strip():
        return error_result(
            "INVALID_INPUT", "patch must be a non-empty string", workspace_id=workspace_id
        )
    if len(patch) > _MAX_PATCH_CHARS:
        return error_result(
            "INVALID_INPUT",
            f"patch exceeds maximum size of {_MAX_PATCH_CHARS} characters",
            workspace_id=workspace_id,
        )

    # ---- 1b. Validate expected_head if provided ----
    if expected_head is not None:
        head_proc = _run_git(worktree, ["git", "rev-parse", "HEAD"])
        if head_proc.get("exit_code", 1) != 0:
            return error_result(
                "INTERNAL_ERROR", "could not read workspace HEAD", workspace_id=workspace_id
            )
        actual_head = head_proc.get("stdout", "").strip()
        if actual_head != expected_head:
            return error_result(
                "FILE_CHANGED",
                "workspace HEAD changed since it was read: "
                f"expected {expected_head[:12]}, actual {actual_head[:12]}",
                workspace_id=workspace_id,
                extra={
                    "expected_head": expected_head,
                    "actual_head": actual_head,
                    "applied": False,
                    "check_only": check_only,
                },
            )

    # ---- 2. Strip wrappers and normalise line endings ----
    stripped = _strip_patch_wrappers(patch)

    # Detect whether stripping removed everything.
    has_diff_header = bool(
        re.search(r"^---\s+", stripped, re.MULTILINE)
        and re.search(r"^\+\+\+\s+", stripped, re.MULTILINE)
    )
    if not has_diff_header:
        return error_result(
            "INVALID_INPUT",
            "No unified diff headers found in the input. "
            "Expected format: a unified diff with ``---`` and ``+++`` "
            "file headers followed by ``@@`` hunk headers.",
            workspace_id=workspace_id,
            extra={"applied": False, "check_only": check_only},
        )

    # ---- 3. Validate syntax, paths, and optional optimistic-lock hashes ----
    op_id = uuid.uuid4().hex[:12]
    parser_diagnostic = _validate_unified_diff(stripped)
    if parser_diagnostic is not None:
        line_number = int(parser_diagnostic["line"])
        parser_diagnostic["normalized_patch_preview"] = _diagnostic_preview(stripped, line_number)
        db.log_operation(
            operation_id=op_id,
            tool_name="apply_patch",
            summary=(f"patch rejected (format_error): {parser_diagnostic['parser_message']}"),
            workspace_id=workspace_id,
            success=False,
        )
        db.complete_operation(op_id)
        return error_result(
            "INVALID_INPUT",
            "patch is not a valid unified diff",
            extra={
                "error_type": "format_error",
                **parser_diagnostic,
                "workspace_id": workspace_id,
                "applied": False,
                "check_only": check_only,
            },
        )

    try:
        _validate_patch_paths(stripped)
    except ValueError as exc:
        db.log_operation(
            operation_id=op_id,
            tool_name="apply_patch",
            summary=f"patch rejected: {exc}",
            workspace_id=workspace_id,
            success=False,
        )
        db.complete_operation(op_id)
        return error_result(
            "PATH_DENIED",
            str(exc),
            workspace_id=workspace_id,
            extra={"applied": False, "check_only": check_only},
        )

    if expected_sha256:
        for relative_path, expected in expected_sha256.items():
            normalised_path = _normalise_header_path(relative_path)
            if normalised_path == "/dev/null":
                continue
            candidate = (worktree / normalised_path).resolve()
            try:
                candidate.relative_to(worktree.resolve())
            except ValueError:
                return error_result(
                    "PATH_DENIED",
                    f"expected_sha256 path escapes workspace: {relative_path!r}",
                    workspace_id=workspace_id,
                    extra={"applied": False, "check_only": check_only},
                )
            actual = (
                hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.is_file() else None
            )
            if actual != expected:
                return error_result(
                    "FILE_CHANGED",
                    f"file changed since it was read: {normalised_path}",
                    workspace_id=workspace_id,
                    extra={
                        "path": normalised_path,
                        "expected_sha256": expected,
                        "actual_sha256": actual,
                        "applied": False,
                        "check_only": check_only,
                    },
                )

    # ---- 4. git apply --check (dry-run) ----
    git = _git_executable()
    check = _run_git(worktree, [git, "apply", "--check", "--"], input_str=stripped)
    if check.get("exit_code", 1) != 0:
        stderr = check.get("stderr", "").strip()
        stdout = check.get("stdout", "").strip()
        detail = stderr or stdout or "git apply --check failed (unknown error)"
        error_type = _classify_patch_error(detail)
        db.log_operation(
            operation_id=op_id,
            tool_name="apply_patch",
            summary=f"apply --check failed ({error_type}): {detail[:200]}",
            workspace_id=workspace_id,
            success=False,
        )
        db.complete_operation(op_id)
        response: dict[str, Any] = {
            "error_type": error_type,
            "check_details": detail,
            "workspace_id": workspace_id,
            "applied": False,
            "check_only": check_only,
        }
        line_match = re.search(r"(?:at|line)\s+(\d+)", detail, re.IGNORECASE)
        if line_match:
            line_number = int(line_match.group(1))
            patch_lines = stripped.splitlines()
            response.update(
                {
                    "line": line_number,
                    "line_content": (
                        patch_lines[line_number - 1] if 0 < line_number <= len(patch_lines) else ""
                    ),
                    "parser_message": detail,
                    "normalized_patch_preview": _diagnostic_preview(stripped, line_number),
                }
            )
        return error_result(
            "PATCH_CONFLICT",
            "patch did not apply cleanly",
            workspace_id=workspace_id,
            extra=response,
        )

    # If check_only, stop here.
    if check_only:
        db.log_operation(
            operation_id=op_id,
            tool_name="apply_patch",
            summary=f"check_only passed: {explanation[:100]}",
            workspace_id=workspace_id,
            success=True,
        )
        db.complete_operation(op_id)
        file_changes = _extract_file_changes(stripped)
        return ok_result(
            {
                "workspace_id": workspace_id,
                "explanation": explanation,
                "applied": False,
                "check_only": True,
                "check_passed": True,
                "check_details": "Patch applies cleanly.",
                "changed_files": _extract_changed_files(stripped),
                "file_changes": file_changes,
            },
            workspace_id=workspace_id,
        )

    # ---- 5. git apply -- (apply via stdin) ----
    apply = _run_git(worktree, [git, "apply", "--"], input_str=stripped)
    if apply.get("exit_code", 1) != 0:
        stderr = apply.get("stderr", "").strip()
        stdout = apply.get("stdout", "").strip()
        detail = stderr or stdout or "git apply failed (unknown error)"
        error_type = _classify_patch_error(detail)
        db.log_operation(
            operation_id=op_id,
            tool_name="apply_patch",
            summary=f"apply failed ({error_type}): {detail[:200]}",
            workspace_id=workspace_id,
            success=False,
        )
        db.complete_operation(op_id)
        return error_result(
            "PATCH_CONFLICT",
            "git apply failed",
            workspace_id=workspace_id,
            extra={
                "error_type": error_type,
                "apply_details": detail,
                "applied": False,
                "check_only": check_only,
            },
        )

    # ---- 6. Capture git status, diff stat, and the actual diff ----
    status_result = _run_git(worktree, [git, "status", "--short", "--branch"])
    diff_stat_result = _run_git(worktree, [git, "diff", "--stat", "--no-color"])
    diff_result = _run_git(worktree, [git, "diff", "--no-color"])

    changed_files = _extract_changed_files(stripped)
    file_changes = _extract_file_changes(stripped)
    change_kind = _classify_change_kind(diff_stat_result.get("stdout", ""))

    db.log_operation(
        operation_id=op_id,
        tool_name="apply_patch",
        summary=f"applied patch: {len(changed_files)} file(s) changed. {explanation[:100]}",
        workspace_id=workspace_id,
        success=True,
    )
    db.complete_operation(op_id)

    return ok_result(
        {
            "workspace_id": workspace_id,
            "explanation": explanation,
            "applied": True,
            "check_only": False,
            "changed_files": changed_files,
            "file_changes": file_changes,
            "change_kind": change_kind,
            "diff_stat": diff_stat_result.get("stdout", "").strip(),
            "diff": diff_result.get("stdout", "").strip(),
            "git_status": status_result.get("stdout", "").strip(),
        },
        workspace_id=workspace_id,
    )


def _run_git(
    worktree: Path, cmd: list[str], *, input_str: str | None = None, timeout: int = 30
) -> dict[str, Any]:
    """Run a git subprocess and return structured output."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(worktree),
            input=input_str,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError:
        return {"error": "git executable not found"}
    except subprocess.TimeoutExpired:
        return {"error": f"git command timed out after {timeout}s"}
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _git_executable() -> str:
    return os.environ.get("GIT_EXECUTABLE", "git")


def _replace_text(
    workspace_id: str,
    path: str,
    old_text: str,
    new_text: str,
    explanation: str,
    expected_sha256: str | None = None,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Perform an exact, concurrency-safe text replacement in one file."""
    record = get_workspace(workspace_id)
    if record is None:
        return error_result(
            "WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id
        )
    worktree = Path(record["worktree_path"])
    try:
        target = resolve_within(worktree, path)
    except ValueError as exc:
        return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)
    if is_denied(target, worktree):
        return error_result("PATH_DENIED", "path is denied by policy", workspace_id=workspace_id)
    if not target.is_file():
        return error_result(
            "INVALID_INPUT", "path is not a regular file", workspace_id=workspace_id
        )
    if not old_text:
        return error_result(
            "INVALID_INPUT", "old_text must be non-empty", workspace_id=workspace_id
        )

    raw = target.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        return error_result(
            "FILE_CHANGED",
            f"file changed since it was read: {path}",
            workspace_id=workspace_id,
            extra={
                "error_type": "stale_content",
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
                "path": path,
            },
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return error_result(
            "INVALID_INPUT", "file is not valid UTF-8 text", workspace_id=workspace_id
        )

    occurrences = content.count(old_text)
    if occurrences == 0:
        return error_result(
            "PATCH_CONFLICT",
            "old_text was not found",
            workspace_id=workspace_id,
            extra={"error_type": "conflict", "path": path},
        )
    if occurrences > 1 and not replace_all:
        return error_result(
            "PATCH_CONFLICT",
            "old_text is ambiguous; set replace_all=true or provide more context",
            workspace_id=workspace_id,
            extra={"error_type": "conflict", "occurrences": occurrences, "path": path},
        )

    updated = content.replace(old_text, new_text, -1 if replace_all else 1)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8", newline="")
        os.chmod(temporary, target.stat().st_mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    git = _git_executable()
    status = _run_git(worktree, [git, "status", "--short", "--branch"])
    diff_stat = _run_git(worktree, [git, "diff", "--stat", "--no-color"])
    diff = _run_git(worktree, [git, "diff", "--no-color", "--", path])
    op_id = uuid.uuid4().hex[:12]
    db.log_operation(
        operation_id=op_id,
        tool_name="replace_text",
        summary=f"replaced text in {path}: {explanation[:100]}",
        workspace_id=workspace_id,
        success=True,
    )
    db.complete_operation(op_id)
    return ok_result(
        {
            "workspace_id": workspace_id,
            "path": path,
            "replacements": occurrences if replace_all else 1,
            "changed_files": [path],
            "file_changes": [{"path": path, "status": "modified"}],
            "sha256_before": actual_sha256,
            "sha256_after": hashlib.sha256(target.read_bytes()).hexdigest(),
            "diff_stat": diff_stat.get("stdout", "").strip(),
            "diff": diff.get("stdout", "").strip(),
            "git_status": status.get("stdout", "").strip(),
        },
        workspace_id=workspace_id,
    )


# ── Tool registration ──────────────────────────────────────────────────


def register_tools(mcp: FastMCP) -> None:
    """Register the apply_patch tool on the FastMCP instance."""

    @mcp.tool(
        name="apply_patch",
        description=(
            "Validate and apply a unified diff (``git diff`` or ``diff -u``"
            " format) to a workspace worktree.  The patch is first checked"
            " with ``git apply --check``; if it passes, it is applied via"
            " ``git apply``.\n\n"
            "**Patch format requirements:**\n"
            "- Raw unified diff with ``---`` / ``+++`` file headers and ``@@``"
            " hunk headers.\n"
            "- Paths must be workspace-relative (e.g. ``src/main.py``, not"
            " ``/absolute/path/src/main.py``).\n"
            "- The ``a/`` and ``b/`` prefixes are optional but recommended.\n"
            "- File creation (``--- /dev/null``) and deletion"
            " (``+++ /dev/null``) are supported.\n"
            "- Binary files are **not** supported.\n\n"
            "**Wrapper stripping:**\n"
            "Common wrapper markers such as ``*** Begin Patch ***`` /"
            " ``*** End Patch ***`` are automatically removed.\n\n"
            "**Error classification:**\n"
            "When a patch is rejected, the response includes an"
            " ``error_type`` field:\n"
            "- ``format_error`` — the patch text is malformed\n"
            "- ``conflict`` — syntax is correct but context does not match\n"
            "- ``file_not_found`` — the target file does not exist\n\n"
            "**Return value:**\n"
            "On success the response includes ``changed_files``,"
            " structured ``file_changes`` (added/modified/deleted/renamed),"
            " ``change_kind`` (code/docs/config/mixed), ``diff_stat``,"
            " ``diff`` (the actual changes), and ``git_status`` so you can"
            " inspect what changed without a separate tool call.\n\n"
            "**Idempotency:**\n"
            "Pass an ``idempotency_key`` to safely retry this call."
            " When the same key is used with the same inputs, the server"
            " returns the cached result without reapplying the patch."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def apply_patch(
        workspace_id: str,
        patch: str,
        explanation: str,
        check_only: bool = False,
        expected_sha256: dict[str, str] | None = None,
        expected_head: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Apply a unified diff to the workspace.

        Args:
            workspace_id: The target workspace.
            patch: The unified diff content (``diff -u`` or ``git diff``
                format).  ``*** Begin Patch ***`` wrappers are stripped
                automatically.
            explanation: A brief human-readable summary of what the patch
                does (stored in the operation log).
            check_only: When ``True``, only validate the patch with ``git
                apply --check`` without applying it.  Defaults to ``False``.
            expected_sha256: Optional mapping of relative file paths to hashes
                returned by read_files. The patch is rejected if any file has
                changed since it was read.
            expected_head: Optional expected HEAD commit hash. The patch is
                rejected if the workspace HEAD does not match.
            idempotency_key: Optional key for idempotent retry. When provided,
                duplicate requests with the same key return the cached result.
        """
        log.info(
            "apply_patch workspace_id=%s explanation=%s patch_len=%d "
            "check_only=%s idempotency_key=%s",
            workspace_id,
            explanation,
            len(patch),
            check_only,
            idempotency_key,
        )
        return with_idempotency(
            idempotency_key,
            "apply_patch",
            {
                "workspace_id": workspace_id,
                "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                "explanation_sha256": hashlib.sha256(explanation.encode("utf-8")).hexdigest(),
                "check_only": check_only,
                "expected_head": expected_head,
            },
            lambda: _apply_patch(
                workspace_id,
                patch,
                explanation,
                check_only,
                expected_sha256,
                expected_head,
            ),
        )

    @mcp.tool(
        name="replace_text",
        description=(
            "Replace an exact UTF-8 text fragment in one workspace file. "
            "By default the fragment must occur exactly once. Pass the SHA-256 "
            "returned by read_files to reject edits based on stale content. "
            "Use this for small targeted edits that do not need a handwritten "
            "unified diff. Returns Git status/diff evidence and before/after hashes."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def replace_text(
        workspace_id: str,
        path: str,
        old_text: str,
        new_text: str,
        explanation: str,
        expected_sha256: str | None = None,
        replace_all: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Perform an exact text replacement inside a workspace file."""
        log.info(
            "replace_text workspace_id=%s path=%s explanation=%s idempotency_key=%s",
            workspace_id,
            path,
            explanation,
            idempotency_key,
        )
        return with_idempotency(
            idempotency_key,
            "replace_text",
            {
                "workspace_id": workspace_id,
                "path": path,
                "old_text_sha256": hashlib.sha256(old_text.encode("utf-8")).hexdigest(),
                "new_text_sha256": hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
                "replace_all": replace_all,
            },
            lambda: _replace_text(
                workspace_id,
                path,
                old_text,
                new_text,
                explanation,
                expected_sha256,
                replace_all,
            ),
        )
