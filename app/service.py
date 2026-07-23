"""Command-line control plane for the user-level background service."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import yaml

from app.config import BASE_DIR, load_operator_config, load_projects_config
from app.services.service_state import ServiceStateStore, process_matches
from app.services.subprocess_utils import no_window_creationflags
from app.services.supervisor import (
    Supervisor,
    _terminate_process_tree,
    format_status,
    load_service_environment,
    reconcile_runtime_state,
)


def _status_with_liveness(store: ServiceStateStore) -> tuple[dict[str, Any], bool]:
    status = store.read_status() or {"state": "not_running"}
    supervisor = status.get("supervisor", {})
    pid = int(supervisor.get("pid") or 0)
    identity = supervisor.get("creation_identity")
    process_running = process_matches(pid, str(identity) if identity is not None else None)
    heartbeat_current = False
    try:
        heartbeat = datetime.fromisoformat(str(status["heartbeat_at"]))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        max_age = max(
            15.0,
            float(load_operator_config()["service"]["heartbeat_interval_seconds"]) * 3,
        )
        heartbeat_current = (datetime.now(timezone.utc) - heartbeat).total_seconds() <= max_age
    except (KeyError, TypeError, ValueError):
        heartbeat_current = False
    running = process_running and heartbeat_current
    status["process_running"] = process_running
    status["heartbeat_current"] = heartbeat_current
    status["running"] = running
    if not running and status.get("state") not in {"stopped", "not_running"}:
        status["observed_state"] = "stale"
    return status, running


def command_run() -> int:
    return Supervisor().run()


def command_status() -> int:
    status, running = _status_with_liveness(ServiceStateStore(BASE_DIR / "data" / "service"))
    print(format_status(status))
    return 0 if running else 1


def command_stop(timeout: float, force: bool) -> int:
    store = ServiceStateStore(BASE_DIR / "data" / "service")
    status, running = _status_with_liveness(store)
    if not running:
        print(json.dumps({"result": "already_stopped"}))
        return 0
    supervisor = status["supervisor"]
    pid = int(supervisor["pid"])
    identity = str(supervisor["creation_identity"])
    store.request_stop(os.getpid())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_matches(pid, identity):
            print(json.dumps({"result": "stopped", "pid": pid}))
            return 0
        time.sleep(0.25)
    if force and process_matches(pid, identity):
        killed = _terminate_process_tree(pid)
        print(json.dumps({"result": "force_stopped" if killed else "force_failed", "pid": pid}))
        return 0 if killed else 1
    print(json.dumps({"result": "timeout", "pid": pid, "timeout_seconds": timeout}))
    return 1


def command_doctor(
    run_tunnel_doctor: bool,
    check_scheduled_task_action: bool = True,
) -> int:
    checks: list[dict[str, Any]] = []

    def add(name: str, state: str, detail: str) -> None:
        checks.append({"name": name, "state": state, "detail": detail})

    try:
        config = load_operator_config()
        load_projects_config()
        add("configuration", "pass", "operator.yaml and projects.yaml are valid")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        add("configuration", "fail", str(exc))
        result = {"result": "failed", "checks": checks}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    add("python", "pass", f"{sys.executable} ({sys.version.split()[0]})")
    if sys.version_info < (3, 12):
        checks[-1] = {"name": "python", "state": "fail", "detail": "Python 3.12+ required"}

    for executable, required in (
        ("pwsh", True),
        ("git", True),
        ("rg", True),
        ("tunnel-client", bool(config["service"]["tunnel_enabled"])),
    ):
        resolved = shutil.which(executable)
        add(
            executable,
            "pass" if resolved else ("fail" if required else "warning"),
            resolved or "not found",
        )

    for directory in (BASE_DIR / "data", BASE_DIR / "logs"):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".doctor-{os.getpid()}.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            add(f"writable:{directory.name}", "pass", str(directory))
        except OSError as exc:
            add(f"writable:{directory.name}", "fail", str(exc))

    gitignore = BASE_DIR / ".gitignore"
    ignored = False
    try:
        git_result = subprocess.run(
            ["git", "check-ignore", "-q", str(BASE_DIR / ".env")],
            cwd=str(BASE_DIR),
            timeout=5,
            check=False,
            creationflags=no_window_creationflags(),
        )
        ignored = git_result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ignored = ".env" in gitignore.read_text(encoding="utf-8", errors="replace")
    add(
        "dotenv_gitignore",
        "pass" if ignored else "fail",
        ".env is ignored" if ignored else ".env is not ignored",
    )

    env, _ = load_service_environment(BASE_DIR)
    key_present = bool(env.get("CONTROL_PLANE_API_KEY"))
    key_required = bool(config["service"]["tunnel_enabled"])
    add(
        "runtime_api_key",
        "pass" if key_present else ("fail" if key_required else "warning"),
        "present (value hidden)" if key_present else "not present",
    )

    status, running = _status_with_liveness(ServiceStateStore(BASE_DIR / "data" / "service"))
    server = config["server"]
    port_available = _port_available(str(server["host"]), int(server["port"]))
    if running:
        add(
            "mcp_port",
            "pass",
            f"owned service is using or preparing {server['host']}:{server['port']}",
        )
    else:
        add(
            "mcp_port",
            "pass" if port_available else "fail",
            "available" if port_available else "occupied by another process",
        )

    proxy = config["proxy"]
    if proxy.get("enabled", True):
        parsed = urlparse(str(proxy["url"]))
        proxy_ready = bool(
            parsed.hostname and parsed.port and _can_connect(parsed.hostname, parsed.port)
        )
        add(
            "proxy",
            "pass" if proxy_ready else "warning",
            "reachable" if proxy_ready else "not currently reachable",
        )
    else:
        add("proxy", "pass", "disabled")

    task_name = str(config["service"]["task_name"])
    try:
        query = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name],
            capture_output=True,
            text=True,
            encoding="mbcs" if os.name == "nt" else "utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=no_window_creationflags(),
        )
        add(
            "scheduled_task",
            "pass" if query.returncode == 0 else "warning",
            "installed" if query.returncode == 0 else "not installed",
        )
        if (
            query.returncode == 0
            and check_scheduled_task_action
            and shutil.which("pwsh")
        ):
            task_env = dict(os.environ)
            task_env["GPT_LOCAL_TASK_NAME"] = task_name
            task_env["GPT_LOCAL_PROJECT_ROOT"] = str(BASE_DIR)
            inspect_script = (
                "$task=Get-ScheduledTask -TaskName $env:GPT_LOCAL_TASK_NAME;"
                "$action=$task.Actions[0];"
                "[pscustomobject]@{"
                "ExecuteMatches=([IO.Path]::GetFileName($action.Execute) -eq 'pythonw.exe');"
                "ArgumentsMatch=($action.Arguments.Trim() -eq '-m app.service run');"
                "WorkingDirectoryMatches=([IO.Path]::GetFullPath($action.WorkingDirectory) "
                "-eq [IO.Path]::GetFullPath($env:GPT_LOCAL_PROJECT_ROOT))}"
                "|ConvertTo-Json -Compress"
            )
            inspected = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", inspect_script],
                env=task_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                creationflags=no_window_creationflags(),
            )
            try:
                action = json.loads(inspected.stdout)
            except json.JSONDecodeError:
                action = {}
            action_valid = bool(
                inspected.returncode == 0
                and action.get("ExecuteMatches") is True
                and action.get("ArgumentsMatch") is True
                and action.get("WorkingDirectoryMatches") is True
            )
            add(
                "scheduled_task_action",
                "pass" if action_valid else "fail",
                "windowless Python host and working directory verified"
                if action_valid
                else (
                    "installed task action does not match this project: "
                    f"exit={inspected.returncode}, checks={action!r}"
                ),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        add("scheduled_task", "warning", str(exc))

    tunnel_owned_healthy = bool(
        running
        and status.get("state") == "healthy"
        and status.get("tunnel", {}).get("state") == "healthy"
        and status.get("tunnel", {}).get("pid")
    )
    if run_tunnel_doctor and tunnel_owned_healthy:
        add(
            "tunnel_doctor",
            "pass",
            "owned tunnel is already healthy; standalone doctor would conflict "
            "with its health-listener port",
        )
    elif (
        run_tunnel_doctor and config["service"]["tunnel_enabled"] and shutil.which("tunnel-client")
    ):
        profile = str(config["service"]["tunnel_profile"])
        try:
            tunnel_result = subprocess.run(
                ["tunnel-client", "doctor", "--profile", profile, "--explain"],
                cwd=str(BASE_DIR),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(config["service"]["startup_timeout_seconds"]),
                check=False,
                creationflags=no_window_creationflags(),
            )
            detail = (tunnel_result.stdout + "\n" + tunnel_result.stderr).strip()
            key = env.get("CONTROL_PLANE_API_KEY", "")
            if key:
                detail = detail.replace(key, "[REDACTED]")
            expected_while_stopped = (
                not running
                and tunnel_result.returncode != 0
                and "FAILED_CHECKS mcp_server_reachable,oauth_metadata" in detail
            )
            if tunnel_result.returncode == 0:
                tunnel_state = "pass"
            elif expected_while_stopped:
                tunnel_state = "warning"
                detail += (
                    "\nPreflight note: MCP reachability is expected to fail while the "
                    "background service is stopped. Runtime startup repeats this check."
                )
            else:
                tunnel_state = "fail"
            add("tunnel_doctor", tunnel_state, detail[-4000:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            add("tunnel_doctor", "fail", str(exc))

    failures = [item for item in checks if item["state"] == "fail"]
    output = {
        "result": "passed" if not failures else "failed",
        "service_running": running,
        "service_state": status.get("state", "not_running"),
        "checks": checks,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def _port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True
    except OSError:
        return False


def _can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="run the foreground supervisor host")
    subparsers.add_parser("status", help="print service status as JSON")
    stop = subparsers.add_parser("stop", help="request a controlled shutdown")
    stop.add_argument("--timeout", type=float, default=20.0)
    stop.add_argument("--force", action="store_true")
    doctor = subparsers.add_parser("doctor", help="run installation and runtime diagnostics")
    doctor.add_argument("--skip-tunnel-doctor", action="store_true")
    doctor.add_argument("--skip-task-action-check", action="store_true", help=argparse.SUPPRESS)
    subparsers.add_parser("reconcile", help="reconcile persisted runtime state")
    subparsers.add_parser("task-name", help="print the configured scheduled-task name")
    subparsers.add_parser("runtime-config", help="print non-secret lifecycle configuration")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return command_run()
    if args.command == "status":
        return command_status()
    if args.command == "stop":
        return command_stop(float(args.timeout), bool(args.force))
    if args.command == "doctor":
        return command_doctor(
            not bool(args.skip_tunnel_doctor),
            not bool(args.skip_task_action_check),
        )
    if args.command == "reconcile":
        print(json.dumps(reconcile_runtime_state(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "task-name":
        print(load_operator_config()["service"]["task_name"])
        return 0
    if args.command == "runtime-config":
        config = load_operator_config()
        server = config["server"]
        print(
            json.dumps(
                {
                    "proxy": config["proxy"],
                    "health_url": (f"http://{server['host']}:{server['port']}/healthz"),
                    "mcp_url": (
                        f"http://{server['host']}:{server['port']}"
                        f"/{str(server['mcp_path']).strip('/')}"
                    ),
                    "service": {
                        "tunnel_profile": config["service"]["tunnel_profile"],
                        "startup_timeout_seconds": config["service"]["startup_timeout_seconds"],
                    },
                }
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
