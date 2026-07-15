"""Process manager service.

Manages the lifecycle of subprocesses spawned by MCP tools
(``run_pwsh``, ``run_check``).  Provides:

- Thread-safe subprocess spawning with stdin input
- Timeout watchdog that terminates the process tree
- File-based stdout / stderr capture with size limits
- Concurrent job limits per workspace and globally
- Status tracking in the SQLite database
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import (
    BASE_DIR,
    get_process_config,
    get_proxy_config,
)
from app.storage import database as db

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


class _RunningProcess:
    """Internal bookkeeping for a single managed subprocess."""

    def __init__(
        self,
        process_id: str,
        workspace_id: str,
        proc: subprocess.Popen[str],
        stdout_path: Path,
        stderr_path: Path,
        working_directory: Path,
        deadline: float,
    ) -> None:
        self.process_id = process_id
        self.workspace_id = workspace_id
        self.proc = proc
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.working_directory = working_directory
        self.deadline = deadline
        self._lock = threading.Lock()
        self._completed = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the process finishes, or until *timeout* seconds.

        Returns ``True`` if the process exited, ``False`` on timeout.
        """
        return self._completed.wait(timeout)

    def mark_done(self) -> None:
        """Called by the watchdog when the process has finished."""
        self._completed.set()


class ProcessManager:
    """Singleton that manages subprocess lifecycle.

    Thread-safe.  Use ``ProcessManager.get_instance()`` to obtain the
    shared instance.
    """

    _instance: ProcessManager | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # process_id -> _RunningProcess
        self._running: dict[str, _RunningProcess] = {}
        self._processes_dir = BASE_DIR / "data" / "processes"
        self._processes_dir.mkdir(parents=True, exist_ok=True)

        cfg = get_process_config()
        self._max_running = int(cfg.get("max_running_jobs", 3))
        self._max_output_chars = int(cfg.get("max_output_chars", 200000))
        self._default_timeout = int(cfg.get("default_timeout_seconds", 600))
        self._max_timeout = int(cfg.get("max_timeout_seconds", 3600))

    # ---- Public API --------------------------------------------------------

    @classmethod
    def get_instance(cls) -> ProcessManager:
        """Return the shared process manager instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

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

        with self._lock:
            running_count = sum(1 for rp in self._running.values() if rp.proc.poll() is None)
            if running_count >= self._max_running:
                raise RuntimeError(
                    f"Maximum concurrent jobs ({self._max_running}) already running. "
                    "Wait for a running job to complete or cancel it."
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
        db.insert_process(
            process_id=process_id,
            workspace_id=workspace_id,
            tool_name=tool_name,
            script_sha256=script_sha256,
            script_preview=script_preview,
            working_directory=str(cwd),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        db.update_process_status(process_id, "running")

        # ---- spawn subprocess ----
        try:
            stdout_file = open(stdout_path, "w", encoding="utf-8")
            stderr_file = open(stderr_path, "w", encoding="utf-8")
        except OSError as exc:
            db.update_process_status(process_id, "failed", exit_code=-1)
            db.complete_operation(process_id)
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
            )
        except FileNotFoundError:
            stdout_file.close()
            stderr_file.close()
            db.update_process_status(process_id, "failed", exit_code=-1)
            return {
                "error": f"pwsh executable not found: {pwsh_path}",
                "process_id": process_id,
            }
        except OSError as exc:
            stdout_file.close()
            stderr_file.close()
            db.update_process_status(process_id, "failed", exit_code=-1)
            return {
                "error": f"cannot start pwsh: {exc}",
                "process_id": process_id,
            }

        # Write script to stdin and close pipe so pwsh can start processing.
        try:
            if proc.stdin is not None:
                proc.stdin.write(prefixed_script)
                proc.stdin.close()
        except OSError:
            pass  # will be picked up by the watchdog

        deadline = time.monotonic() + timeout
        rp = _RunningProcess(
            process_id=process_id,
            workspace_id=workspace_id,
            proc=proc,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            working_directory=cwd,
            deadline=deadline,
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

        record = db.get_process(process_id)
        if record is None:
            return {"error": f"process not found: {process_id}", "process_id": process_id}

        stdout_path = Path(record["stdout_path"]) if record.get("stdout_path") else None
        stderr_path = Path(record["stderr_path"]) if record.get("stderr_path") else None

        tail = tail_chars if tail_chars is not None else self._max_output_chars

        return self._build_result(record, stdout_path, stderr_path, tail)

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
            record = db.get_process(process_id)
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

        pid = rp.proc.pid
        tree_killed = self._kill_tree(pid)

        if tree_killed:
            db.update_process_status(
                process_id,
                status="cancelled",
                completed_at=_now_iso(),
            )
        else:
            # Fallback: send SIGTERM / terminate
            try:
                rp.proc.terminate()
            except OSError:
                pass

        rp.mark_done()
        with self._lock:
            self._running.pop(process_id, None)

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

    # ---- Internal helpers --------------------------------------------------

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

    def _get_running(self, process_id: str) -> _RunningProcess | None:
        """Return the running-process record, or None."""
        with self._lock:
            return self._running.get(process_id)

    def _check_exit(self, rp: _RunningProcess) -> None:
        """If the process has exited, update DB and clean up."""
        if rp.proc.poll() is not None:
            self._finalize(rp, rp.proc.returncode)

    def _finalize(
        self,
        rp: _RunningProcess,
        exit_code: int,
    ) -> None:
        """Record process completion and remove from the running table."""
        now = _now_iso()
        status = "passed" if exit_code == 0 else "failed"
        db.update_process_status(
            rp.process_id,
            status=status,
            exit_code=exit_code,
            completed_at=now,
        )
        rp.mark_done()
        with self._lock:
            self._running.pop(rp.process_id, None)

        # Capture git status after the command completes.
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
            )
            summary = f"git status after pwsh: {result.stdout.strip()[:200] or '(clean)'}"
        except Exception as exc:
            summary = f"git status after pwsh unavailable: {exc}"

        db.log_operation(
            operation_id=_short_id(),
            tool_name="run_pwsh",
            summary=summary,
            workspace_id=rp.workspace_id,
            success=True,
        )

    def _watchdog(self, rp: _RunningProcess) -> None:
        """Background thread: wait for process exit or timeout."""
        remaining = rp.deadline - time.monotonic()
        if remaining > 0:
            exited = rp.wait(timeout=remaining)
        else:
            exited = False

        if not exited:
            # Timeout: kill the process tree.
            log.warning(
                "watchdog: process %s timed out, killing tree (pid=%d)",
                rp.process_id,
                rp.proc.pid,
            )
            self._kill_tree(rp.proc.pid)
            try:
                rp.proc.terminate()
            except OSError:
                pass
            db.update_process_status(
                rp.process_id,
                status="timed_out",
                exit_code=-1,
                completed_at=_now_iso(),
            )
            rp.mark_done()
            with self._lock:
                self._running.pop(rp.process_id, None)
            return

        if rp.proc.poll() is not None:
            self._finalize(rp, rp.proc.returncode)

    def _kill_tree(self, pid: int) -> bool:
        """Kill a process and all its children using ``taskkill /T /F``.

        Returns ``True`` if the kill command succeeded.
        """
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return True
        except Exception as exc:
            log.warning("kill_tree failed for pid %d: %s", pid, exc)
            return False

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
            "status": status,
            "tool_status": "success",
            "task_status": status,
            "exit_code": record.get("exit_code"),
            "started_at": record.get("started_at", ""),
            "completed_at": record.get("completed_at"),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "truncated": stdout_truncated or stderr_truncated,
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    return secrets.token_hex(6)
