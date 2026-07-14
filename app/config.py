"""Configuration loader for gpt-local-code-operator.

Loads projects.yaml and operator.yaml from the config/ directory.
"""

import os
from pathlib import Path
from typing import Any

import yaml

# Base directory is the project root (parent of app/)
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents."""
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    if data is None:
        data = {}
    return data


def load_operator_config(path: Path | None = None) -> dict[str, Any]:
    """Load operator.yaml with defaults for missing keys."""
    path = path or CONFIG_DIR / "operator.yaml"
    cfg = _load_yaml(path)

    # --- server defaults ---
    server = cfg.setdefault("server", {})
    server.setdefault("host", "127.0.0.1")
    server.setdefault("port", 8765)
    server.setdefault("mcp_path", "/mcp")

    # --- proxy defaults ---
    proxy = cfg.setdefault("proxy", {})
    proxy.setdefault("enabled", True)
    proxy.setdefault("url", "http://127.0.0.1:7897")
    proxy.setdefault("wait_for_proxy_seconds", 120)
    proxy.setdefault("no_proxy", ["127.0.0.1", "localhost", "::1"])

    # --- workspace defaults ---
    ws = cfg.setdefault("workspace", {})
    ws.setdefault("ttl_hours", 168)
    ws.setdefault("max_active_per_project", 8)

    # --- process defaults ---
    proc = cfg.setdefault("process", {})
    proc.setdefault("max_running_jobs", 3)
    proc.setdefault("default_timeout_seconds", 600)
    proc.setdefault("max_timeout_seconds", 3600)
    proc.setdefault("max_output_chars", 200000)
    proc.setdefault("output_tail_chars", 50000)

    # --- files defaults ---
    files = cfg.setdefault("files", {})
    files.setdefault("max_read_chars", 100000)
    files.setdefault("deny_paths", [".git", ".env", ".env.local"])

    # --- logging defaults ---
    log = cfg.setdefault("logging", {})
    log.setdefault("level", "INFO")
    log.setdefault("retention_days", 14)

    return cfg


def load_projects_config(path: Path | None = None) -> dict[str, Any]:
    """Load projects.yaml and return the project registry."""
    path = path or CONFIG_DIR / "projects.yaml"
    cfg = _load_yaml(path)
    return cfg.get("projects", {})


def get_project(project_id: str) -> dict[str, Any] | None:
    """Return a single project config by ID, or None if not found."""
    projects = load_projects_config()
    return projects.get(project_id)


def get_server_bind() -> tuple[str, int]:
    """Return (host, port) for the MCP server."""
    cfg = load_operator_config()
    srv = cfg["server"]
    return srv["host"], srv["port"]


def get_mcp_path() -> str:
    """Return the MCP HTTP path (e.g. '/mcp')."""
    cfg = load_operator_config()
    return cfg["server"]["mcp_path"]


def get_proxy_config() -> dict[str, Any]:
    """Return the proxy configuration block."""
    cfg = load_operator_config()
    return cfg["proxy"]


def get_logging_config() -> dict[str, Any]:
    """Return the logging configuration block."""
    cfg = load_operator_config()
    return cfg["logging"]


def get_process_config() -> dict[str, Any]:
    """Return the process configuration block."""
    cfg = load_operator_config()
    return cfg["process"]


def get_files_config() -> dict[str, Any]:
    """Return the file access configuration block."""
    cfg = load_operator_config()
    return cfg["files"]