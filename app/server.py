"""gpt-local-code-operator MCP Server entry point.

Starts a FastMCP server with Streamable HTTP transport on 127.0.0.1:8765.
"""

import logging
from pathlib import Path

from starlette.responses import JSONResponse

from app.config import (
    get_logging_config,
    get_mcp_path,
    get_server_bind,
    load_operator_config,
)
from app.storage.database import init_db
from app.tools import projects

log = logging.getLogger(__name__)


def create_app() -> "FastMCP":  # noqa: F821 — forward reference for readability
    """Create and configure the FastMCP server instance.

    Initialises the database, registers all tools, and attaches a
    health-check endpoint before returning the app.
    """
    from mcp.server.fastmcp import FastMCP

    # ---- Initialise state store ----
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    init_db(data_dir / "operator.db")

    # ---- Configure logging ----
    log_cfg = get_logging_config()
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO")),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # ---- Read server binding ----
    host, port = get_server_bind()
    mcp_path = get_mcp_path()

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

    # ---- Attach health-check endpoint ----
    # This is used by start-tunnel.ps1 to wait for the server to be ready.
    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):  # noqa: ANN001, ANN201
        return JSONResponse(
            {"status": "ok", "service": "gpt-local-code-operator"}
        )

    # ---- Register MCP tools ----
    projects.register_tools(mcp)

    # Phase 1+ tools will be registered here as they are implemented.
    # from app.tools import workspaces, repo_map, search, reader, git_tools

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