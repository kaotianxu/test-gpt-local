"""MCP tools for project discovery and server health.

Provides:
  - ping: lightweight connectivity check
  - list_projects: return registered project IDs and names
"""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import load_projects_config


def register_tools(mcp: FastMCP) -> None:
    """Register all project-related tools on the FastMCP instance."""

    @mcp.tool(
        name="ping",
        description="Check if the MCP server is reachable and responsive.",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def ping() -> dict[str, str]:
        """Return a simple pong response to confirm connectivity."""
        return {
            "status": "ok",
            "service": "gpt-local-code-operator",
            "version": "0.1.0",
        }

    @mcp.tool(
        name="list_projects",
        description="Return all registered projects that are available for workspace creation.",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_projects() -> list[dict[str, str]]:
        """Return a list of registered projects with their IDs and display names."""
        projects = load_projects_config()
        return [
            {
                "project_id": pid,
                "name": info.get("name", pid),
            }
            for pid, info in projects.items()
        ]
