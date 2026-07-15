"""Path validation helpers used by the read/search/repo-map tools.

These enforce the boundaries described in the plan (section 15.3):
requests must resolve to a path inside the workspace, and a small list
of always-denied basenames is refused by the file reader. This module
is intentionally narrow — `run_pwsh` is not constrained by it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.config import get_files_config

# Always-blocked file or directory basenames (case-insensitive).
_DEFAULT_DENY = (".git", ".env", ".env.local")


def _deny_set() -> set[str]:
    cfg = get_files_config()
    deny: Iterable[str] = cfg.get("deny_paths", _DEFAULT_DENY)
    return {str(p).strip().lower() for p in deny if str(p).strip()}


def resolve_within(
    worktree: Path,
    relative: str,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve ``relative`` against ``worktree`` and reject escapes.

    The returned path is absolute. Raises ValueError when the request
    tries to leave the worktree, references a denied path, or (when
    must_exist=True) does not exist.
    """
    if relative is None or relative == "":
        raise ValueError("path must be a non-empty relative path")

    # Reject absolute paths and obvious traversal tokens.
    p = Path(relative)
    parts = p.parts
    if p.is_absolute() or any(part == ".." for part in parts):
        raise ValueError(f"path escapes the workspace: {relative!r}")

    base = worktree.resolve()
    candidate = (base / p).resolve()

    # Containment check (handles Windows case-insensitivity as Path does).
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path escapes the workspace: {relative!r}") from exc

    if must_exist and not candidate.exists():
        raise ValueError(f"path does not exist in workspace: {relative!r}")

    return candidate


def is_denied(absolute_path: Path, root: Path | None = None) -> bool:
    """Return True if any path component matches a denied entry."""
    deny = _deny_set()
    parts = absolute_path.parts
    if root is not None:
        try:
            parts = absolute_path.resolve().relative_to(root.resolve()).parts
        except ValueError:
            pass
    return any(part.lower() in deny for part in parts)
