from __future__ import annotations

from typing import Any

from app.services import process_resources
from app.services.process_resources import (
    ProcessResourceController,
    ProcessResourceHandle,
)
from app.services.process_scheduler import ResourceLimits


def test_disabled_job_limits_are_portable_noop(monkeypatch: Any) -> None:
    monkeypatch.setattr(process_resources.os, "name", "posix")
    handle = ProcessResourceController().attach(
        process_id="pr-12345678",
        pid=123,
        process=object(),
        limits=ResourceLimits(max_output_bytes=1024),
    )

    assert handle == ProcessResourceHandle()
    handle.close()
    handle.close()


def test_job_only_limits_report_platform_unavailability(monkeypatch: Any) -> None:
    monkeypatch.setattr(process_resources.os, "name", "posix")
    handle = ProcessResourceController().attach(
        process_id="pr-12345678",
        pid=123,
        process=object(),
        limits=ResourceLimits(
            cpu_time_seconds=10,
            memory_bytes=1024,
            max_processes=2,
        ),
    )

    assert handle.applied is False
    assert handle.error == "platform_job_limits_unavailable"
