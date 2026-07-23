"""Dependency-aware supervisor for the MCP server and Secure MCP Tunnel."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from app.config import BASE_DIR, get_project, load_operator_config
from app.services.logging_config import configure_component_logger
from app.services.process_recovery import recover_processes
from app.services.service_state import (
    ServiceLock,
    ServiceStateStore,
    process_creation_identity,
    utc_now,
)
from app.services.subprocess_utils import no_window_creationflags
from app.services.workspace_manager import discard_workspace
from app.storage import database as db

Clock = Callable[[], float]
Sleeper = Callable[[float], None]
Reconciler = Callable[[], dict[str, Any]]


@dataclass
class ChildRuntime:
    """Mutable state for one supervised child process."""

    name: str
    command: list[str]
    process: subprocess.Popen[str] | None = None
    state: str = "stopped"
    started_monotonic: float | None = None
    unhealthy_since_monotonic: float | None = None
    next_start_monotonic: float = 0.0
    consecutive_failures: int = 0
    restart_count: int = 0
    start_count: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    creation_identity: str | None = None
    pumps: list[threading.Thread] = field(default_factory=list)

    @property
    def pid(self) -> int | None:
        return (
            self.process.pid if self.process is not None and self.process.poll() is None else None
        )


class Supervisor:
    """Own and monitor the local MCP and tunnel child processes."""

    def __init__(
        self,
        *,
        base_dir: Path = BASE_DIR,
        config: dict[str, Any] | None = None,
        mcp_command: list[str] | None = None,
        tunnel_command: list[str] | None = None,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
        reconciler: Reconciler | None = None,
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.config = config or load_operator_config()
        self.service_cfg: dict[str, Any] = self.config["service"]
        self.proxy_cfg: dict[str, Any] = self.config["proxy"]
        self.server_cfg: dict[str, Any] = self.config["server"]
        self.restart_cfg: dict[str, Any] = self.service_cfg["restart"]
        self.cleanup_cfg: dict[str, Any] = self.service_cfg["cleanup"]
        self.clock = clock
        self.sleep = sleeper
        self.reconciler = reconciler or (lambda: reconcile_runtime_state(self.config))
        self.store = ServiceStateStore(self.base_dir / "data" / "service")
        self.lock = ServiceLock(self.store.lock_path)
        self.run_id = secrets.token_hex(8)
        self.started_at = utc_now()
        self._last_heartbeat = 0.0
        self._shutdown = False
        self._top_state = "starting"
        self._recovery: dict[str, Any] = {}
        self.environment, known_secrets = load_service_environment(self.base_dir)
        self.log = configure_component_logger(
            "supervisor", secrets=known_secrets, logs_dir=self.base_dir / "logs"
        )
        self.mcp_log = configure_component_logger(
            "mcp", secrets=known_secrets, logs_dir=self.base_dir / "logs"
        )
        self.tunnel_log = configure_component_logger(
            "tunnel", secrets=known_secrets, logs_dir=self.base_dir / "logs"
        )

        profile = str(self.service_cfg["tunnel_profile"])
        self.mcp = ChildRuntime(
            "mcp",
            mcp_command or [sys.executable, "-m", "app.server"],
        )
        self.tunnel = ChildRuntime(
            "tunnel",
            tunnel_command or ["tunnel-client", "run", "--profile", profile],
        )

    def run(self) -> int:
        """Run until a stop request arrives; return a process exit code."""
        if not self.lock.acquire():
            self.log.warning("event=duplicate_start result=already_running")
            return 2
        try:
            self.store.clear_stop_request()
            self._recovery = self.reconciler()
            self.log.info("event=supervisor_started run_id=%s", self.run_id)
            self._write_status(force=True)
            while not self._shutdown:
                if self.store.stop_requested():
                    self.log.info("event=stop_requested")
                    self._shutdown = True
                    break
                self.tick()
                self._write_status()
                self.sleep(float(self.service_cfg["poll_interval_seconds"]))
            self._shutdown_children()
            self._top_state = "stopped"
            self.store.clear_stop_request()
            self._write_status(force=True)
            self.log.info("event=supervisor_stopped run_id=%s", self.run_id)
            return 0
        except Exception:
            self._top_state = "failed"
            self.log.exception("event=supervisor_failed")
            self._shutdown_children()
            self._write_status(force=True)
            return 1
        finally:
            self.lock.release()

    def tick(self) -> None:
        """Advance child lifecycle state by one monitoring iteration."""
        now = self.clock()
        self._observe_exit(self.mcp, now)
        self._observe_exit(self.tunnel, now)

        if self.mcp.process is None and now >= self.mcp.next_start_monotonic:
            self._start_child(self.mcp)

        mcp_healthy = False
        if self.mcp.process is not None:
            mcp_healthy = self._mcp_is_healthy()
            if mcp_healthy:
                self.mcp.state = "healthy"
                self.mcp.last_error = None
                self.mcp.unhealthy_since_monotonic = None
                self._reset_failures_if_stable(self.mcp, now)
            else:
                self.mcp.state = "starting"
                if self.mcp.unhealthy_since_monotonic is None:
                    self.mcp.unhealthy_since_monotonic = now
                unhealthy_since = self.mcp.unhealthy_since_monotonic
                if now - unhealthy_since >= float(
                    self.service_cfg["startup_timeout_seconds"]
                ):
                    self.mcp.last_error = "health check did not become ready before timeout"
                    self.log.error("event=mcp_startup_timeout pid=%s", self.mcp.pid)
                    self._stop_child(self.mcp)
                    self._schedule_restart(self.mcp, now)

        proxy_ready = self._proxy_is_ready()
        tunnel_enabled = bool(self.service_cfg.get("tunnel_enabled", True))
        dependencies_ready = mcp_healthy and proxy_ready

        if not tunnel_enabled:
            if self.tunnel.process is not None:
                self._stop_child(self.tunnel)
            self.tunnel.state = "stopped"
        elif not dependencies_ready:
            if self.tunnel.process is not None:
                self.log.warning("event=tunnel_dependency_lost")
                self._stop_child(self.tunnel)
            self.tunnel.state = "backoff"
            self.tunnel.last_error = (
                "waiting for MCP health" if not mcp_healthy else "waiting for proxy"
            )
        elif self.tunnel.process is None and now >= self.tunnel.next_start_monotonic:
            if self._tunnel_doctor():
                self._start_child(self.tunnel)
            else:
                self._schedule_restart(self.tunnel, now)

        if self.tunnel.process is not None:
            if self._tunnel_is_healthy():
                self.tunnel.state = "healthy"
                self.tunnel.last_error = None
                self.tunnel.unhealthy_since_monotonic = None
                self._reset_failures_if_stable(self.tunnel, now)
            else:
                self.tunnel.state = "starting"
                if self.tunnel.unhealthy_since_monotonic is None:
                    self.tunnel.unhealthy_since_monotonic = now
                unhealthy_since = self.tunnel.unhealthy_since_monotonic
                if now - unhealthy_since >= float(
                    self.service_cfg["startup_timeout_seconds"]
                ):
                    self.tunnel.last_error = "readiness check did not pass before timeout"
                    self.log.error("event=tunnel_startup_timeout pid=%s", self.tunnel.pid)
                    self._stop_child(self.tunnel)
                    self._schedule_restart(self.tunnel, now)

        if not mcp_healthy:
            self._top_state = "starting_mcp" if self.mcp.process else "degraded"
        elif tunnel_enabled and not proxy_ready:
            self._top_state = "waiting_for_proxy"
        elif tunnel_enabled and self.tunnel.state != "healthy":
            self._top_state = "starting_tunnel" if dependencies_ready else "degraded"
        else:
            self._top_state = "healthy"

    def _start_child(self, child: ChildRuntime) -> None:
        logger = self.mcp_log if child.name == "mcp" else self.tunnel_log
        creation_flags = no_window_creationflags()
        try:
            process = subprocess.Popen(
                child.command,
                cwd=str(self.base_dir),
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as exc:
            child.last_error = f"could not start: {exc}"
            child.state = "failed"
            self.log.error("event=child_start_failed child=%s error=%s", child.name, exc)
            self._schedule_restart(child, self.clock())
            return

        child.process = process
        child.started_monotonic = self.clock()
        child.unhealthy_since_monotonic = child.started_monotonic
        child.start_count += 1
        if child.start_count > 1:
            child.restart_count += 1
        child.state = "starting"
        child.creation_identity = process_creation_identity(process.pid)
        child.last_error = None
        child.pumps = []
        for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is None:
                continue
            pump = threading.Thread(
                target=self._pump_output,
                args=(child.name, stream_name, stream, logger),
                daemon=True,
            )
            pump.start()
            child.pumps.append(pump)
        self.log.info(
            "event=child_started child=%s pid=%s start_count=%s",
            child.name,
            process.pid,
            child.start_count,
        )

    def _pump_output(
        self,
        child_name: str,
        stream_name: str,
        stream: Any,
        logger: Any,
    ) -> None:
        try:
            for line in stream:
                logger.info(
                    "event=child_output child=%s stream=%s %s",
                    child_name,
                    stream_name,
                    line.rstrip(),
                )
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _observe_exit(self, child: ChildRuntime, now: float) -> None:
        process = child.process
        if process is None:
            return
        exit_code = process.poll()
        if exit_code is None:
            return
        child.last_exit_code = exit_code
        child.last_error = f"exited with code {exit_code}"
        child.process = None
        child.creation_identity = None
        child.unhealthy_since_monotonic = None
        self.log.warning("event=child_exited child=%s exit_code=%s", child.name, exit_code)
        if not self._shutdown:
            self._schedule_restart(child, now)

    def _schedule_restart(self, child: ChildRuntime, now: float) -> None:
        child.consecutive_failures += 1
        initial = float(self.restart_cfg["initial_delay_seconds"])
        multiplier = float(self.restart_cfg["multiplier"])
        maximum = float(self.restart_cfg["max_delay_seconds"])
        delay = min(initial * multiplier ** (child.consecutive_failures - 1), maximum)
        child.next_start_monotonic = now + delay
        child.state = "backoff"
        self.log.warning(
            "event=child_backoff child=%s delay_seconds=%s failures=%s",
            child.name,
            delay,
            child.consecutive_failures,
        )

    def _reset_failures_if_stable(self, child: ChildRuntime, now: float) -> None:
        if child.started_monotonic is None:
            return
        if now - child.started_monotonic >= float(self.restart_cfg["stable_reset_seconds"]):
            child.consecutive_failures = 0

    def _stop_child(self, child: ChildRuntime) -> None:
        process = child.process
        if process is None:
            return
        child.state = "stopped"
        try:
            process.terminate()
            process.wait(timeout=min(5.0, float(self.service_cfg["shutdown_timeout_seconds"])))
        except (OSError, subprocess.TimeoutExpired):
            _terminate_process_tree(process.pid)
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        child.last_exit_code = process.poll()
        child.process = None
        child.creation_identity = None
        child.unhealthy_since_monotonic = None
        self.log.info("event=child_stopped child=%s", child.name)

    def _shutdown_children(self) -> None:
        self._top_state = "stopping"
        self._stop_child(self.tunnel)
        self._stop_child(self.mcp)

    def _mcp_is_healthy(self) -> bool:
        process = self.mcp.process
        if process is None or process.poll() is not None:
            return False
        owner_pids = _listening_process_ids(
            str(self.server_cfg["host"]), int(self.server_cfg["port"])
        )
        if owner_pids is not None and process.pid not in owner_pids:
            if owner_pids:
                owners = ", ".join(str(pid) for pid in sorted(owner_pids))
                self.mcp.last_error = (
                    f"MCP port {self.server_cfg['host']}:{self.server_cfg['port']} "
                    f"is owned by another process (PID {owners})"
                )
            return False
        host = str(self.server_cfg["host"])
        if host == "::1":
            host = "[::1]"
        url = f"http://{host}:{int(self.server_cfg['port'])}/healthz"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=1.0) as response:
                return bool(response.status == 200)
        except (OSError, urllib.error.URLError):
            return False

    def _proxy_is_ready(self) -> bool:
        if not self.proxy_cfg.get("enabled", True):
            return True
        parsed = urlparse(str(self.proxy_cfg["url"]))
        if parsed.hostname is None or parsed.port is None:
            return False
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=1.0):
                return True
        except OSError:
            return False

    def _tunnel_doctor(self) -> bool:
        profile = str(self.service_cfg["tunnel_profile"])
        command = ["tunnel-client", "doctor", "--profile", profile, "--explain"]
        try:
            result = subprocess.run(
                command,
                cwd=str(self.base_dir),
                env=self.environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(self.service_cfg["startup_timeout_seconds"]),
                check=False,
                creationflags=no_window_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.tunnel.last_error = f"tunnel doctor could not complete: {exc}"
            self.log.error("event=tunnel_doctor_error error=%s", exc)
            return False
        for line in (result.stdout + "\n" + result.stderr).splitlines():
            if line.strip():
                self.tunnel_log.info("event=doctor_output %s", line)
        if result.returncode != 0:
            self.tunnel.last_error = f"tunnel doctor failed with code {result.returncode}"
            self.log.error("event=tunnel_doctor_failed exit_code=%s", result.returncode)
            return False
        return True

    def _tunnel_is_healthy(self) -> bool:
        url = str(self.service_cfg["tunnel_health_url"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=1.0) as response:
                return bool(response.status == 200)
        except (OSError, urllib.error.URLError):
            return False

    def _write_status(self, *, force: bool = False) -> None:
        now = self.clock()
        heartbeat_interval = float(self.service_cfg["heartbeat_interval_seconds"])
        if not force and now - self._last_heartbeat < heartbeat_interval:
            return
        self._last_heartbeat = now
        identity = process_creation_identity(os.getpid())
        payload: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "state": self._top_state,
            "started_at": self.started_at,
            "heartbeat_at": utc_now(),
            "supervisor": {
                "pid": os.getpid(),
                "creation_identity": identity,
            },
            "mcp": self._child_status(self.mcp),
            "tunnel": self._child_status(self.tunnel),
            "proxy": {
                "enabled": bool(self.proxy_cfg.get("enabled", True)),
                "url": str(self.proxy_cfg.get("url", "")),
                "ready": self._proxy_is_ready(),
            },
            "endpoint": {
                "health": self._health_url(),
                "mcp": self._mcp_url(),
            },
            "config_path": str(self.base_dir / "config" / "operator.yaml"),
            "logs": {
                "supervisor": str(self.base_dir / "logs" / "supervisor.log"),
                "mcp": str(self.base_dir / "logs" / "mcp.log"),
                "tunnel": str(self.base_dir / "logs" / "tunnel.log"),
            },
            "recovery": self._recovery,
        }
        self.store.write_status(payload)

    def _child_status(self, child: ChildRuntime) -> dict[str, Any]:
        next_retry = max(0.0, child.next_start_monotonic - self.clock())
        return {
            "state": child.state,
            "pid": child.pid,
            "creation_identity": child.creation_identity if child.pid else None,
            "restart_count": child.restart_count,
            "consecutive_failures": child.consecutive_failures,
            "last_exit_code": child.last_exit_code,
            "last_error": child.last_error,
            "next_retry_seconds": round(next_retry, 3) if next_retry else 0,
            **(
                {
                    "listener_pids": sorted(
                        _listening_process_ids(
                            str(self.server_cfg["host"]), int(self.server_cfg["port"])
                        )
                        or set()
                    )
                }
                if child.name == "mcp"
                else {}
            ),
        }

    def _health_url(self) -> str:
        return f"http://{self.server_cfg['host']}:{self.server_cfg['port']}/healthz"

    def _mcp_url(self) -> str:
        return (
            f"http://{self.server_cfg['host']}:{self.server_cfg['port']}"
            f"/{str(self.server_cfg['mcp_path']).strip('/')}"
        )


def load_service_environment(base_dir: Path) -> tuple[dict[str, str], list[str]]:
    """Build child environment, loading the tunnel key from the local dotenv file."""
    env = dict(os.environ)
    dotenv = base_dir / ".env"
    if dotenv.is_file():
        try:
            for raw_line in dotenv.read_text(encoding="utf-8-sig").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.removeprefix("export ").split("=", 1)
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                env.setdefault(key, value)
        except OSError:
            pass

    cfg = load_operator_config()
    proxy = cfg["proxy"]
    if proxy.get("enabled", True):
        proxy_url = str(proxy["url"])
        no_proxy = ",".join(str(item) for item in proxy["no_proxy"])
        env["CONTROL_PLANE_HTTP_PROXY"] = proxy_url
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[key] = proxy_url
        env["NO_PROXY"] = no_proxy
    secrets_found = [env.get("CONTROL_PLANE_API_KEY", "")]
    return env, [value for value in secrets_found if value]


def reconcile_runtime_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reconcile persisted process/workspace state without re-running commands."""
    cfg = config or load_operator_config()
    db.init_db(BASE_DIR / "data" / "operator.db")
    incomplete = db.list_incomplete_processes()
    interrupted_workspaces = {str(item["workspace_id"]) for item in incomplete}
    process_recovery = recover_processes(db)
    interrupted = int(process_recovery["interrupted"])
    workspace_results: list[dict[str, Any]] = []
    cleanup = cfg["service"]["cleanup"]
    ttl = timedelta(hours=float(cfg["workspace"]["ttl_hours"]))
    now = datetime.now(timezone.utc)

    if cleanup.get("reconcile_on_start", True):
        for record in db.list_workspaces():
            path = Path(str(record["worktree_path"]))
            project = get_project(str(record["project_id"]))
            result: dict[str, Any] = {
                "workspace_id": record["workspace_id"],
                "path_exists": path.is_dir(),
                "project_registered": project is not None,
                "expired": False,
                "action": "retained",
            }
            try:
                last_accessed = datetime.fromisoformat(str(record["last_accessed_at"]))
                if last_accessed.tzinfo is None:
                    last_accessed = last_accessed.replace(tzinfo=timezone.utc)
                result["expired"] = now - last_accessed > ttl
            except ValueError:
                result["invalid_last_accessed_at"] = True

            if project is not None:
                root = Path(str(project["worktree_root"])).expanduser().resolve()
                try:
                    path.resolve().relative_to(root)
                    result["within_configured_root"] = True
                except ValueError:
                    result["within_configured_root"] = False
                result["git_registered"] = _is_registered_worktree(
                    Path(str(project["repository"])), path
                )
            else:
                result["within_configured_root"] = False
                result["git_registered"] = False

            eligible = (
                bool(result["expired"])
                and bool(result["path_exists"])
                and bool(result["project_registered"])
                and bool(result["within_configured_root"])
                and bool(result["git_registered"])
                and str(record["workspace_id"]) not in interrupted_workspaces
                and _worktree_is_clean(path)
            )
            result["had_interrupted_process"] = (
                str(record["workspace_id"]) in interrupted_workspaces
            )
            result["auto_cleanup_eligible"] = eligible
            if cleanup.get("auto_discard_clean_expired", False) and eligible:
                outcome = discard_workspace(str(record["workspace_id"]))
                result["action"] = "discarded" if outcome["database_record_removed"] else "error"
                result["cleanup_result"] = outcome
            workspace_results.append(result)

    return {
        "interrupted_processes": interrupted,
        "process_recovery": process_recovery,
        "workspaces": workspace_results,
    }


def _is_registered_worktree(repository: Path, worktree: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=no_window_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    registered = {
        Path(line[9:]).resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    }
    return worktree.resolve() in registered


def _worktree_is_clean(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=no_window_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(result.returncode == 0 and not result.stdout.strip())


def _terminate_process_tree(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=no_window_creationflags(),
            check=False,
        )
        return result.returncode == 0
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        return False


def _listening_process_ids(host: str, port: int) -> set[int] | None:
    """Return listener owners where the OS exposes them, or None if unsupported."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=no_window_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    expected_hosts = {host}
    if host in {"localhost", "127.0.0.1"}:
        expected_hosts.update({"127.0.0.1", "0.0.0.0"})
    elif host == "::1":
        expected_hosts.update({"::1", "[::1]", "[::]"})

    owners: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3] != "LISTENING":
            continue
        local = fields[1]
        local_host, separator, local_port = local.rpartition(":")
        if not separator or local_port != str(port):
            continue
        if local_host not in expected_hosts:
            continue
        try:
            owners.add(int(fields[4]))
        except ValueError:
            continue
    return owners


def format_status(status: dict[str, Any] | None) -> str:
    """Return stable JSON suitable for command-line status output."""
    return json.dumps(status or {"state": "not_running"}, ensure_ascii=False, indent=2)
