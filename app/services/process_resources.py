"""Platform process-tree resource controls.

Windows uses a Job Object so CPU time, memory, and active-process ceilings apply
across the managed process tree. Other platforms retain the portable watchdog
limits (wall clock, output, and disk) until a platform adapter is configured.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any

from app.services.process_scheduler import ResourceLimits

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessResourceHandle:
    """A closeable OS resource-control attachment."""

    identity: str | None = None
    applied: bool = False
    error: str | None = None
    _native_handle: int | None = None

    def close(self) -> None:
        handle = self._native_handle
        self._native_handle = None
        if handle is None or os.name != "nt":
            return
        try:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        except Exception:
            log.exception("failed to close process resource handle")


class ProcessResourceController:
    """Attach platform resource limits to one process tree."""

    def attach(
        self,
        *,
        process_id: str,
        pid: int,
        process: Any,
        limits: ResourceLimits,
    ) -> ProcessResourceHandle:
        if os.name != "nt":
            return ProcessResourceHandle(
                error="platform_job_limits_unavailable" if _needs_job_limits(limits) else None
            )
        if not _needs_job_limits(limits):
            return ProcessResourceHandle()
        return _attach_windows_job(
            process_id=process_id,
            pid=pid,
            process=process,
            limits=limits,
        )


def _needs_job_limits(limits: ResourceLimits) -> bool:
    return any(
        value is not None
        for value in (
            limits.cpu_time_seconds,
            limits.memory_bytes,
            limits.max_processes,
        )
    )


def _attach_windows_job(
    *,
    process_id: str,
    pid: int,
    process: Any,
    limits: ResourceLimits,
) -> ProcessResourceHandle:
    import ctypes
    import ctypes.wintypes as wintypes

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_limit_job_time = 0x00000004
    job_object_limit_active_process = 0x00000008
    job_object_limit_job_memory = 0x00000200
    job_object_extended_limit_information = 9
    process_terminate = 0x0001
    process_set_quota = 0x0100
    process_query_limited_information = 0x1000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job_name = f"gpt-local-{process_id}-{secrets.token_hex(4)}"
    job = kernel32.CreateJobObjectW(None, job_name)
    if not job:
        return ProcessResourceHandle(error=_last_windows_error("CreateJobObjectW"))

    process_handle: int | None = None
    opened_handle = False
    keep_job = False
    try:
        info = JobObjectExtendedLimitInformation()
        flags = 0
        if limits.cpu_time_seconds is not None:
            info.BasicLimitInformation.PerJobUserTimeLimit = (
                limits.cpu_time_seconds * 10_000_000
            )
            flags |= job_object_limit_job_time
        if limits.max_processes is not None:
            info.BasicLimitInformation.ActiveProcessLimit = limits.max_processes
            flags |= job_object_limit_active_process
        if limits.memory_bytes is not None:
            info.JobMemoryLimit = limits.memory_bytes
            flags |= job_object_limit_job_memory
        info.BasicLimitInformation.LimitFlags = flags

        if not kernel32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            return ProcessResourceHandle(error=_last_windows_error("SetInformationJobObject"))

        raw_handle = getattr(process, "_handle", None)
        if raw_handle is not None:
            process_handle = int(raw_handle)
        if not process_handle:
            process_handle = int(
                kernel32.OpenProcess(
                    process_terminate
                    | process_set_quota
                    | process_query_limited_information,
                    False,
                    pid,
                )
                or 0
            )
            opened_handle = bool(process_handle)
        if not process_handle:
            return ProcessResourceHandle(error=_last_windows_error("OpenProcess"))
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            return ProcessResourceHandle(error=_last_windows_error("AssignProcessToJobObject"))

        keep_job = True
        return ProcessResourceHandle(
            identity=f"windows-job:{job_name}",
            applied=True,
            _native_handle=int(job),
        )
    finally:
        if opened_handle and process_handle:
            kernel32.CloseHandle(process_handle)
        if not keep_job:
            kernel32.CloseHandle(job)


def _last_windows_error(operation: str) -> str:
    import ctypes

    code = ctypes.get_last_error()
    return f"{operation} failed with Windows error {code}"
