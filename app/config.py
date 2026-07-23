"""Configuration loader for gpt-local-code-operator.

Loads projects.yaml and operator.yaml from the config/ directory.
"""

from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

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
    proc.setdefault("max_running_jobs_per_workspace", 2)
    proc.setdefault("queue_timeout_seconds", 0.0)
    proc.setdefault("heartbeat_interval_seconds", 2.0)
    proc.setdefault("default_timeout_seconds", 600)
    proc.setdefault("max_timeout_seconds", 3600)
    proc.setdefault("max_output_chars", 200000)
    proc.setdefault("output_tail_chars", 50000)
    proc.setdefault("cpu_time_seconds", 0)
    proc.setdefault("memory_bytes", 0)
    proc.setdefault("max_processes", 0)
    proc.setdefault("max_output_bytes", 20_971_520)
    proc.setdefault("max_disk_bytes", 0)

    # --- files defaults ---
    files = cfg.setdefault("files", {})
    files.setdefault("max_read_chars", 100000)
    files.setdefault("deny_paths", [".git", ".env", ".env.local"])

    # --- artifact defaults ---
    artifacts = cfg.setdefault("artifacts", {})
    artifacts.setdefault("retention_days", 7)
    artifacts.setdefault("cleanup_on_workspace_discard", True)
    artifacts.setdefault("max_discovery_files", 100)

    # --- durable event stream defaults ---
    events = cfg.setdefault("events", {})
    events.setdefault("enabled", True)
    events.setdefault("retention_days", 7)
    events.setdefault("max_events_per_workspace", 50_000)
    events.setdefault("max_payload_bytes", 16_384)
    events.setdefault("max_page_size", 500)
    events.setdefault("max_wait_seconds", 25.0)
    events.setdefault("max_waiters", 32)
    events.setdefault("output_coalesce_ms", 100)

    # --- logging defaults ---
    log = cfg.setdefault("logging", {})
    log.setdefault("level", "INFO")
    log.setdefault("retention_days", 14)
    log.setdefault("max_file_bytes", 10_485_760)
    log.setdefault("backup_count", 5)

    # --- background service defaults ---
    service = cfg.setdefault("service", {})
    service.setdefault("task_name", "gpt-local-code-operator")
    service.setdefault("tunnel_enabled", True)
    service.setdefault("tunnel_profile", "local-code-operator")
    service.setdefault("tunnel_health_url", "http://127.0.0.1:8080/readyz")
    service.setdefault("poll_interval_seconds", 2.0)
    service.setdefault("heartbeat_interval_seconds", 5.0)
    service.setdefault("startup_timeout_seconds", 120.0)
    service.setdefault("shutdown_timeout_seconds", 20.0)
    restart = service.setdefault("restart", {})
    restart.setdefault("initial_delay_seconds", 2.0)
    restart.setdefault("multiplier", 2.0)
    restart.setdefault("max_delay_seconds", 60.0)
    restart.setdefault("stable_reset_seconds", 300.0)
    cleanup = service.setdefault("cleanup", {})
    cleanup.setdefault("reconcile_on_start", True)
    cleanup.setdefault("report_expired_workspaces", True)
    cleanup.setdefault("auto_discard_clean_expired", False)

    _validate_operator_config(cfg)

    return cfg


def _validate_operator_config(cfg: dict[str, Any]) -> None:
    """Reject unsafe or nonsensical operator settings."""
    host = str(cfg["server"]["host"]).strip().lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("server.host must be a loopback address")
    port = int(cfg["server"]["port"])
    if not 1 <= port <= 65535:
        raise ValueError("server.port must be between 1 and 65535")

    proxy = cfg["proxy"]
    if proxy.get("enabled", True):
        parsed = urlparse(str(proxy.get("url", "")))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError("proxy.url must be an HTTP(S) URL with an explicit port")

    process = cfg["process"]
    global_limit = int(process["max_running_jobs"])
    workspace_limit = int(process["max_running_jobs_per_workspace"])
    if global_limit < 1:
        raise ValueError("process.max_running_jobs must be at least 1")
    if not 1 <= workspace_limit <= global_limit:
        raise ValueError(
            "process.max_running_jobs_per_workspace must be between 1 and the global limit"
        )
    if float(process["queue_timeout_seconds"]) < 0:
        raise ValueError("process.queue_timeout_seconds must not be negative")
    if float(process["heartbeat_interval_seconds"]) <= 0:
        raise ValueError("process.heartbeat_interval_seconds must be positive")
    if int(process["default_timeout_seconds"]) <= 0:
        raise ValueError("process.default_timeout_seconds must be positive")
    if int(process["max_timeout_seconds"]) < int(process["default_timeout_seconds"]):
        raise ValueError("process.max_timeout_seconds must not be below the default timeout")
    for key in (
        "max_output_chars",
        "output_tail_chars",
        "max_output_bytes",
    ):
        if int(process[key]) <= 0:
            raise ValueError(f"process.{key} must be positive")
    for key in ("cpu_time_seconds", "memory_bytes", "max_processes", "max_disk_bytes"):
        if int(process[key]) < 0:
            raise ValueError(f"process.{key} must not be negative")

    events = cfg["events"]
    for key in (
        "retention_days",
        "max_events_per_workspace",
        "max_payload_bytes",
        "max_page_size",
        "max_waiters",
        "output_coalesce_ms",
    ):
        if int(events[key]) < (0 if key == "retention_days" else 1):
            raise ValueError(f"events.{key} is below its minimum")
    if float(events["max_wait_seconds"]) <= 0:
        raise ValueError("events.max_wait_seconds must be positive")

    service = cfg["service"]
    for key in (
        "poll_interval_seconds",
        "heartbeat_interval_seconds",
        "startup_timeout_seconds",
        "shutdown_timeout_seconds",
    ):
        if float(service[key]) <= 0:
            raise ValueError(f"service.{key} must be positive")
    if service.get("tunnel_enabled", True) and not str(service["tunnel_profile"]).strip():
        raise ValueError("service.tunnel_profile is required when the tunnel is enabled")
    tunnel_health = urlparse(str(service["tunnel_health_url"]))
    if service.get("tunnel_enabled", True) and (
        tunnel_health.scheme != "http"
        or tunnel_health.hostname not in {"127.0.0.1", "localhost", "::1"}
        or tunnel_health.port is None
    ):
        raise ValueError("service.tunnel_health_url must be an explicit loopback HTTP URL")

    restart = service["restart"]
    for key in ("initial_delay_seconds", "multiplier", "max_delay_seconds"):
        if float(restart[key]) <= 0:
            raise ValueError(f"service.restart.{key} must be positive")
    if float(restart["stable_reset_seconds"]) < 0:
        raise ValueError("service.restart.stable_reset_seconds must not be negative")
    if float(restart["max_delay_seconds"]) < float(restart["initial_delay_seconds"]):
        raise ValueError("service.restart.max_delay_seconds must not be below the initial delay")


def load_projects_config(path: Path | None = None) -> dict[str, Any]:
    """Load projects.yaml and return the project registry."""
    path = path or CONFIG_DIR / "projects.yaml"
    cfg = _load_yaml(path)
    return cast(dict[str, Any], cfg.get("projects", {}))


def get_project(project_id: str) -> dict[str, Any] | None:
    """Return a single project config by ID, or None if not found."""
    projects = load_projects_config()
    return projects.get(project_id)


def get_server_bind() -> tuple[str, int]:
    """Return (host, port) for the MCP server."""
    cfg = load_operator_config()
    srv = cfg["server"]
    return str(srv["host"]), int(srv["port"])


def get_mcp_path() -> str:
    """Return the MCP HTTP path (e.g. '/mcp')."""
    cfg = load_operator_config()
    return str(cfg["server"]["mcp_path"])


def get_proxy_config() -> dict[str, Any]:
    """Return the proxy configuration block."""
    cfg = load_operator_config()
    return cast(dict[str, Any], cfg["proxy"])


def get_logging_config() -> dict[str, Any]:
    """Return the logging configuration block."""
    cfg = load_operator_config()
    return cast(dict[str, Any], cfg["logging"])


def get_service_config() -> dict[str, Any]:
    """Return the background-service configuration block."""
    cfg = load_operator_config()
    return cast(dict[str, Any], cfg["service"])


def get_process_config() -> dict[str, Any]:
    """Return the process configuration block."""
    cfg = load_operator_config()
    return cast(dict[str, Any], cfg["process"])


def get_files_config() -> dict[str, Any]:
    """Return the file access configuration block."""
    cfg = load_operator_config()
    return cast(dict[str, Any], cfg["files"])


def get_events_config() -> dict[str, Any]:
    """Return durable event-stream configuration."""
    cfg = load_operator_config()
    return cast(dict[str, Any], cfg["events"])


def get_image_config() -> dict[str, Any]:
    """Return the image configuration block with defaults."""
    cfg = load_operator_config()
    img = cfg.setdefault("image", {})
    img.setdefault("max_file_size_bytes", 20_971_520)
    img.setdefault("max_pixels", 50_000_000)
    img.setdefault("max_dimension", 10_000)
    img.setdefault("max_dimension_high", 2048)
    img.setdefault("supported_formats", ["image/png", "image/jpeg", "image/webp"])
    img.setdefault("deny_svg", True)
    return cast(dict[str, Any], img)


def get_artifact_config() -> dict[str, Any]:
    """Return artifact discovery and cleanup settings."""
    return cast(dict[str, Any], load_operator_config()["artifacts"])
