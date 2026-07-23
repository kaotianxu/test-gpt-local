"""Process manager service.

Manages the lifecycle of subprocesses spawned by MCP tools
(``run_pwsh``, ``run_check``).  Provides:

- Thread-safe subprocess spawning with stdin input
- Timeout watchdog that terminates the process tree
- File-based stdout / stderr capture with size limits
- Concurrent job limits per workspace and globally
- Status tracking in the SQLite database
- PTY/interactive mode via Windows ConPTY
- ``write_input`` and ``send_signal`` for interactive processes
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.config import (
    BASE_DIR,
    get_events_config,
    get_process_config,
    get_proxy_config,
)
from app.services.event_store import TERMINAL_PROCESS_STATUSES, EventStore
from app.services.process_recovery import pid_alive, process_creation_identity
from app.services.process_resources import (
    ProcessResourceController,
    ProcessResourceHandle,
)
from app.services.process_scheduler import (
    ConcurrencyKey,
    ProcessLease,
    ProcessScheduler,
    QueuePolicy,
    QueueTimeoutError,
    ResourceLimits,
)
from app.services.subprocess_utils import no_window_creationflags
from app.storage import database as db
from app.storage.database import Database

log = logging.getLogger(__name__)

# Process ID pattern:  pr-XXXXXXXX  (8 hex chars)
_PROCESS_ID_RE = re.compile(r"^pr-[0-9a-f]{8}$")

# Sentinel for the watchdog "process already cleaned up" case.
_PROCESS_GONE = -1

# Default PowerShell prefix injected before every script so that common
# interactive nuisances are suppressed.
_PWSH_PREFIX = (
    "$ProgressPreference = 'SilentlyContinue'\n$PSNativeCommandUseErrorActionPreference = $true\n"
)

# ---------------------------------------------------------------------------
# Windows ConPTY helpers (available on Windows 10 1809+)
# ---------------------------------------------------------------------------

_ConPTY: Any = None  # module-level cache of ctypes ConPTY wrappers


def _get_conpty() -> Any:
    """Lazy-load the ConPTY wrapper module, or None on failure."""
    global _ConPTY
    if _ConPTY is not None:
        return _ConPTY
    try:
        from app.services.conpty import ConPTYWrapper

        _ConPTY = ConPTYWrapper
        return _ConPTY
    except Exception:
        _ConPTY = False
        return None


def _has_conpty() -> bool:
    """Return True if the ConPTY wrapper is available."""
    wrapper = _get_conpty()
    if wrapper is None:
        return False
    try:
        return bool(wrapper.is_available())
    except Exception:
        return False


class _RunningProcess:
    """Internal bookkeeping for a single managed subprocess."""

    def __init__(
        self,
        process_id: str,
        workspace_id: str,
        proc: Any,
        stdout_path: Path,
        stderr_path: Path,
        working_directory: Path,
        deadline: float,
        *,
        pty_handle: Any = None,
        pty_input_handle: Any = None,
        pty_output_handle: Any = None,
        pty_reader_thread: threading.Thread | None = None,
        stdin_pipe: Any = None,
        lease: ProcessLease | None = None,
        creation_identity: str | None = None,
        resource_handle: ProcessResourceHandle | None = None,
        tool_name: str = "run_pwsh",
        request_id: str | None = None,
    ) -> None:
        self.process_id = process_id
        self.workspace_id = workspace_id
        self.proc = proc
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.working_directory = working_directory
        self.deadline = deadline
        self.pty_handle = pty_handle
        self.pty_input_handle = pty_input_handle
        self.pty_output_handle = pty_output_handle
        self.pty_reader_thread = pty_reader_thread
        self.stdin_pipe = stdin_pipe  # subprocess stdin pipe (non-PTY mode)
        self.lease = lease
        self.creation_identity = creation_identity
        self.resource_handle = resource_handle
        self.tool_name = tool_name
        self.request_id = request_id
        self.output_offsets = {"stdout": 0, "stderr": 0}
        self._lock = threading.RLock()
        self._completed = threading.Event()
        self._finalized = False

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the process finishes, or until *timeout* seconds.

        Returns ``True`` if the process exited, ``False`` on timeout.
        """
        if self._completed.is_set():
            return True
        try:
            return self.proc.wait(timeout=timeout) is not None
        except subprocess.TimeoutExpired:
            return False

    def claim_completion(self) -> bool:
        """Claim exclusive ownership of terminal-state cleanup."""
        with self._lock:
            if self._finalized:
                return False
            self._finalized = True
            return True

    def mark_done(self) -> None:
        """Mark process completion for waiters."""
        self._completed.set()

    def wait_done(self, timeout: float | None = None) -> bool:
        """Wait for lifecycle finalization rather than polling the child."""
        return self._completed.wait(timeout)

    def write_stdin(self, text: str, append_newline: bool = True) -> None:
        """Write *text* to the process's stdin (PTY or pipe)."""
        with self._lock:
            if append_newline and not text.endswith(("\n", "\r")):
                text += "\r\n" if self.pty_input_handle is not None else "\n"

            # PTY mode: write to the PTY input pipe.
            if self.pty_input_handle is not None:
                from app.services.conpty import ConPTYWrapper

                ConPTYWrapper.write_input(self.pty_input_handle, text)
                return

            # Pipe mode: write to the subprocess stdin.
            if self.stdin_pipe is not None and not self.stdin_pipe.closed:
                self.stdin_pipe.write(text)
                self.stdin_pipe.flush()
                return

            raise RuntimeError("process stdin is not available")

    def close_stdin(self) -> None:
        """Close the stdin pipe (sends EOF to the process)."""
        with self._lock:
            if self.pty_input_handle is not None:
                from app.services.conpty import ConPTYWrapper

                # In a Windows terminal, EOF is Ctrl+Z followed by Enter.
                # Closing ConPTY's host pipe raises CTRL_CLOSE_EVENT and
                # produces exit code 0xC000013A instead of a clean EOF.
                ConPTYWrapper.write_input(self.pty_input_handle, "\x1a\r\n")
                return

            if self.stdin_pipe is not None and not self.stdin_pipe.closed:
                self.stdin_pipe.close()
                self.stdin_pipe = None

    def send_interrupt(self) -> None:
        """Send Ctrl+C to the process.

        In PTY mode, this writes the Ctrl+C byte sequence.
        In pipe mode, this falls back to GenerateConsoleCtrlEvent.
        """
        with self._lock:
            if self.pty_input_handle is not None:
                from app.services.conpty import ConPTYWrapper

                ConPTYWrapper.send_interrupt(self.pty_input_handle)
                return
            if self.pty_handle is not None:
                from app.services.conpty import ConPTYWrapper

                ConPTYWrapper.send_interrupt(self.pty_handle)
                return

        # Pipe mode: send Ctrl+C via the Windows console event.
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if not kernel32.GenerateConsoleCtrlEvent(0, 0):
                log.warning("GenerateConsoleCtrlEvent failed for process %s", self.process_id)
        except Exception:
            # POSIX fallback is useful for development and test runners.
            try:
                os.kill(self.proc.pid, signal.SIGINT)
            except (OSError, AttributeError):
                log.warning("send_interrupt fallback failed for process %s", self.process_id)

    def release_slot(self) -> bool:
        """Release the scheduler lease exactly once."""
        with self._lock:
            lease = self.lease
            self.lease = None
        return lease.release() if lease is not None else False


class ProcessManager:
    """Thread-safe subprocess lifecycle manager.

    Production callers may use :meth:`get_instance`; tests and app factories
    can construct an isolated manager by injecting a :class:`Database`.
    """

    _instance: ProcessManager | None = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        database: Database | None = None,
        config: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        processes_dir: Path | None = None,
        scheduler: ProcessScheduler | None = None,
        resource_controller: ProcessResourceController | None = None,
        event_store: EventStore | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._database: Any = database if database is not None else db
        self._clock = clock
        # process_id -> _RunningProcess
        self._running: dict[str, _RunningProcess] = {}
        self._processes_dir = processes_dir or BASE_DIR / "data" / "processes"
        self._processes_dir.mkdir(parents=True, exist_ok=True)

        cfg = config if config is not None else get_process_config()
        self._max_running = int(cfg.get("max_running_jobs", 3))
        self._max_output_chars = int(cfg.get("max_output_chars", 200000))
        self._default_timeout = int(cfg.get("default_timeout_seconds", 600))
        self._max_timeout = int(cfg.get("max_timeout_seconds", 3600))
        self._register_artifacts = bool(cfg.get("register_artifacts", True))
        per_workspace = int(cfg.get("max_running_jobs_per_workspace", self._max_running))
        per_workspace = max(1, min(per_workspace, self._max_running))
        self._queue_timeout = float(cfg.get("queue_timeout_seconds", 0.0))
        self._heartbeat_interval = max(
            0.1, float(cfg.get("heartbeat_interval_seconds", 2.0))
        )
        self._scheduler = scheduler or ProcessScheduler(
            QueuePolicy(
                global_limit=self._max_running,
                per_workspace_limit=per_workspace,
                queue_timeout_seconds=self._queue_timeout,
            ),
            clock=clock,
        )
        self._resource_limits = ResourceLimits(
            cpu_time_seconds=_optional_positive_int(cfg.get("cpu_time_seconds")),
            memory_bytes=_optional_positive_int(cfg.get("memory_bytes")),
            max_processes=_optional_positive_int(cfg.get("max_processes")),
            max_output_bytes=_optional_positive_int(
                cfg.get("max_output_bytes", self._max_output_chars * 4)
            ),
            max_disk_bytes=_optional_positive_int(cfg.get("max_disk_bytes")),
        )
        self._resource_controller = resource_controller or ProcessResourceController()
        event_cfg = get_events_config()
        self._events_enabled = bool(event_cfg.get("enabled", True)) and (
            event_store is not None or callable(getattr(self._database, "connect", None))
        )
        self._event_store = event_store or EventStore(
            self._database,
            max_payload_bytes=int(event_cfg["max_payload_bytes"]),
            max_page_size=int(event_cfg["max_page_size"]),
            max_wait_seconds=float(event_cfg["max_wait_seconds"]),
            max_waiters=int(event_cfg["max_waiters"]),
        )
        self._shutting_down = False

    @property
    def event_store(self) -> EventStore:
        """Return the app-scoped durable event store."""
        return self._event_store

    # ---- Public API --------------------------------------------------------

    @classmethod
    def get_instance(cls) -> ProcessManager:
        """Return the shared process manager instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def configure_instance(cls, manager: ProcessManager) -> None:
        """Install the app-scoped manager before tools begin serving calls."""
        with cls._instance_lock:
            cls._instance = manager

    def get_record(self, process_id: str) -> dict[str, Any] | None:
        """Return the process record from this manager's state store."""
        return cast(dict[str, Any] | None, self._database.get_process(process_id))

    def spawn(
        self,
        workspace_id: str,
        worktree_path: Path,
        script: str,
        *,
        pwsh_path: str = "pwsh.exe",
        working_directory: str | None = None,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        tool_name: str = "run_pwsh",
        concurrency_write: bool = True,
        priority: int = 0,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a PowerShell script via stdin and return the process result.

        When *wait* is ``True`` the call blocks until the process finishes
        (or times out).  When ``False`` it returns immediately with the
        process in ``running`` state and the caller must use
        :meth:`get_result` to poll for completion.

        Returns the same structure as :meth:`get_result`.
        """
        # ---- resource checks ----
        timeout = min(
            timeout_seconds or self._default_timeout,
            self._max_timeout,
        )

        # ---- generate process id ----
        process_id = "pr-" + secrets.token_hex(4)

        # ---- build environment ----
        merged_env = self._build_env(env)

        # ---- resolve working directory ----
        cwd = self._resolve_cwd(worktree_path, working_directory)

        # ---- prepare output files ----
        proc_dir = self._processes_dir / process_id
        proc_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = proc_dir / "stdout.txt"
        stderr_path = proc_dir / "stderr.txt"

        # ---- prepare script ----
        prefixed_script = _PWSH_PREFIX + script
        script_sha256 = hashlib.sha256(prefixed_script.encode("utf-8")).hexdigest()
        script_preview = script[:200]

        # ---- write DB record ----
        self._database.insert_process(
            process_id=process_id,
            workspace_id=workspace_id,
            tool_name=tool_name,
            script_sha256=script_sha256,
            script_preview=script_preview,
            working_directory=str(cwd),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        self._append_event(
            "tool.queued",
            workspace_id=workspace_id,
            process_id=process_id,
            request_id=request_id,
            payload={"tool_name": tool_name, "priority": priority},
        )
        try:
            lease = self._acquire_lease(
                workspace_id,
                write=concurrency_write,
                priority=priority,
            )
        except RuntimeError:
            self._transition_record(
                process_id,
                "failed",
                exit_code=-1,
                reason="queue_timeout",
                recovery_status="queue_timeout",
                request_id=request_id,
            )
            raise

        # ---- spawn subprocess ----
        try:
            stdout_file = open(stdout_path, "w", encoding="utf-8")
            stderr_file = open(stderr_path, "w", encoding="utf-8")
        except OSError as exc:
            self._transition_record(
                process_id, "failed", exit_code=-1, reason="output_open_failed"
            )
            lease.release()
            return {
                "error": f"cannot open output files: {exc}",
                "process_id": process_id,
            }

        try:
            proc = subprocess.Popen(
                [
                    pwsh_path,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "-",
                ],
                cwd=str(cwd),
                env=merged_env,
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=no_window_creationflags(new_process_group=True),
            )
        except FileNotFoundError:
            stdout_file.close()
            stderr_file.close()
            self._transition_record(
                process_id, "failed", exit_code=-1, reason="executable_not_found"
            )
            lease.release()
            return {
                "error": f"pwsh executable not found: {pwsh_path}",
                "process_id": process_id,
            }
        except OSError as exc:
            stdout_file.close()
            stderr_file.close()
            self._transition_record(
                process_id, "failed", exit_code=-1, reason="process_spawn_failed"
            )
            lease.release()
            return {
                "error": f"cannot start pwsh: {exc}",
                "process_id": process_id,
            }

        creation_identity = process_creation_identity(proc.pid)
        resource_handle = self._resource_controller.attach(
            process_id=process_id,
            pid=proc.pid,
            process=proc,
            limits=self._resource_limits,
        )
        self._mark_process_started(
            process_id,
            pid=proc.pid,
            tool_name=tool_name,
            request_id=request_id,
        )
        self._database.update_process_runtime(
            process_id,
            pid=proc.pid,
            process_creation_identity=creation_identity,
            heartbeat=_now_iso(),
            job_object_identity=resource_handle.identity,
            recovery_status=(
                f"resource_limits_unavailable:{resource_handle.error}"
                if resource_handle.error
                else "started"
            ),
        )

        # Write script to stdin and close pipe so pwsh can start processing.
        try:
            if proc.stdin is not None:
                proc.stdin.write(prefixed_script)
                proc.stdin.close()
        except OSError:
            pass  # will be picked up by the watchdog

        deadline = self._clock() + timeout
        rp = _RunningProcess(
            process_id=process_id,
            workspace_id=workspace_id,
            proc=proc,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            working_directory=cwd,
            deadline=deadline,
            lease=lease,
            creation_identity=creation_identity,
            resource_handle=resource_handle,
            tool_name=tool_name,
            request_id=request_id,
        )

        with self._lock:
            self._running[process_id] = rp

        # ---- start watchdog thread ----
        threading.Thread(
            target=self._watchdog,
            args=(rp,),
            daemon=True,
        ).start()

        return {
            "process_id": process_id,
            "status": "running",
            "pid": proc.pid,
            "started_at": _now_iso(),
        }

    def get_result(
        self,
        process_id: str,
        tail_chars: int | None = None,
    ) -> dict[str, Any]:
        """Return the current state of a process.

        If the process is still running, checks whether it has exited
        and transitions the state accordingly.
        """
        rp = self._get_running(process_id)
        if rp is not None:
            self._check_exit(rp)

        record = self._database.get_process(process_id)
        if record is None:
            return {"error": f"process not found: {process_id}", "process_id": process_id}

        stdout_path = Path(record["stdout_path"]) if record.get("stdout_path") else None
        stderr_path = Path(record["stderr_path"]) if record.get("stderr_path") else None

        tail = tail_chars if tail_chars is not None else self._max_output_chars

        return self._build_result(record, stdout_path, stderr_path, tail)

    def wait_for_terminal(
        self,
        process_id: str,
        timeout_seconds: float,
        *,
        tail_chars: int | None = None,
    ) -> dict[str, Any]:
        """Wait for process finalization without fixed-interval status polling."""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        record = self._database.get_process(process_id)
        if record is None:
            return {"error": f"process not found: {process_id}", "process_id": process_id}
        if str(record["status"]) in TERMINAL_PROCESS_STATUSES:
            return self.get_result(process_id, tail_chars=tail_chars)

        running = self._get_running(process_id)
        if running is not None:
            running.wait_done(timeout_seconds)
            return self.get_result(process_id, tail_chars=tail_chars)

        cursor = self._event_store.list_after(
            None,
            workspace_id=str(record["workspace_id"]),
            process_id=process_id,
        )["cursor"]
        self._event_store.wait_after(
            str(cursor),
            workspace_id=str(record["workspace_id"]),
            process_id=process_id,
            event_types=["process.exited"],
            timeout_seconds=timeout_seconds,
        )
        return self.get_result(process_id, tail_chars=tail_chars)

    def cancel(self, process_id: str) -> dict[str, Any]:
        """Terminate a running process and its child process tree."""
        if not _PROCESS_ID_RE.match(process_id):
            return {
                "error": f"invalid process_id format: {process_id!r}",
                "status": "unknown",
                "process_tree_terminated": False,
            }

        rp = self._get_running(process_id)
        if rp is None:
            record = self._database.get_process(process_id)
            if record is None:
                return {
                    "error": f"process not found: {process_id}",
                    "status": "unknown",
                    "process_tree_terminated": False,
                }
            return {
                "status": record["status"],
                "process_id": process_id,
                "process_tree_terminated": False,
                "info": "process is not running",
            }

        if not rp.claim_completion():
            record = self._database.get_process(process_id)
            return {
                "status": record["status"] if record else "unknown",
                "process_id": process_id,
                "process_tree_terminated": False,
                "info": "process completion is already being handled",
            }

        pid = rp.proc.pid
        tree_killed = self._kill_tree(pid)

        if not tree_killed:
            # Fallback: send SIGTERM / terminate
            try:
                rp.proc.terminate()
            except OSError:
                pass
        self._emit_output_events(rp)
        self._transition_record(
            process_id,
            "cancelled",
            reason="cancelled_by_caller",
            request_id=rp.request_id,
            output_offsets=rp.output_offsets,
        )

        rp.mark_done()
        with self._lock:
            self._running.pop(process_id, None)
        rp.release_slot()

        # Clean up PTY handles.
        self._cleanup_pty(rp)
        self._register_process_artifacts(rp)

        return {
            "status": "cancelled",
            "process_id": process_id,
            "process_tree_terminated": tree_killed,
        }

    def cancel_all_for_workspace(self, workspace_id: str) -> None:
        """Cancel all running processes belonging to a workspace."""
        with self._lock:
            to_cancel = [
                pid for pid, rp in self._running.items() if rp.workspace_id == workspace_id
            ]
        for pid in to_cancel:
            self.cancel(pid)

    def shutdown(
        self,
        *,
        grace_seconds: float = 2.0,
        terminate_seconds: float = 2.0,
    ) -> dict[str, int]:
        """Drain managed processes with interrupt, terminate, then force-kill."""
        if grace_seconds < 0 or terminate_seconds < 0:
            raise ValueError("shutdown grace periods must not be negative")

        with self._lock:
            if self._shutting_down:
                return {"interrupted": 0, "terminated": 0, "killed": 0, "total": 0}
            self._shutting_down = True
            processes = [rp for rp in self._running.values() if rp.claim_completion()]

        if not processes:
            self._event_store.shutdown()
            return {"interrupted": 0, "terminated": 0, "killed": 0, "total": 0}

        stages = {rp.process_id: "service_shutdown_interrupt" for rp in processes}
        for rp in processes:
            self._database.update_process_runtime(
                rp.process_id,
                heartbeat=_now_iso(),
                recovery_status="service_shutdown_interrupt_sent",
            )
            try:
                rp.send_interrupt()
            except Exception:
                log.exception("shutdown interrupt failed for %s", rp.process_id)

        remaining = self._wait_for_shutdown(processes, grace_seconds)
        for rp in remaining:
            stages[rp.process_id] = "service_shutdown_terminate"
            try:
                rp.proc.terminate()
            except (OSError, AttributeError):
                pass

        remaining = self._wait_for_shutdown(remaining, terminate_seconds)
        for rp in remaining:
            stages[rp.process_id] = "service_shutdown_kill"
            self._kill_tree(rp.proc.pid)

        for rp in processes:
            exit_code = rp.proc.poll()
            self._emit_output_events(rp)
            self._transition_record(
                rp.process_id,
                "interrupted",
                exit_code=exit_code if exit_code is not None else -1,
                recovery_status=stages[rp.process_id],
                reason=stages[rp.process_id],
                request_id=rp.request_id,
                output_offsets=rp.output_offsets,
            )
            rp.mark_done()
            with self._lock:
                self._running.pop(rp.process_id, None)
            rp.release_slot()
            self._cleanup_pty(rp)
            self._register_process_artifacts(rp)

        self._event_store.shutdown()
        interrupted = sum(
            stage == "service_shutdown_interrupt" for stage in stages.values()
        )
        terminated = sum(
            stage == "service_shutdown_terminate" for stage in stages.values()
        )
        killed = sum(stage == "service_shutdown_kill" for stage in stages.values())
        return {
            "interrupted": interrupted,
            "terminated": terminated,
            "killed": killed,
            "total": len(processes),
        }

    @staticmethod
    def _wait_for_shutdown(
        processes: list[_RunningProcess], timeout_seconds: float
    ) -> list[_RunningProcess]:
        deadline = time.monotonic() + timeout_seconds
        remaining = list(processes)
        while remaining and time.monotonic() < deadline:
            remaining = [rp for rp in remaining if rp.proc.poll() is None]
            if remaining:
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return [rp for rp in remaining if rp.proc.poll() is None]

    def delete_outputs_for_workspace(self, workspace_id: str) -> int:
        """Delete controlled process-output directories for a workspace."""
        removed = 0
        root = self._processes_dir.resolve()
        for process in self._database.list_processes(workspace_id):
            process_id = str(process.get("process_id", ""))
            if not _PROCESS_ID_RE.match(process_id):
                continue
            target = (root / process_id).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if target.is_dir():
                shutil.rmtree(target)
                removed += 1
        return removed

    # ---- PTY / Interactive API ------------------------------------------------

    def spawn_interactive(
        self,
        workspace_id: str,
        worktree_path: Path,
        command: str,
        *,
        shell: str = "pwsh",
        working_directory: str | None = None,
        timeout_seconds: int | None = None,
        env: dict[str, str] | None = None,
        tool_name: str = "run_command",
        tty: bool = False,
        columns: int = 80,
        rows: int = 24,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Spawn an interactive (or PTY) process.

        Unlike ``spawn()``, the stdin pipe is kept open so the caller can
        write input via :meth:`write_input` and send signals via
        :meth:`send_signal`.

        When *tty* is ``True`` and the Windows ConPTY API is available, the
        process is attached to a pseudo console for proper terminal emulation.
        """
        # ---- resource checks ----
        timeout = min(
            timeout_seconds or self._default_timeout,
            self._max_timeout,
        )

        if not 20 <= columns <= 500 or not 5 <= rows <= 200:
            return {"error": "terminal dimensions must be columns 20-500 and rows 5-200"}

        # ---- generate process id ----
        process_id = "pr-" + secrets.token_hex(4)

        # ---- build environment ----
        merged_env = self._build_env(env)
        # Python 3.13's new PyREPL directly queries legacy console handles and
        # currently fails under ConPTY. The basic REPL remains fully
        # interactive and portable through pseudo-console pipes.
        if tty and shell.lower().strip() == "python":
            merged_env.setdefault("PYTHON_BASIC_REPL", "1")

        # ---- resolve working directory ----
        cwd = self._resolve_cwd(worktree_path, working_directory)

        # ---- prepare output files ----
        proc_dir = self._processes_dir / process_id
        proc_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = proc_dir / "stdout.txt"
        stderr_path = proc_dir / "stderr.txt"

        # ---- write DB record ----
        script_preview = command[:200]
        script_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest() if command else ""
        self._database.insert_process(
            process_id=process_id,
            workspace_id=workspace_id,
            tool_name=tool_name,
            script_sha256=script_sha256,
            script_preview=script_preview,
            working_directory=str(cwd),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        self._append_event(
            "tool.queued",
            workspace_id=workspace_id,
            process_id=process_id,
            request_id=request_id,
            payload={"tool_name": tool_name, "priority": 0},
        )
        try:
            lease = self._acquire_lease(workspace_id, write=True, priority=0)
        except RuntimeError:
            self._transition_record(
                process_id,
                "failed",
                exit_code=-1,
                reason="queue_timeout",
                recovery_status="queue_timeout",
                request_id=request_id,
            )
            raise

        # ---- Build the command line ----
        cmd_parts = self._build_command_line(shell, command)

        # ---- Spawn ----
        pty_handle = None
        pty_input_write = None
        pty_output_read = None
        pty_input_read = None
        pty_output_write = None
        pty_reader_thread = None
        stdin_pipe = None
        stdout_file: Any
        stderr_file: Any
        proc: Any
        pid = 0

        use_conpty = tty and _has_conpty()
        if tty and os.name == "nt" and not use_conpty:
            self._transition_record(
                process_id, "failed", exit_code=-1, reason="pty_unavailable"
            )
            lease.release()
            return {
                "error": "ConPTY is unavailable on this Windows host",
                "code": "PTY_UNAVAILABLE",
            }

        if use_conpty:
            # Use ConPTY.
            try:
                conpty_wrapper = _get_conpty()
                pty = conpty_wrapper.create(width=columns, height=rows)
                pty_handle = pty["con_pty_handle"]
                pty_input_write = pty["pty_input_write"]
                pty_output_read = pty["pty_output_read"]
                pty_input_read = pty["pty_input_read"]
                pty_output_write = pty["pty_output_write"]

                # Start the process attached to the pseudo console.
                pi = conpty_wrapper.start_process(
                    pty_handle,
                    command_line=subprocess.list2cmdline(cmd_parts),
                    cwd=str(cwd),
                    env=merged_env,
                )
                conpty_wrapper.close_handle(pty_input_read)
                conpty_wrapper.close_handle(pty_output_write)
                pty_input_read = None
                pty_output_write = None

                # Open the process handle from the PID.
                proc_handle = pi["process_handle"]
                pid = pi["pid"]

                # We need a subprocess.Popen wrapper for the process handle.
                # Since we already have the process handle, we create a
                # minimal Popen-like object.
                proc = _PseudoProcess(pid=pid, handle=proc_handle)

                # Start a background thread to read PTY output into stdout.
                stdout_file = open(stdout_path, "wb")
                stderr_file = open(stderr_path, "w", encoding="utf-8")

                def _pty_reader() -> None:
                    """Read from PTY output and write to stdout file."""
                    try:
                        buf_size = 4096
                        while True:
                            data = conpty_wrapper.read_output(pty_output_read, buf_size)
                            if not data:
                                break
                            stdout_file.write(data)
                            stdout_file.flush()
                    except (OSError, ValueError):
                        pass
                    finally:
                        try:
                            stdout_file.close()
                        except Exception:
                            pass

                pty_reader_thread = threading.Thread(target=_pty_reader, daemon=True)
                pty_reader_thread.start()

            except Exception as exc:
                # Clean up on failure.
                if pty_handle is not None:
                    try:
                        if pty_input_read is not None:
                            conpty_wrapper.close_handle(pty_input_read)
                        if pty_output_write is not None:
                            conpty_wrapper.close_handle(pty_output_write)
                        conpty_wrapper.close(pty_handle, pty_input_write, pty_output_read)
                    except Exception:
                        pass
                self._transition_record(
                    process_id, "failed", exit_code=-1, reason="pty_create_failed"
                )
                lease.release()
                return {
                    "error": f"ConPTY creation failed: {exc}",
                    "code": "PTY_CREATE_FAILED",
                    "process_id": process_id,
                }

        else:
            # Pipe mode: keep stdin open.
            try:
                stdout_file = open(stdout_path, "w", encoding="utf-8")
                stderr_file = open(stderr_path, "w", encoding="utf-8")
            except OSError as exc:
                self._transition_record(
                    process_id, "failed", exit_code=-1, reason="output_open_failed"
                )
                lease.release()
                return {
                    "error": f"cannot open output files: {exc}",
                    "process_id": process_id,
                }

            try:
                proc = subprocess.Popen(
                    cmd_parts,
                    cwd=str(cwd),
                    env=merged_env,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=os.name != "nt",
                    creationflags=no_window_creationflags(new_process_group=True),
                )
                stdin_pipe = proc.stdin
            except FileNotFoundError:
                stdout_file.close()
                stderr_file.close()
                self._transition_record(
                    process_id, "failed", exit_code=-1, reason="executable_not_found"
                )
                lease.release()
                return {
                    "error": f"executable not found: {cmd_parts[0] if cmd_parts else command}",
                    "process_id": process_id,
                }
            except OSError as exc:
                stdout_file.close()
                stderr_file.close()
                self._transition_record(
                    process_id, "failed", exit_code=-1, reason="process_spawn_failed"
                )
                lease.release()
                return {
                    "error": f"cannot start process: {exc}",
                    "process_id": process_id,
                }

        actual_pid = proc.pid if hasattr(proc, "pid") else pid
        creation_identity = process_creation_identity(actual_pid)
        resource_handle = self._resource_controller.attach(
            process_id=process_id,
            pid=actual_pid,
            process=proc,
            limits=self._resource_limits,
        )
        self._mark_process_started(
            process_id,
            pid=actual_pid,
            tool_name=tool_name,
            request_id=request_id,
        )
        self._database.update_process_runtime(
            process_id,
            pid=actual_pid,
            process_creation_identity=creation_identity,
            heartbeat=_now_iso(),
            job_object_identity=resource_handle.identity,
            recovery_status=(
                f"resource_limits_unavailable:{resource_handle.error}"
                if resource_handle.error
                else "started"
            ),
        )

        deadline = self._clock() + timeout
        rp = _RunningProcess(
            process_id=process_id,
            workspace_id=workspace_id,
            proc=proc,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            working_directory=cwd,
            deadline=deadline,
            pty_handle=pty_handle,
            pty_input_handle=pty_input_write,
            pty_output_handle=pty_output_read,
            pty_reader_thread=pty_reader_thread,
            stdin_pipe=stdin_pipe,
            lease=lease,
            creation_identity=creation_identity,
            resource_handle=resource_handle,
            tool_name=tool_name,
            request_id=request_id,
        )

        with self._lock:
            self._running[process_id] = rp

        # ---- start watchdog thread ----
        threading.Thread(
            target=self._watchdog,
            args=(rp,),
            daemon=True,
        ).start()

        return {
            "process_id": process_id,
            "status": "running",
            "pid": actual_pid,
            "started_at": _now_iso(),
            "tty": use_conpty,
            "pty": use_conpty,
            "columns": columns,
            "rows": rows,
        }

    def write_input(
        self,
        process_id: str,
        text: str,
        append_newline: bool = True,
    ) -> dict[str, Any]:
        """Write text to a running process's stdin.

        Returns a result dict.  On error the dict contains an ``error`` key.
        """
        if not _PROCESS_ID_RE.match(process_id):
            return {"error": f"invalid process_id format: {process_id!r}"}

        rp = self._get_running(process_id)
        if rp is None:
            record = self._database.get_process(process_id)
            if record is None:
                return {"error": f"process not found: {process_id}"}
            return {"error": f"process is not running: status={record['status']}"}

        if rp.proc.poll() is not None:
            self._check_exit(rp)
            return {"error": f"process has already exited (exit_code={rp.proc.returncode})"}

        try:
            rp.write_stdin(text, append_newline=append_newline)
            return {"status": "written", "process_id": process_id}
        except RuntimeError as exc:
            return {"error": str(exc), "process_id": process_id}

    def send_signal(
        self,
        process_id: str,
        signal: str,
    ) -> dict[str, Any]:
        """Send a signal to a running process.

        Supported signals:
        - ``"interrupt"`` — Ctrl+C (SIGINT equivalent)
        - ``"eof"`` — close stdin (EOF)
        - ``"terminate"`` — kill the process tree
        """
        if not _PROCESS_ID_RE.match(process_id):
            return {"error": f"invalid process_id format: {process_id!r}"}

        rp = self._get_running(process_id)
        if rp is None:
            record = self._database.get_process(process_id)
            if record is None:
                return {"error": f"process not found: {process_id}"}
            return {"error": f"process is not running: status={record['status']}"}

        if signal == "interrupt":
            rp.send_interrupt()
            return {"status": "interrupt_sent", "process_id": process_id}
        elif signal == "eof":
            rp.close_stdin()
            return {"status": "eof_sent", "process_id": process_id}
        elif signal == "terminate":
            return self.cancel(process_id)
        else:
            return {"error": f"unsupported signal: {signal!r}"}

    def resize_terminal(self, process_id: str, columns: int, rows: int) -> dict[str, Any]:
        """Resize an active ConPTY terminal."""
        if not 20 <= columns <= 500 or not 5 <= rows <= 200:
            return {"error": "terminal dimensions must be columns 20-500 and rows 5-200"}
        rp = self._get_running(process_id)
        if rp is None:
            record = self._database.get_process(process_id)
            if record is None:
                return {"error": f"process not found: {process_id}"}
            return {"error": f"process is not running: status={record['status']}"}
        if rp.pty_handle is None:
            return {"error": "process does not have an active PTY"}
        wrapper = _get_conpty()
        if wrapper is None:
            return {"error": "ConPTY is unavailable"}
        try:
            wrapper.resize(rp.pty_handle, columns, rows)
        except Exception as exc:
            return {"error": f"terminal resize failed: {exc}"}
        return {
            "status": "resized",
            "process_id": process_id,
            "columns": columns,
            "rows": rows,
        }

    # ---- Internal helpers --------------------------------------------------

    def _append_event(
        self,
        event_type: str,
        *,
        workspace_id: str,
        process_id: str,
        payload: Mapping[str, Any],
        request_id: str | None = None,
    ) -> None:
        if not self._events_enabled:
            return
        try:
            self._event_store.append(
                event_type,
                request_id=request_id,
                workspace_id=workspace_id,
                process_id=process_id,
                payload=payload,
            )
        except Exception:
            log.exception("failed to append %s for %s", event_type, process_id)

    def _mark_process_started(
        self,
        process_id: str,
        *,
        pid: int,
        tool_name: str,
        request_id: str | None,
    ) -> None:
        if self._events_enabled:
            try:
                self._event_store.start_process(
                    process_id,
                    pid=pid,
                    tool_name=tool_name,
                    request_id=request_id,
                )
                return
            except Exception:
                log.exception("failed to atomically start process %s", process_id)
        self._database.update_process_status(process_id, "running", pid=pid)

    def _transition_record(
        self,
        process_id: str,
        status: str,
        *,
        exit_code: int | None = None,
        recovery_status: str | None = None,
        reason: str | None = None,
        request_id: str | None = None,
        output_offsets: Mapping[str, int] | None = None,
    ) -> None:
        if self._events_enabled:
            try:
                event = self._event_store.transition_process(
                    process_id,
                    status,
                    exit_code=exit_code,
                    recovery_status=recovery_status,
                    reason=reason,
                    request_id=request_id,
                    output_offsets=output_offsets,
                )
                record = self._database.get_process(process_id)
                tool_name = str(record.get("tool_name", "")) if record else ""
                if tool_name.startswith("run_check:") and event.get("type") == "process.exited":
                    self._append_event(
                        "check.completed",
                        workspace_id=str(record["workspace_id"]),
                        process_id=process_id,
                        request_id=request_id,
                        payload={
                            "check_id": tool_name.split(":", 1)[1],
                            "status": status,
                            "process_id": process_id,
                        },
                    )
                return
            except Exception:
                log.exception("failed to atomically finish process %s", process_id)
        self._database.update_process_status(
            process_id,
            status=status,
            exit_code=exit_code,
            completed_at=_now_iso(),
        )
        if recovery_status is not None:
            self._database.update_process_runtime(
                process_id, recovery_status=recovery_status
            )

    def _emit_output_events(self, rp: _RunningProcess) -> None:
        for stream, path in (("stdout", rp.stdout_path), ("stderr", rp.stderr_path)):
            current = _file_size(path)
            previous = rp.output_offsets[stream]
            if current == previous:
                continue
            payload: dict[str, Any] = {
                "stream": stream,
                "offset_start": previous if current >= previous else 0,
                "offset_end": current,
            }
            if current < previous:
                payload["warning"] = "output_file_truncated"
            self._append_event(
                "process.output",
                workspace_id=rp.workspace_id,
                process_id=rp.process_id,
                request_id=rp.request_id,
                payload=payload,
            )
            rp.output_offsets[stream] = current

    def _build_env(self, env: dict[str, str] | None) -> dict[str, str]:
        """Build the environment for the subprocess.

        Starts from the current process environment, then overlays
        proxy variables from the operator config.
        """
        merged = dict(os.environ)
        if env is not None:
            merged.update(env)

        proxy_cfg = get_proxy_config()
        if proxy_cfg.get("enabled", True):
            proxy_url = proxy_cfg.get("url", "http://127.0.0.1:7897")
            no_proxy_list = proxy_cfg.get("no_proxy", ["127.0.0.1", "localhost", "::1"])
            no_proxy = ",".join(no_proxy_list)

            merged.setdefault("HTTP_PROXY", proxy_url)
            merged.setdefault("HTTPS_PROXY", proxy_url)
            merged.setdefault("ALL_PROXY", proxy_url)
            merged.setdefault("NO_PROXY", no_proxy)
            merged.setdefault("http_proxy", proxy_url)
            merged.setdefault("https_proxy", proxy_url)
            merged.setdefault("all_proxy", proxy_url)
            merged.setdefault("no_proxy", no_proxy)

        return merged

    def _build_command_line(self, shell: str, command: str) -> list[str]:
        """Build the subprocess command line from the shell and command.

        Returns a list of arguments suitable for ``subprocess.Popen``.
        """
        shell = shell.lower().strip()
        if shell == "pwsh":
            return [
                "pwsh.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ]
        elif shell == "cmd":
            return ["cmd.exe", "/c", command]
        elif shell == "python":
            if command.strip().lower() in {"", "python", "python3", "repl", "-i"}:
                return [os.environ.get("PYTHON", "python"), "-u", "-i"]
            return [os.environ.get("PYTHON", "python"), "-u", "-c", command]
        elif shell == "wsl-bash":
            return ["wsl", "--", "bash", "-c", command]
        else:
            # Treat as a direct executable path.
            return [shell, "-c", command] if command else [shell]

    def _resolve_cwd(
        self,
        worktree_path: Path,
        working_directory: str | None,
    ) -> Path:
        """Resolve and validate the working directory.

        Must be inside the worktree.  Defaults to the worktree root.
        """
        if not working_directory:
            return worktree_path

        candidate = (worktree_path / working_directory).resolve()
        try:
            candidate.relative_to(worktree_path.resolve())
        except ValueError:
            raise ValueError(f"working_directory escapes the worktree: {working_directory!r}")
        if not candidate.is_dir():
            # Try to create it.
            candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _acquire_lease(
        self,
        workspace_id: str,
        *,
        write: bool,
        priority: int,
    ) -> ProcessLease:
        """Acquire a fair scheduler lease for one workspace process."""
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("Process manager is shutting down; new jobs are not accepted")
        try:
            return self._scheduler.acquire(
                workspace_id,
                key=ConcurrencyKey.workspace(workspace_id, write=write),
                priority=priority,
                timeout_seconds=self._queue_timeout,
            )
        except QueueTimeoutError as exc:
            raise RuntimeError(
                f"Maximum concurrent jobs ({self._max_running}) or workspace lock "
                f"unavailable: {exc}"
            ) from exc

    def scheduler_snapshot(self) -> dict[str, Any]:
        """Return current process admission diagnostics."""
        return self._scheduler.snapshot()

    def record_recovery_terminal(
        self, process_id: str, status: str, reason: str
    ) -> None:
        """Persist one recovery disposition with its terminal event."""
        self._transition_record(
            process_id,
            status,
            exit_code=-1,
            recovery_status=reason,
            reason=reason,
        )

    def adopt_recovered_process(self, record: Mapping[str, Any]) -> bool:
        """Resume monitoring a process whose PID identity survived restart."""
        process_id = str(record.get("process_id", ""))
        workspace_id = str(record.get("workspace_id", ""))
        raw_pid = record.get("pid")
        pid = int(raw_pid) if isinstance(raw_pid, int) else 0
        expected_identity = str(record.get("process_creation_identity") or "")
        if (
            not _PROCESS_ID_RE.match(process_id)
            or not workspace_id
            or pid <= 0
            or not expected_identity
            or not pid_alive(pid)
            or process_creation_identity(pid) != expected_identity
        ):
            return False
        try:
            lease = self._scheduler.acquire(
                workspace_id,
                key=ConcurrencyKey.workspace(workspace_id, write=True),
                priority=100,
                timeout_seconds=0,
            )
        except QueueTimeoutError:
            return False

        stdout_path = Path(str(record.get("stdout_path") or ""))
        stderr_path = Path(str(record.get("stderr_path") or ""))
        working_directory = Path(str(record.get("working_directory") or "."))
        recovered = _RecoveredProcess(pid=pid, creation_identity=expected_identity)
        running = _RunningProcess(
            process_id=process_id,
            workspace_id=workspace_id,
            proc=recovered,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            working_directory=working_directory,
            deadline=self._clock() + self._default_timeout,
            lease=lease,
            creation_identity=expected_identity,
            tool_name=str(record.get("tool_name") or "recovered_process"),
        )
        running.output_offsets = {
            "stdout": _file_size(stdout_path),
            "stderr": _file_size(stderr_path),
        }
        with self._lock:
            if process_id in self._running:
                lease.release()
                return True
            self._running[process_id] = running
        self._append_event(
            "tool.started",
            workspace_id=workspace_id,
            process_id=process_id,
            payload={
                "tool_name": running.tool_name,
                "pid": pid,
                "working_directory_redacted": ".",
                "recovered": True,
            },
        )
        threading.Thread(target=self._watchdog, args=(running,), daemon=True).start()
        return True

    def _get_running(self, process_id: str) -> _RunningProcess | None:
        """Return the running-process record, or None."""
        with self._lock:
            return self._running.get(process_id)

    def _check_exit(self, rp: _RunningProcess) -> None:
        """If the process has exited, update DB and clean up."""
        exit_code = rp.proc.poll()
        if exit_code is not None:
            self._finalize(rp, exit_code)

    def _finalize(
        self,
        rp: _RunningProcess,
        exit_code: int,
    ) -> None:
        """Record process completion and remove from the running table."""
        if not rp.claim_completion():
            return
        status = "passed" if exit_code == 0 else "failed"
        self._emit_output_events(rp)
        self._transition_record(
            rp.process_id,
            status,
            exit_code=exit_code,
            reason="process_exit",
            request_id=rp.request_id,
            output_offsets=rp.output_offsets,
        )
        rp.mark_done()
        with self._lock:
            self._running.pop(rp.process_id, None)
        rp.release_slot()

        # Clean up PTY handles.
        self._cleanup_pty(rp)

        self._register_process_artifacts(rp)

        # Capture git status after the command completes.
        if rp.working_directory and rp.working_directory.exists():
            self._log_git_status(rp)

    def _log_git_status(self, rp: _RunningProcess) -> None:
        """Run git status --short --branch and log it as an operation."""
        try:
            result = subprocess.run(
                ["git", "status", "--short", "--branch"],
                cwd=str(rp.working_directory),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=no_window_creationflags(),
            )
            summary = f"git status after pwsh: {result.stdout.strip()[:200] or '(clean)'}"
        except Exception as exc:
            summary = f"git status after pwsh unavailable: {exc}"

        try:
            self._database.log_operation(
                operation_id=_short_id(),
                tool_name="run_pwsh",
                summary=summary,
                workspace_id=rp.workspace_id,
                success=True,
            )
        except Exception:
            log.exception("failed to record git-status audit for %s", rp.process_id)

    def _watchdog(self, rp: _RunningProcess) -> None:
        """Monitor exit, timeout, heartbeat, and resource ceilings."""
        while True:
            remaining = rp.deadline - self._clock()
            if remaining <= 0:
                log.warning(
                    "watchdog: process %s timed out, killing tree (pid=%d)",
                    rp.process_id,
                    rp.proc.pid,
                )
                self._terminate_running(rp, "timed_out", "wall_clock_timeout")
                return

            wait_slice = min(remaining, self._heartbeat_interval)
            if rp.wait(timeout=wait_slice):
                exit_code = rp.proc.poll()
                if exit_code is not None:
                    self._finalize(rp, exit_code)
                    return

            violation = self._resource_violation(rp)
            self._emit_output_events(rp)
            output_offset = self._resource_limits.output_usage(
                (rp.stdout_path, rp.stderr_path)
            )
            self._database.update_process_runtime(
                rp.process_id,
                heartbeat=_now_iso(),
                last_output_offset=output_offset,
                recovery_status="monitoring" if violation is None else violation,
            )
            if violation is not None:
                log.warning(
                    "watchdog: process %s exceeded %s, killing tree (pid=%d)",
                    rp.process_id,
                    violation,
                    rp.proc.pid,
                )
                self._terminate_running(rp, "resource_exhausted", violation)
                return

    def _resource_violation(self, rp: _RunningProcess) -> str | None:
        if self._resource_limits.output_limit_exceeded(
            (rp.stdout_path, rp.stderr_path)
        ):
            return "output_limit_exceeded"
        disk_limit = self._resource_limits.max_disk_bytes
        if disk_limit is not None and _directory_size(rp.working_directory) > disk_limit:
            return "disk_limit_exceeded"
        return None

    def _terminate_running(
        self,
        rp: _RunningProcess,
        status: str,
        recovery_status: str,
    ) -> None:
        if not rp.claim_completion():
            return
        self._kill_tree(rp.proc.pid)
        try:
            rp.proc.terminate()
        except OSError:
            pass
        self._emit_output_events(rp)
        self._transition_record(
            rp.process_id,
            status,
            exit_code=-1,
            recovery_status=recovery_status,
            reason=recovery_status,
            request_id=rp.request_id,
            output_offsets=rp.output_offsets,
        )
        rp.mark_done()
        with self._lock:
            self._running.pop(rp.process_id, None)
        rp.release_slot()
        self._cleanup_pty(rp)
        self._register_process_artifacts(rp)

    def _kill_tree(self, pid: int) -> bool:
        """Kill a process and its descendants without touching unrelated PIDs."""
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    creationflags=no_window_creationflags(),
                )
                # taskkill reports a non-zero status when the process already
                # exited; that is still an idempotent terminal outcome.
                return result.returncode in (0, 128, 255) or not self._pid_alive(pid)
            except Exception as exc:
                log.warning("kill_tree failed for pid %d: %s", pid, exc)
                return False

        try:
            killpg = getattr(os, "killpg")
            sigkill = getattr(signal, "SIGKILL")
            killpg(pid, sigkill)
            return True
        except (AttributeError, ProcessLookupError):
            try:
                os.kill(pid, getattr(signal, "SIGKILL"))
                return True
            except ProcessLookupError:
                return True
            except OSError as exc:
                log.warning("kill_tree failed for pid %d: %s", pid, exc)
                return False
        except OSError as exc:
            log.warning("kill_tree failed for pid %d: %s", pid, exc)
            return False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Return whether a PID is still alive."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _cleanup_pty(self, rp: _RunningProcess) -> None:
        """Clean up PTY handles for a completed process."""
        if rp.pty_handle is not None:
            try:
                conpty_wrapper = _get_conpty()
                if conpty_wrapper:
                    conpty_wrapper.close(
                        rp.pty_handle,
                        rp.pty_input_handle or 0,
                        rp.pty_output_handle or 0,
                    )
            except Exception as exc:
                log.warning("PTY cleanup failed for %s: %s", rp.process_id, exc)
            rp.pty_handle = None
            rp.pty_input_handle = None
            rp.pty_output_handle = None

        # Close stdin pipe if still open.
        if rp.stdin_pipe is not None and not rp.stdin_pipe.closed:
            try:
                rp.stdin_pipe.close()
            except Exception:
                pass
            rp.stdin_pipe = None

        resource_handle = rp.resource_handle
        rp.resource_handle = None
        if resource_handle is not None:
            resource_handle.close()

    def _register_process_artifacts(self, rp: _RunningProcess) -> None:
        """Discover workspace outputs and retain complete process logs."""
        if not self._register_artifacts:
            return
        try:
            from app.services import artifact_registry

            records = artifact_registry.register_process_output_artifacts(
                rp.workspace_id,
                rp.process_id,
                rp.stdout_path,
                rp.stderr_path,
            )
            records.extend(
                artifact_registry.discover_artifacts(
                    rp.working_directory,
                    rp.workspace_id,
                    process_id=rp.process_id,
                )
            )
            seen: set[str] = set()
            for record in records:
                artifact_id = str(record.get("artifact_id", ""))
                if not artifact_id or artifact_id in seen:
                    continue
                seen.add(artifact_id)
                self._append_event(
                    "artifact.created",
                    workspace_id=rp.workspace_id,
                    process_id=rp.process_id,
                    request_id=rp.request_id,
                    payload={
                        "artifact_id": artifact_id,
                        "kind": str(record.get("kind", "unknown")),
                        "relative_path": _redacted_artifact_path(
                            str(record.get("path", "")), rp.working_directory
                        ),
                        "size_bytes": int(record.get("size_bytes", 0)),
                    },
                )
        except Exception as exc:
            log.warning("artifact registration failed for %s: %s", rp.process_id, exc)

    def _build_result(
        self,
        record: dict[str, Any],
        stdout_path: Path | None,
        stderr_path: Path | None,
        tail_chars: int,
    ) -> dict[str, Any]:
        """Build the result dict from a process record and output files."""
        stdout_tail, stdout_truncated = self._read_tail(stdout_path, tail_chars)
        stderr_tail, stderr_truncated = self._read_tail(stderr_path, tail_chars)

        status = str(record["status"])
        return {
            "process_id": record["process_id"],
            "output_artifact_id": record["process_id"],
            "status": status,
            "tool_status": "success",
            "task_status": status,
            "exit_code": record.get("exit_code"),
            "started_at": record.get("started_at", ""),
            "completed_at": record.get("completed_at"),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "truncated": stdout_truncated or stderr_truncated,
            "artifacts": self._database.list_artifacts_for_process(
                str(record["workspace_id"]), str(record["process_id"])
            ),
        }

    def _read_tail(self, path: Path | None, max_chars: int) -> tuple[str, bool]:
        """Read the tail of a file, returning (content, truncated)."""
        if path is None or not path.exists():
            return "", False
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", False
        if len(text) <= max_chars:
            return text, False
        return text[-max_chars:], True


# ---- Module-level helpers -------------------------------------------------

class _RecoveredProcess:
    """Popen-like monitor for a process adopted after service restart."""

    def __init__(self, *, pid: int, creation_identity: str) -> None:
        self.pid = pid
        self.creation_identity = creation_identity
        self.returncode: int | None = None
        self.stdin: Any = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if (
            not pid_alive(self.pid)
            or process_creation_identity(self.pid) != self.creation_identity
        ):
            self.returncode = -1
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        deadline = time.monotonic() + (timeout if timeout is not None else 30.0)
        while time.monotonic() < deadline:
            result = self.poll()
            if result is not None:
                return result
            time.sleep(0.05)
        return None

    def terminate(self) -> None:
        try:
            os.kill(self.pid, getattr(signal, "SIGTERM"))
        except (OSError, ProcessLookupError):
            pass


# Minimal pseudo-process wrapper for ConPTY mode.
class _PseudoProcess:
    """Minimal duck-typed replacement for ``subprocess.Popen``.

    Used when the child process was created via the ConPTY API rather than
    ``subprocess.Popen``.  Exposes just enough of the Popen interface for
    the watchdog and ``_RunningProcess``.
    """

    def __init__(self, *, pid: int, handle: int) -> None:
        self.pid = pid
        self._handle = handle
        self._returncode: int | None = None
        self.stdin: Any = None
        self.stdout: Any = None
        self.stderr: Any = None

    def poll(self) -> int | None:
        """Check if the process has exited."""
        if self._returncode is not None:
            return self._returncode
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            exit_code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(
                wintypes.HANDLE(self._handle),
                ctypes.byref(exit_code),
            ):
                # Process handle might be invalid, assume it exited.
                self._returncode = -1
                return self._returncode
            if exit_code.value == 259:  # STILL_ACTIVE
                return None
            self._returncode = exit_code.value
            return self._returncode
        except Exception:
            return None

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the process to exit, polling."""
        import time as _time

        deadline = _time.monotonic() + (timeout or 30)
        while _time.monotonic() < deadline:
            rc = self.poll()
            if rc is not None:
                return rc
            _time.sleep(0.05)
        return None

    def terminate(self) -> None:
        """Terminate the process."""
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateProcess(wintypes.HANDLE(self._handle), 1)
        except Exception:
            pass


def _optional_positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _directory_size(root: Path) -> int:
    total = 0
    try:
        paths = root.rglob("*")
    except OSError:
        return 0
    for path in paths:
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _redacted_artifact_path(raw_path: str, workspace: Path) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    return secrets.token_hex(6)
