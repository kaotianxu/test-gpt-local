"""MCP tool: get_capabilities.

Returns the server's version, supported features, and limits so that
GPT can adapt its tool calls to what the current server supports.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import (
    get_artifact_config,
    get_files_config,
    get_process_config,
    load_operator_config,
)

SERVER_VERSION = "0.2.0"
SCHEMA_VERSION = "1.0.0"


def _build_capabilities() -> dict[str, object]:
    """Return the server capabilities dictionary."""
    proc_cfg = get_process_config()
    files_cfg = get_files_config()
    artifact_cfg = get_artifact_config()
    ws_cfg = load_operator_config().get("workspace", {})

    caps: dict[str, bool] = {
        "supports_async_process": True,
        "supports_expected_hash": True,
        "supports_idempotency": True,
        "supports_artifacts": True,
        "supports_multi_query_search": True,
        "supports_diff_context_lines": True,
        "supports_diff_stat_only": True,
        "supports_project_manifest": True,
        "supports_read_process_output": True,
        "supports_view_image": True,
        "supports_pty": True,
        "supports_process_input": True,
        "supports_process_signal": True,
        "supports_terminal_resize": True,
        "supports_artifact_registry": True,
        "supports_artifact_discovery": True,
        "supports_workspace_plan": True,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "server_version": SERVER_VERSION,
        "server_name": "gpt-local-code-operator",
        "capabilities": caps,
        "limits": {
            "max_read_chars": int(files_cfg.get("max_read_chars", 100000)),
            "max_output_chars": int(proc_cfg.get("max_output_chars", 200000)),
            "max_timeout_seconds": int(proc_cfg.get("max_timeout_seconds", 3600)),
            "max_active_workspaces_per_project": int(ws_cfg.get("max_active_per_project", 8)),
            "max_concurrent_jobs": int(proc_cfg.get("max_running_jobs", 3)),
            "max_artifact_discovery_files": int(
                artifact_cfg.get("max_discovery_files", 100)
            ),
        },
    }


def register_tools(mcp: FastMCP) -> None:
    """Register the get_capabilities tool on the FastMCP instance."""

    @mcp.tool(
        name="get_capabilities",
        description=(
            "Return the server version, supported features, and operational limits. "
            "Use this to discover what the current server supports before calling "
            "tools with optional parameters such as idempotency_key, expected_sha256, "
            "or multi_query."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_capabilities() -> dict[str, object]:
        """Return server capabilities and limits."""
        return _build_capabilities()
