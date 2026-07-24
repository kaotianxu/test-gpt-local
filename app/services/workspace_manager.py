"""Workspace manager service.

Creates, inspects, and discards detached Git worktrees for project tasks.
Each workspace is recorded in the SQLite state store so the MCP server can
reload it after a restart.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.config import get_project, load_operator_config
from app.services.process_manager import ProcessManager
from app.services.subprocess_utils import no_window_creationflags
from app.storage import database as db

# Workspace IDs are short, URL-safe, and not user-controlled. Format:
#   ws-XXXXXXXX  (8 lowercase hex chars)
_WORKSPACE_ID_RE = re.compile(r"^ws-[0-9a-f]{8}$")


def _generate_workspace_id() -> str:
    """Return a new short workspace identifier."""
    return "ws-" + secrets.token_hex(4)


def _ensure_safe_task_name(task_name: str) -> str:
    """Validate a task name and return a directory-safe form.

    The task name is used as part of the worktree directory name for
    human readability, so we keep letters, digits, dot, underscore and
    hyphen. The original name is stored in the database verbatim.
    """
    if not task_name or not task_name.strip():
        raise ValueError("task_name must not be empty")
    if len(task_name) > 200:
        raise ValueError("task_name must be 200 characters or fewer")
    if any(ch in task_name for ch in ("/", "\\", "\0")):
        raise ValueError("task_name must not contain path separators")
    return task_name.strip()


def _worktree_dir_name(workspace_id: str, task_name: str) -> str:
    """Build a filesystem-safe directory name for the worktree."""
    safe_task = re.sub(r"[^A-Za-z0-9._-]+", "-", task_name).strip("-")
    if not safe_task:
        safe_task = "task"
    return f"{workspace_id}-{safe_task}"


def _resolve_git_executable() -> str:
    """Return the git executable path; rely on PATH by default."""
    return os.environ.get("GIT_EXECUTABLE", "git")


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git subprocess and return the result."""
    cmd = [_resolve_git_executable(), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        creationflags=no_window_creationflags(),
    )


def _normalise_repo_path(repo: str) -> Path:
    """Resolve a repository path to an absolute Windows path."""
    return Path(repo).expanduser().resolve()


def _is_git_repo(path: Path) -> bool:
    """Check whether a path is the working tree of a Git repository."""
    if not path.is_dir():
        return False
    proc = _run_git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    if proc.returncode != 0:
        return False
    toplevel = proc.stdout.strip()
    return bool(toplevel) and Path(toplevel).resolve() == path.resolve()


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    """Return a workspace record by ID, or None if not found."""
    if not _WORKSPACE_ID_RE.match(workspace_id):
        return None
    record = db.get_workspace(workspace_id)
    if record is not None:
        db.touch_workspace(workspace_id)
    return record


def list_workspaces(project_id: str | None = None) -> list[dict[str, Any]]:
    """Return all workspaces, optionally filtered by project."""
    return db.list_workspaces(project_id)


def _worktree_path_for(project: dict[str, Any], workspace_id: str, task_name: str) -> Path:
    """Compute the destination path for a new worktree."""
    root = Path(project["worktree_root"]).expanduser().resolve()
    return root / _worktree_dir_name(workspace_id, task_name)


def _active_count(project_id: str) -> int:
    """Count the active workspaces for a project."""
    return sum(1 for w in db.list_workspaces(project_id) if w["status"] == "active")


def _discover_project_manifest(repo: Path) -> dict[str, Any]:
    """Scan the repository for well-known files and return a project manifest.

    The manifest helps GPT understand the project structure without
    additional tool calls.
    """
    manifest: dict[str, Any] = {
        "languages": [],
        "instructions": [],
        "test_commands": [],
        "package_manager": None,
        "entrypoints": [],
        "project_config_files": [],
        "git_head": None,
    }

    # Capture HEAD.
    head_proc = _run_git(["rev-parse", "HEAD"], cwd=repo, check=False)
    if head_proc.returncode == 0:
        manifest["git_head"] = head_proc.stdout.strip()

    # Walk well-known files.
    for fname in ("AGENTS.md", "CONTRIBUTING.md", "README.md", "README.rst"):
        if (repo / fname).is_file():
            manifest["instructions"].append(fname)

    # Detect project config files and language.
    if (repo / "pyproject.toml").is_file():
        manifest["project_config_files"].append("pyproject.toml")
        manifest["languages"].append("python")
        # Detect package manager from pyproject.toml content.
        try:
            text = (repo / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            if "[tool.uv]" in text or "uv" in text:
                manifest["package_manager"] = "uv"
            elif "[tool.poetry]" in text:
                manifest["package_manager"] = "poetry"
            elif "[project]" in text:
                manifest["package_manager"] = "pip"
        except OSError:
            pass

    if (repo / "package.json").is_file():
        manifest["project_config_files"].append("package.json")
        if "javascript" not in manifest["languages"] and "typescript" not in manifest["languages"]:
            manifest["languages"].append("javascript")
        if "typescript" not in manifest["languages"]:
            # Check for tsconfig.json or TypeScript dependencies.
            if (repo / "tsconfig.json").is_file():
                manifest["languages"].append("typescript")
        try:
            import json as _json

            _json.loads((repo / "package.json").read_text(encoding="utf-8"))
            if not manifest["package_manager"]:
                manifest["package_manager"] = "npm"
        except (OSError, _json.JSONDecodeError):
            pass

    if (repo / "Cargo.toml").is_file():
        manifest["project_config_files"].append("Cargo.toml")
        manifest["languages"].append("rust")
        if not manifest["package_manager"]:
            manifest["package_manager"] = "cargo"

    if (repo / "Makefile").is_file():
        manifest["project_config_files"].append("Makefile")

    if (repo / "Gemfile").is_file():
        manifest["project_config_files"].append("Gemfile")
        manifest["languages"].append("ruby")
        if not manifest["package_manager"]:
            manifest["package_manager"] = "bundler"

    # Scan for .sln and .csproj files.
    sln_files = list(repo.glob("*.sln"))
    csproj_files = list(repo.glob("**/*.csproj"))
    if sln_files:
        manifest["project_config_files"].extend(str(f.relative_to(repo)) for f in sln_files[:3])
        manifest["languages"].append("csharp")
    if csproj_files:
        manifest["project_config_files"].extend(str(f.relative_to(repo)) for f in csproj_files[:3])
        if "csharp" not in manifest["languages"]:
            manifest["languages"].append("csharp")
        if not manifest["package_manager"]:
            manifest["package_manager"] = "nuget"

    # Discover entrypoints via simple heuristics.
    py_files = list(repo.rglob("*.py"))
    for pyf in py_files:
        if pyf.name == "__main__.py":
            rel = str(pyf.relative_to(repo))
            if rel not in manifest["entrypoints"]:
                manifest["entrypoints"].append(rel)
            break
    for pyf in py_files:
        try:
            text = pyf.read_text(encoding="utf-8", errors="replace")
            if 'if __name__ == "__main__"' in text or "def main()" in text:
                rel = str(pyf.relative_to(repo))
                if rel not in manifest["entrypoints"]:
                    manifest["entrypoints"].append(rel)
            if len(manifest["entrypoints"]) >= 5:
                break
        except OSError:
            continue

    # Deduplicate languages.
    seen_langs: set[str] = set()
    manifest["languages"] = [
        lang
        for lang in manifest["languages"]
        if lang not in seen_langs and not seen_langs.add(lang)  # type: ignore[func-returns-value]
    ]

    return manifest


def create_workspace(project_id: str, task_name: str) -> dict[str, Any]:
    """Create a detached Git worktree for a project task.

    Raises:
        ValueError: if the project is not registered or inputs are invalid.
        FileExistsError: if the destination worktree directory already exists.
        RuntimeError: if the repository is not a Git repo or git fails.
    """
    project = get_project(project_id)
    if project is None:
        raise ValueError(f"project_id not registered: {project_id!r}")
    safe_task = _ensure_safe_task_name(task_name)

    cfg = load_operator_config()
    max_active = int(cfg["workspace"].get("max_active_per_project", 8))
    if _active_count(project_id) >= max_active:
        raise RuntimeError(
            f"project {project_id!r} already has {max_active} active workspaces; "
            "discard one before creating a new one"
        )

    repo = _normalise_repo_path(project["repository"])
    if not _is_git_repo(repo):
        raise RuntimeError(f"repository is not a Git working tree: {repo}")

    main_head_result = _run_git(["rev-parse", "HEAD"], cwd=repo, check=False)
    main_status_result = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo,
        check=False,
    )
    if main_head_result.returncode != 0 or main_status_result.returncode != 0:
        raise RuntimeError("could not capture the main worktree baseline")

    workspace_id = _generate_workspace_id()
    worktree_path = _worktree_path_for(project, workspace_id, safe_task)
    if worktree_path.exists():
        raise FileExistsError(f"worktree path already exists: {worktree_path}")

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Create detached worktree at HEAD.
    proc = _run_git(
        ["worktree", "add", "--detach", str(worktree_path), "HEAD"],
        cwd=repo,
        check=False,
    )
    if proc.returncode != 0:
        # Clean up the directory we created, if any.
        if worktree_path.exists() and not any(worktree_path.iterdir()):
            try:
                worktree_path.rmdir()
            except OSError:
                pass
        raise RuntimeError(
            f"git worktree add failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )

    # Capture the base commit (HEAD of the new worktree).
    rev_proc = _run_git(["rev-parse", "HEAD"], cwd=worktree_path, check=False)
    if rev_proc.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed in new worktree: {rev_proc.stderr.strip()}")
    base_commit = rev_proc.stdout.strip()

    record = db.insert_workspace(
        workspace_id=workspace_id,
        project_id=project_id,
        task_name=safe_task,
        worktree_path=str(worktree_path),
        base_commit=base_commit,
        main_head_at_creation=main_head_result.stdout.strip(),
        main_status_sha256_at_creation=hashlib.sha256(
            main_status_result.stdout.encode("utf-8")
        ).hexdigest(),
    )

    # Attach project manifest.
    manifest = _discover_project_manifest(repo)
    record["project_manifest"] = manifest
    return record


def discard_workspace(workspace_id: str) -> dict[str, Any]:
    """Remove a worktree from disk and the state store.

    Returns a summary dict describing what was removed. Raises
    ValueError if the workspace id is invalid or unknown.
    """
    if not _WORKSPACE_ID_RE.match(workspace_id):
        raise ValueError(f"invalid workspace_id format: {workspace_id!r}")
    record = db.get_workspace(workspace_id)
    if record is None:
        raise ValueError(f"workspace not found: {workspace_id!r}")
    from app.services.change_set_store import (
        ChangeSetError,
        assert_workspace_discardable,
        delete_terminal_data,
    )

    try:
        assert_workspace_discardable(workspace_id)
    except ChangeSetError as exc:
        raise ValueError(str(exc)) from exc

    worktree_path = Path(record["worktree_path"])
    project = get_project(record["project_id"])
    removed_path = False
    git_error: str | None = None
    git_registration_removed = False

    # Stop commands before removing their working directory or audit records.
    ProcessManager.get_instance().cancel_all_for_workspace(workspace_id)

    if project is not None:
        repo = _normalise_repo_path(project["repository"])
        if _is_git_repo(repo):
            proc = _run_git(
                ["worktree", "remove", "--force", str(worktree_path)],
                cwd=repo,
                check=False,
            )
            if proc.returncode == 0:
                removed_path = True
            else:
                # Fall back to manual cleanup. `git worktree prune` then
                # delete the directory if it is empty.
                _run_git(["worktree", "prune"], cwd=repo, check=False)
                if worktree_path.exists():
                    try:
                        shutil.rmtree(worktree_path)
                        removed_path = True
                    except OSError as exc:
                        git_error = (
                            f"git worktree remove failed ({proc.stderr.strip()}); "
                            f"manual cleanup failed: {exc}"
                        )
                else:
                    removed_path = True
                    git_error = (
                        f"git worktree remove reported error but path is gone: "
                        f"{proc.stderr.strip()}"
                    )
            _run_git(["worktree", "prune"], cwd=repo, check=False)
            registrations = _run_git(["worktree", "list", "--porcelain"], cwd=repo, check=False)
            registered_paths = {
                Path(line.removeprefix("worktree ")).resolve()
                for line in registrations.stdout.splitlines()
                if line.startswith("worktree ")
            }
            git_registration_removed = worktree_path.resolve() not in registered_paths
    else:
        # Project no longer registered; best-effort cleanup of the path.
        if worktree_path.exists():
            try:
                shutil.rmtree(worktree_path)
                removed_path = True
            except OSError as exc:
                git_error = f"manual cleanup failed: {exc}"

    path_exists_after = worktree_path.exists()
    if project is None:
        git_registration_removed = not path_exists_after

    database_record_removed = False
    if not path_exists_after and git_registration_removed:
        ProcessManager.get_instance().delete_outputs_for_workspace(workspace_id)
        delete_terminal_data(workspace_id)
        db.delete_workspace(workspace_id)
        database_record_removed = db.get_workspace(workspace_id) is None
    elif git_error is None:
        git_error = "workspace cleanup verification failed; database record retained"

    return {
        "workspace_id": workspace_id,
        "removed_path": removed_path,
        "filesystem_removed": not path_exists_after,
        "path_exists_after": path_exists_after,
        "git_registration_removed": git_registration_removed,
        "database_record_removed": database_record_removed,
        "worktree_path": str(worktree_path),
        "remaining_project_workspaces": [
            item["workspace_id"]
            for item in db.list_workspaces(record["project_id"])
            if item["workspace_id"] != workspace_id
        ],
        "error": git_error,
    }
