"""Persistent state and single-instance primitives for the background supervisor."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 form."""
    return datetime.now(timezone.utc).isoformat()


def process_creation_identity(pid: int) -> str | None:
    """Return a stable creation identity for *pid*, or ``None`` if it is gone."""
    if pid <= 0:
        return None
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            exit_code = wintypes.DWORD()
            exit_ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            )
            if not exit_ok or exit_code.value != 259:  # STILL_ACTIVE
                return None
            ok = ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return str(ticks)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    proc_path = Path("/proc") / str(pid)
    try:
        return str(proc_path.stat().st_ctime_ns)
    except OSError:
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return "alive"


def process_matches(pid: int, creation_identity: str | None) -> bool:
    """Return whether *pid* is alive and still denotes the recorded process."""
    current = process_creation_identity(pid)
    return current is not None and current == creation_identity


class ServiceLock:
    """A kernel-backed, process-lifetime lock stored in the service data directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> bool:
        """Acquire the lock without waiting; return ``False`` if already held."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self.path, "a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                getattr(fcntl, "flock")(
                    lock_file.fileno(),
                    getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"),
                )
        except OSError:
            lock_file.close()
            return False
        self._file = lock_file
        return True

    def release(self) -> None:
        """Release the held lock, if any."""
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                getattr(fcntl, "flock")(self._file.fileno(), getattr(fcntl, "LOCK_UN"))
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> ServiceLock:
        if not self.acquire():
            raise RuntimeError("service supervisor is already running")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


class ServiceStateStore:
    """Atomic JSON status and file-based shutdown requests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.status_path = root / "status.json"
        self.stop_path = root / "stop.request.json"
        self.lock_path = root / "supervisor.lock"
        root.mkdir(parents=True, exist_ok=True)

    def write_status(self, status: dict[str, Any]) -> None:
        """Atomically replace the public status document."""
        payload = json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix="status-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(20):
                try:
                    os.replace(temp_name, self.status_path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.01)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def read_status(self) -> dict[str, Any] | None:
        """Read the latest status, returning ``None`` for absent/invalid state."""
        for attempt in range(20):
            try:
                value = json.loads(self.status_path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else None
            except (FileNotFoundError, PermissionError):
                if attempt == 19:
                    return None
                time.sleep(0.005)
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def request_stop(self, requester_pid: int) -> dict[str, Any]:
        """Write an atomic shutdown request and return its payload."""
        request = {"requested_at": utc_now(), "requester_pid": requester_pid}
        temp = self.stop_path.with_suffix(".tmp")
        temp.write_text(json.dumps(request), encoding="utf-8")
        os.replace(temp, self.stop_path)
        return request

    def stop_requested(self) -> bool:
        """Return whether a shutdown request exists."""
        return self.stop_path.is_file()

    def clear_stop_request(self) -> None:
        """Remove a stale or fulfilled shutdown request."""
        self.stop_path.unlink(missing_ok=True)
