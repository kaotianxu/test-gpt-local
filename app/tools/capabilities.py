"""MCP tool: get_capabilities.

Returns the server's version, supported features, and limits so that
GPT can adapt its tool calls to what the current server supports.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import (
    get_artifact_config,
    get_change_set_config,
    get_events_config,
    get_files_config,
    get_process_config,
    load_operator_config,
)
from app.services.tool_registry import (
    IdempotencyPolicy,
    list_tool_specs,
    tool_contracts,
)


def _server_version() -> str:
    """Read installed metadata, falling back to pyproject in a source checkout."""
    try:
        return version("gpt-local-code-operator")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])


SERVER_VERSION = _server_version()
SCHEMA_VERSION = "1.0.0"


def _build_capabilities() -> dict[str, object]:
    """Return capabilities derived from the central ToolSpec registry."""
    proc_cfg = get_process_config()
    files_cfg = get_files_config()
    artifact_cfg = get_artifact_config()
    event_cfg = get_events_config()
    change_set_cfg = get_change_set_config()
    ws_cfg = load_operator_config().get("workspace", {})

    specs = list_tool_specs()
    tool_names = {spec.name for spec in specs}
    requirements: dict[str, set[str]] = {
        "supports_async_process": {"run_pwsh", "get_process_result"},
        "supports_expected_hash": {"read_files", "apply_patch", "replace_text"},
        "supports_artifacts": {"list_artifacts", "read_artifact", "view_artifact"},
        "supports_multi_query_search": {"search_code"},
        "supports_diff_context_lines": {"git_diff"},
        "supports_diff_stat_only": {"git_diff"},
        "supports_project_manifest": {"create_workspace"},
        "supports_read_process_output": {"read_process_output"},
        "supports_view_image": {"view_image"},
        "supports_pty": {"run_command"},
        "supports_process_input": {"write_process_input"},
        "supports_process_signal": {"send_process_signal"},
        "supports_terminal_resize": {"resize_terminal"},
        "supports_artifact_registry": {"list_artifacts"},
        "supports_artifact_discovery": {"list_artifacts"},
        "supports_workspace_plan": {
            "get_workspace_plan",
            "update_workspace_plan",
            "update_workspace_plan_step",
        },
        "supports_code_intelligence": {
            "list_symbols",
            "find_definition",
            "find_references",
            "find_implementations",
            "get_call_hierarchy",
            "get_diagnostics",
            "get_changed_symbols",
        },
        "supports_event_stream": {"get_events", "subscribe_process"},
        "supports_event_long_poll": {"get_events", "subscribe_process"},
        "supports_change_sets": {
            "begin_change_set",
            "stage_patch",
            "stage_replace",
            "validate_change_set",
            "commit_change_set",
            "rollback_change_set",
            "get_change_set",
        },
    }
    caps = {name: required <= tool_names for name, required in requirements.items()}
    caps["supports_idempotency"] = any(
        spec.idempotency is not IdempotencyPolicy.NONE for spec in specs
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "server_version": SERVER_VERSION,
        "server_name": "gpt-local-code-operator",
        "capabilities": caps,
        "registered_tool_count": len(specs),
        "tools": tool_contracts(),
        "limits": {
            "max_read_chars": int(files_cfg.get("max_read_chars", 100000)),
            "max_output_chars": int(proc_cfg.get("max_output_chars", 200000)),
            "max_timeout_seconds": int(proc_cfg.get("max_timeout_seconds", 3600)),
            "max_active_workspaces_per_project": int(ws_cfg.get("max_active_per_project", 8)),
            "max_concurrent_jobs": int(proc_cfg.get("max_running_jobs", 3)),
            "max_artifact_discovery_files": int(
                artifact_cfg.get("max_discovery_files", 100)
            ),
            "event_retention_days": int(event_cfg["retention_days"]),
            "max_events_per_workspace": int(event_cfg["max_events_per_workspace"]),
            "max_event_page_size": int(event_cfg["max_page_size"]),
            "max_event_wait_seconds": int(event_cfg["max_wait_seconds"]),
            "max_event_waiters": int(event_cfg["max_waiters"]),
        },
        "change_set_limits": {
            "max_operations": int(change_set_cfg["max_operations"]),
            "max_changed_files": int(change_set_cfg["max_changed_files"]),
            "max_staging_bytes": int(change_set_cfg["max_staging_bytes"]),
            "ttl_hours": int(change_set_cfg["ttl_hours"]),
        },
        "change_set_file_types": ["utf8_regular_file"],
        "supports_change_set_validators": False,
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
