"""gpt-local-code-operator MCP Server entry point.

Starts a FastMCP server with Streamable HTTP transport on 127.0.0.1:8765.
"""

import logging
from pathlib import Path
from typing import cast

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.config import (
    get_logging_config,
    get_mcp_path,
    get_server_bind,
    load_operator_config,
)
from app.services.process_manager import ProcessManager
from app.services.tool_registry import RegisteredToolMCP
from app.storage import database as db
from app.storage.database import Database
from app.tools import (
    artifacts,
    capabilities,
    checks,
    git_tools,
    patcher,
    plans,
    powershell,
    projects,
    pty_process,
    reader,
    repo_map,
    reports,
    search,
    view_image,
    workspaces,
)

log = logging.getLogger(__name__)


def _normalise_http_path(path: str) -> str:
    """Return a single-slash, route-safe HTTP path."""
    normalised = "/" + path.strip("/")
    return normalised if normalised != "/" else "/"


def _local_http_url(host: str, port: int, path: str) -> str:
    """Build the local HTTP URL advertised in protected-resource metadata."""
    display_host = host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}{path}"


def create_app() -> FastMCP:
    """Create and configure the FastMCP server instance.

    Initialises the database, registers all tools, and attaches a
    health-check endpoint before returning the app.
    """
    # ---- Initialise state store ----
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(data_dir / "operator.db")
    database = Database(data_dir / "operator.db")
    # Tools share an explicitly app-scoped manager; independently constructed
    # managers (notably tests) remain isolated from this singleton.
    ProcessManager.configure_instance(ProcessManager(database=database))
    orphaned = database.interrupt_incomplete_processes()
    if orphaned:
        log.warning("marked %d orphaned process record(s) as interrupted", orphaned)

    # ---- Configure logging ----
    log_cfg = get_logging_config()
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO")),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # ---- Read server binding ----
    host, port = get_server_bind()
    mcp_path = _normalise_http_path(get_mcp_path())

    mcp = FastMCP(
        name="gpt-local-code-operator",
        instructions=(
            "Local Code Operator MCP Server. "
            "Provides code search, file reading, patch application, "
            "PowerShell execution, and Git inspection on registered projects."
        ),
        host=host,
        port=port,
        mount_path=mcp_path,
        log_level=log_cfg.get("level", "INFO"),
    )

    # ---- OAuth protected-resource discovery ----
    # This server intentionally has no OAuth provider.  Returning valid
    # protected-resource metadata with no authorization_servers lets
    # tunnel-client distinguish a no-auth MCP server from a broken/missing
    # metadata endpoint without inventing a local authorization server.
    mcp_url = _local_http_url(host, port, mcp_path)
    protected_resource_metadata = {
        "resource": mcp_url,
        "authorization_servers": [],
    }
    protected_resource_path = f"/.well-known/oauth-protected-resource{mcp_path}"

    @mcp.custom_route(
        protected_resource_path,
        methods=["GET"],
        include_in_schema=False,
    )  # type: ignore[untyped-decorator]
    async def protected_resource_metadata_route(request: object) -> JSONResponse:
        return JSONResponse(protected_resource_metadata)

    # Keep the path-independent RFC 9728 candidate available for clients that
    # do not include the MCP endpoint path in their discovery URL.
    root_protected_resource_path = "/.well-known/oauth-protected-resource"
    if protected_resource_path != root_protected_resource_path:

        @mcp.custom_route(
            root_protected_resource_path,
            methods=["GET"],
            include_in_schema=False,
        )  # type: ignore[untyped-decorator]
        async def root_protected_resource_metadata_route(
            request: object,
        ) -> JSONResponse:
            return JSONResponse(protected_resource_metadata)

    # ---- Attach health-check endpoint ----
    # This is used by start-tunnel.ps1 to wait for the server to be ready.
    @mcp.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def healthz(request: object) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "gpt-local-code-operator"})

    # ---- Register MCP tools ----
    # Preserve each public signature while routing all invocations through the
    # central ToolSpec execution middleware.
    registry_mcp = RegisteredToolMCP(mcp)
    tool_mcp = cast(FastMCP, registry_mcp)

    projects.register_tools(tool_mcp)
    capabilities.register_tools(tool_mcp)
    workspaces.register_tools(tool_mcp)
    repo_map.register_tools(tool_mcp)
    search.register_tools(tool_mcp)
    reader.register_tools(tool_mcp)
    git_tools.register_tools(tool_mcp)
    reports.register_tools(tool_mcp)

    # Phase 2: patch application.
    patcher.register_tools(tool_mcp)

    # Phase 3: PowerShell execution.
    powershell.register_tools(tool_mcp)

    # Phase 4: check shortcuts.
    checks.register_tools(tool_mcp)
    # (run_check, list_checks)
    # here as they are implemented.

    # Phase 5: view_image.
    view_image.register_tools(tool_mcp)

    # Phase 5: PTY / interactive process.
    pty_process.register_tools(tool_mcp)

    # Phase 5: artifact registry.
    artifacts.register_tools(tool_mcp)

    # Phase 5: workspace plan.
    plans.register_tools(tool_mcp)

    registry_mcp.validate_coverage()

    return mcp


def main() -> None:
    """Entry point: create the app and run the server."""
    cfg = load_operator_config()
    log.info(
        "Starting gpt-local-code-operator on %s:%s%s",
        cfg["server"]["host"],
        cfg["server"]["port"],
        cfg["server"]["mcp_path"],
    )

    mcp = create_app()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
