"""Safe reconciliation of managed processes after a service restart."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.subprocess_utils import no_window_creationflags


def process_creation_identity(pid: int) -> str | None:
    """Return a stable process creation identity, or ``None`` if unavailable.

    A PID alone is unsafe because operating systems reuse process identifiers.
    Linux combines the boot id with ``/proc`` start ticks; Windows uses the
    process creation FILETIME. A portable ``ps`` fallback supports other
    development hosts.
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_creation_identity(pid)

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        stat = stat_path.read_text(encoding="utf-8")
        # The comm field can contain spaces and parentheses, so split only
        # after its final closing parenthesis. Field 22 is starttime; the
        # remainder starts at field 3, making its zero-based index 19.
        remainder = stat[stat.rfind(")") + 2 :].split()
        start_ticks = remainder[19]
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
        except OSError:
            boot_id = "unknown-boot"
        return f"linux:{boot_id}:{start_ticks}"
    except (OSError, IndexError, ValueError):
        pass

    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
            creationflags=no_window_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = result.stdout.strip()
    return f"ps:{started}" if result.returncode == 0 and started else None


def pid_alive(pid: int) -> bool:
    """Return whether *pid* currently names a live process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def recover_processes(
    database: Any,
    process_manager: Any | None = None,
    *,
    identity_reader: Callable[[int], str | None] = process_creation_identity,
    alive_checker: Callable[[int], bool] = pid_alive,
) -> dict[str, Any]:
    """Reconcile queued/running records without re-executing commands.

    Outcomes are deliberately explicit:

    * queued records have no safely resumable OS process and become
      ``interrupted``;
    * missing processes become ``interrupted``;
    * PID identity mismatches become ``lost``;
    * unverifiable records become ``recovery_required``; and
    * identity matches are adopted by ``process_manager`` when supplied.
    """
    summary: dict[str, Any] = {
        "recovered": 0,
        "interrupted": 0,
        "lost": 0,
        "recovery_required": 0,
        "records": [],
    }
    for record in database.list_incomplete_processes():
        process_id = str(record["process_id"])
        status = str(record["status"])
        disposition: str

        if status == "queued":
            disposition = "interrupted"
            _terminal(
                database,
                process_id,
                disposition,
                "queued_without_process",
                process_manager,
            )
        else:
            raw_pid = record.get("pid")
            pid = int(raw_pid) if isinstance(raw_pid, int) or str(raw_pid).isdigit() else 0
            if pid <= 0:
                disposition = "recovery_required"
                _terminal(database, process_id, disposition, "missing_pid", process_manager)
            elif not alive_checker(pid):
                disposition = "interrupted"
                _terminal(
                    database,
                    process_id,
                    disposition,
                    "process_not_found",
                    process_manager,
                )
            else:
                expected = record.get("process_creation_identity")
                current = identity_reader(pid)
                if not expected or current is None:
                    disposition = "recovery_required"
                    _terminal(
                        database,
                        process_id,
                        disposition,
                        "identity_unavailable",
                        process_manager,
                    )
                elif str(expected) != current:
                    disposition = "lost"
                    _terminal(
                        database,
                        process_id,
                        disposition,
                        "pid_identity_mismatch",
                        process_manager,
                    )
                else:
                    adopted = True
                    if process_manager is not None:
                        try:
                            adopted = bool(process_manager.adopt_recovered_process(record))
                        except Exception:
                            adopted = False
                    if adopted:
                        disposition = "recovered"
                        database.update_process_runtime(
                            process_id,
                            heartbeat=_now_iso(),
                            last_output_offset=_output_offset(record),
                            recovery_status="recovered",
                        )
                    else:
                        disposition = "recovery_required"
                        _terminal(
                            database,
                            process_id,
                            disposition,
                            "monitor_adoption_failed",
                            process_manager,
                        )

        summary[disposition] += 1
        summary["records"].append(
            {"process_id": process_id, "disposition": disposition}
        )
    return summary


def _terminal(
    database: Any,
    process_id: str,
    status: str,
    reason: str,
    process_manager: Any | None,
) -> None:
    if process_manager is not None and hasattr(
        process_manager, "record_recovery_terminal"
    ):
        process_manager.record_recovery_terminal(process_id, status, reason)
        return
    database.update_process_status(process_id, status, completed_at=_now_iso())
    database.update_process_runtime(process_id, recovery_status=reason)


def _output_offset(record: dict[str, Any]) -> int:
    total = 0
    for field in ("stdout_path", "stderr_path"):
        raw = record.get(field)
        if not raw:
            continue
        try:
            total += Path(str(raw)).stat().st_size
        except OSError:
            continue
    return total


def _windows_creation_identity(pid: int) -> str | None:
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                process,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return f"windows-filetime:{value}"
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
