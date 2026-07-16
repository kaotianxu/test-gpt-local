"""MCP tool: get_repo_map.

Returns a high-level directory overview for a workspace: top-level
entries with simple file/directory classification. Designed to give GPT
a quick map before reading specific files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services.envelope import error_result, ok_result
from app.services.path_guard import is_denied, resolve_within
from app.services.workspace_manager import get_workspace

log = logging.getLogger(__name__)

# Cap the depth of the returned tree so a giant repo cannot produce an
# unbounded response.
_MAX_DEPTH = 4
# Cap the number of entries per directory; a hint to GPT to be selective.
_MAX_ENTRIES = 200


def _classify(entry: Path) -> str:
    if entry.is_dir():
        return "directory"
    if entry.is_file():
        return "file"
    if entry.is_symlink():
        return "symlink"
    return "other"


def _build_tree(
    root: Path,
    depth: int = 0,
    deny_root: Path | None = None,
) -> dict[str, Any]:
    """Recursively build a tree dict, capped by depth and entry count."""
    if depth > _MAX_DEPTH:
        return {"truncated": True, "reason": "max_depth"}
    if not root.is_dir():
        return {"type": _classify(root), "name": root.name}

    children: list[dict[str, Any]] = []
    try:
        iterator = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (PermissionError, OSError) as exc:
        return {"type": "directory", "name": root.name, "error": str(exc)}

    truncated = False
    for child in iterator:
        if is_denied(child, deny_root):
            continue
        if len(children) >= _MAX_ENTRIES:
            truncated = True
            break
        node: dict[str, Any] = {
            "name": child.name,
            "type": _classify(child),
        }
        if child.is_dir():
            try:
                subtree = _build_tree(child, depth + 1, deny_root)
                node.update(subtree)
            except (PermissionError, OSError) as exc:
                node["error"] = str(exc)
        children.append(node)

    return {
        "type": "directory",
        "name": root.name,
        "children": children,
        **({"truncated": True} if truncated else {}),
    }


def _repo_map(workspace_id: str, path: str = "") -> dict[str, Any]:
    record = get_workspace(workspace_id)
    if record is None:
        return error_result(
            "WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}", workspace_id=workspace_id
        )
    worktree = Path(record["worktree_path"])

    if path.strip() == "":
        root = worktree
        rel = "."
    else:
        try:
            root = resolve_within(worktree, path)
            if is_denied(root, worktree):
                raise ValueError("path is denied by policy")
        except ValueError as exc:
            return error_result("PATH_DENIED", str(exc), workspace_id=workspace_id)
        rel = path

    tree = _build_tree(root, deny_root=worktree)
    return ok_result(
        {
            "workspace_id": workspace_id,
            "path": rel,
            "worktree_path": str(worktree),
            "tree": tree,
        },
        workspace_id=workspace_id,
    )


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_repo_map",
        description=(
            "Return a directory overview for a workspace. Defaults to the "
            "worktree root; pass a relative path to inspect a subdirectory. "
            "Use this before reading large files to navigate the codebase."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_repo_map(
        workspace_id: str,
        path: str = "",
    ) -> dict[str, object]:
        log.info("get_repo_map workspace_id=%s path=%s", workspace_id, path)
        return _repo_map(workspace_id, path)
