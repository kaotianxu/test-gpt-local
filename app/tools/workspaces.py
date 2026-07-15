"""MCP tools for workspace lifecycle.

Exposes:
  - create_workspace
  - get_workspace
  - list_workspaces
  - discard_workspace
"""

import logging

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services import workspace_manager

log = logging.getLogger(__name__)


def register_tools(mcp: FastMCP) -> None:
    """Register workspace lifecycle tools on the FastMCP instance."""

    @mcp.tool(
        name="create_workspace",
        description=(
            "Create a detached Git worktree for a registered project. "
            "The worktree lives under the project's worktree_root and is "
            "identified by a short workspace_id. The main repository is "
            "never modified; all changes must happen inside the worktree."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def create_workspace(
        project_id: str,
        task_name: str,
    ) -> dict[str, object]:
        """Create a detached worktree for a project task.

        Args:
            project_id: must be a key in config/projects.yaml.
            task_name: human-readable label stored in metadata.
        """
        log.info("create_workspace project_id=%s task_name=%s", project_id, task_name)
        try:
            return workspace_manager.create_workspace(project_id, task_name)
        except (ValueError, FileExistsError, RuntimeError) as exc:
            log.warning("create_workspace failed: %s", exc)
            return {"error": str(exc), "project_id": project_id, "task_name": task_name}

    @mcp.tool(
        name="get_workspace",
        description=(
            "Return metadata for a single workspace, including worktree_path "
            "and base_commit. Unknown IDs return an error payload."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_workspace(workspace_id: str) -> dict[str, object]:
        """Look up a workspace by ID."""
        record = workspace_manager.get_workspace(workspace_id)
        if record is None:
            return {"error": f"workspace not found: {workspace_id}", "workspace_id": workspace_id}
        return record

    @mcp.tool(
        name="list_workspaces",
        description=("List workspaces, newest first. Optionally filter by project_id."),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_workspaces(
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Return the workspace list, optionally filtered by project."""
        return workspace_manager.list_workspaces(project_id)

    @mcp.tool(
        name="discard_workspace",
        description=(
            "Permanently delete a worktree and its database record. "
            "All uncommitted changes inside the worktree are lost. "
            "The main repository and other workspaces are unaffected."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def discard_workspace(workspace_id: str) -> dict[str, object]:
        """Remove a workspace and its worktree from disk."""
        log.info("discard_workspace workspace_id=%s", workspace_id)
        try:
            return workspace_manager.discard_workspace(workspace_id)
        except ValueError as exc:
            log.warning("discard_workspace failed: %s", exc)
            return {"error": str(exc), "workspace_id": workspace_id}
