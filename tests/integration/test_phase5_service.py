"""Live-process integration coverage for the Phase 5 supervisor."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from app.config import load_operator_config
from app.services.service_state import ServiceStateStore
from app.services.supervisor import Supervisor


class FixtureSupervisor(Supervisor):
    def _tunnel_doctor(self) -> bool:
        return True

    def _tunnel_is_healthy(self) -> bool:
        return self.tunnel.process is not None and self.tunnel.process.poll() is None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


def _make_supervisor(tmp_path: Path) -> FixtureSupervisor:
    port = _free_port()
    config = load_operator_config()
    config["server"] = {"host": "127.0.0.1", "port": port, "mcp_path": "/mcp"}
    config["proxy"] = {
        "enabled": False,
        "url": "http://127.0.0.1:1",
        "wait_for_proxy_seconds": 1,
        "no_proxy": ["127.0.0.1"],
    }
    config["service"] = {
        "task_name": "phase5-test",
        "tunnel_enabled": True,
        "tunnel_profile": "fixture",
        "tunnel_health_url": "http://127.0.0.1:1/readyz",
        "poll_interval_seconds": 0.05,
        "heartbeat_interval_seconds": 0.05,
        "startup_timeout_seconds": 5,
        "shutdown_timeout_seconds": 2,
        "restart": {
            "initial_delay_seconds": 0.1,
            "multiplier": 2,
            "max_delay_seconds": 0.25,
            "stable_reset_seconds": 30,
        },
        "cleanup": {
            "reconcile_on_start": False,
            "report_expired_workspaces": False,
            "auto_discard_clean_expired": False,
        },
    }
    server_code = (
        "from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer;"
        "H=type('H',(BaseHTTPRequestHandler,),{"
        "'do_GET':lambda s:(s.send_response(200),s.end_headers(),s.wfile.write(b'ok')),"
        "'log_message':lambda *a:None});"
        f"ThreadingHTTPServer(('127.0.0.1',{port}),H).serve_forever()"
    )
    sleeper_code = "import time; print('tunnel-ready', flush=True); time.sleep(60)"
    return FixtureSupervisor(
        base_dir=tmp_path,
        config=config,
        mcp_command=[sys.executable, "-u", "-c", server_code],
        tunnel_command=[sys.executable, "-u", "-c", sleeper_code],
        reconciler=lambda: {"interrupted_processes": 0, "workspaces": []},
    )


def test_supervisor_recovers_children_and_stops_exact_tree(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    store = ServiceStateStore(tmp_path / "data" / "service")

    _wait_for(lambda: (store.read_status() or {}).get("state") == "healthy")
    first_mcp_pid = supervisor.mcp.pid
    first_tunnel_pid = supervisor.tunnel.pid
    assert first_mcp_pid is not None
    assert first_tunnel_pid is not None

    assert supervisor.tunnel.process is not None
    supervisor.tunnel.process.terminate()
    _wait_for(lambda: supervisor.tunnel.restart_count >= 1 and supervisor.tunnel.pid is not None)
    assert supervisor.mcp.pid == first_mcp_pid
    assert supervisor.tunnel.pid != first_tunnel_pid

    replacement_tunnel_pid = supervisor.tunnel.pid
    assert supervisor.mcp.process is not None
    supervisor.mcp.process.terminate()
    _wait_for(lambda: supervisor.mcp.restart_count >= 1 and supervisor.mcp.state == "healthy")
    _wait_for(lambda: supervisor.tunnel.state == "healthy")
    assert supervisor.mcp.pid != first_mcp_pid
    assert supervisor.tunnel.pid != replacement_tunnel_pid

    store.request_stop(0)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert supervisor.mcp.pid is None
    assert supervisor.tunnel.pid is None
    status = store.read_status()
    assert status is not None
    assert status["state"] == "stopped"


def test_delayed_proxy_keeps_mcp_healthy_then_starts_tunnel(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    proxy_ready = threading.Event()
    supervisor.config["proxy"]["enabled"] = True
    supervisor.proxy_cfg["enabled"] = True
    supervisor._proxy_is_ready = proxy_ready.is_set  # type: ignore[method-assign]
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    store = ServiceStateStore(tmp_path / "data" / "service")

    _wait_for(lambda: (store.read_status() or {}).get("state") == "waiting_for_proxy")
    assert supervisor.mcp.state == "healthy"
    assert supervisor.tunnel.pid is None
    proxy_ready.set()
    _wait_for(lambda: (store.read_status() or {}).get("state") == "healthy")
    assert supervisor.tunnel.pid is not None

    store.request_stop(0)
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_mcp_only_mode_becomes_healthy_without_tunnel(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    supervisor.service_cfg["tunnel_enabled"] = False
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    store = ServiceStateStore(tmp_path / "data" / "service")

    _wait_for(lambda: (store.read_status() or {}).get("state") == "healthy")
    assert supervisor.mcp.state == "healthy"
    assert supervisor.tunnel.pid is None

    store.request_stop(0)
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_transient_health_failure_after_stability_does_not_restart_mcp(
    tmp_path: Path,
) -> None:
    supervisor = _make_supervisor(tmp_path)
    supervisor.service_cfg["tunnel_enabled"] = False
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    store = ServiceStateStore(tmp_path / "data" / "service")

    _wait_for(lambda: (store.read_status() or {}).get("state") == "healthy")
    first_pid = supervisor.mcp.pid
    assert first_pid is not None

    original_health = supervisor._mcp_is_healthy
    failed_once = threading.Event()

    def transient_health() -> bool:
        if not failed_once.is_set():
            failed_once.set()
            return False
        return original_health()

    supervisor._mcp_is_healthy = transient_health  # type: ignore[method-assign]
    _wait_for(failed_once.is_set)
    _wait_for(lambda: supervisor.mcp.state == "healthy")

    assert supervisor.mcp.pid == first_pid
    assert supervisor.mcp.restart_count == 0

    store.request_stop(0)
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_foreign_healthy_listener_never_unlocks_tunnel(tmp_path: Path) -> None:
    supervisor = _make_supervisor(tmp_path)
    port = int(supervisor.server_cfg["port"])
    foreign_code = (
        "from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer;"
        "H=type('H',(BaseHTTPRequestHandler,),{"
        "'do_GET':lambda s:(s.send_response(200),s.end_headers()),"
        "'log_message':lambda *a:None});"
        f"ThreadingHTTPServer(('127.0.0.1',{port}),H).serve_forever()"
    )
    foreign = __import__("subprocess").Popen([sys.executable, "-c", foreign_code])
    thread: threading.Thread | None = None
    try:
        _wait_for(lambda: _port_accepts_connections(port))
        thread = threading.Thread(target=supervisor.run)
        thread.start()
        _wait_for(lambda: supervisor.mcp.last_error is not None)
        assert supervisor.tunnel.pid is None
        assert "owned by another process" in supervisor.mcp.last_error
    finally:
        if thread is not None:
            supervisor.store.request_stop(0)
            thread.join(timeout=10)
        foreign.terminate()
        foreign.wait(timeout=5)


def _port_accepts_connections(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False
