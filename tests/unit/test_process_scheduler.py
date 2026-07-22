from __future__ import annotations

import threading
import time

import pytest

from app.services.process_scheduler import (
    ConcurrencyKey,
    ProcessScheduler,
    QueuePolicy,
    QueueTimeoutError,
)


def _wait_for(predicate: object, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_readers_share_key_but_writer_waits() -> None:
    scheduler = ProcessScheduler(
        QueuePolicy(global_limit=3, per_workspace_limit=3, queue_timeout_seconds=0)
    )
    first = scheduler.acquire(
        "ws-a", key=ConcurrencyKey.workspace("ws-a", write=False)
    )
    second = scheduler.acquire(
        "ws-a", key=ConcurrencyKey.workspace("ws-a", write=False)
    )

    assert scheduler.snapshot()["active"] == 2
    with pytest.raises(QueueTimeoutError):
        scheduler.acquire(
            "ws-a",
            key=ConcurrencyKey.workspace("ws-a", write=True),
            timeout_seconds=0,
        )

    assert first.release() is True
    assert first.release() is False
    assert second.release() is True


def test_global_and_per_workspace_quotas() -> None:
    scheduler = ProcessScheduler(
        QueuePolicy(global_limit=2, per_workspace_limit=1, queue_timeout_seconds=0)
    )
    first = scheduler.acquire("ws-a")
    with pytest.raises(QueueTimeoutError):
        scheduler.acquire("ws-a", timeout_seconds=0)

    second = scheduler.acquire("ws-b")
    with pytest.raises(QueueTimeoutError):
        scheduler.acquire("ws-c", timeout_seconds=0)

    first.release()
    second.release()


def test_round_robin_fairness_across_workspaces() -> None:
    scheduler = ProcessScheduler(
        QueuePolicy(global_limit=1, per_workspace_limit=1, queue_timeout_seconds=2)
    )
    blocker = scheduler.acquire("ws-seed")
    acquired: list[str] = []
    releases = {name: threading.Event() for name in ("a1", "a2", "b1")}

    def worker(name: str, workspace_id: str) -> None:
        lease = scheduler.acquire(workspace_id, timeout_seconds=2)
        acquired.append(name)
        releases[name].wait(timeout=2)
        lease.release()

    threads: list[threading.Thread] = []
    for name, workspace_id, queued in (
        ("a1", "ws-a", 1),
        ("a2", "ws-a", 2),
        ("b1", "ws-b", 1),
    ):
        thread = threading.Thread(target=worker, args=(name, workspace_id), daemon=True)
        thread.start()
        threads.append(thread)
        _wait_for(
            lambda workspace_id=workspace_id, queued=queued: scheduler.snapshot()[
                "queued_by_workspace"
            ].get(workspace_id) == queued
        )

    blocker.release()
    _wait_for(lambda: acquired == ["a1"])
    releases["a1"].set()
    _wait_for(lambda: acquired == ["a1", "b1"])
    releases["b1"].set()
    _wait_for(lambda: acquired == ["a1", "b1", "a2"])
    releases["a2"].set()

    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_priority_precedes_older_lower_priority_workspace() -> None:
    scheduler = ProcessScheduler(
        QueuePolicy(global_limit=1, per_workspace_limit=1, queue_timeout_seconds=2)
    )
    blocker = scheduler.acquire("ws-seed")
    acquired: list[str] = []
    release = threading.Event()

    def worker(name: str, workspace_id: str, priority: int) -> None:
        lease = scheduler.acquire(
            workspace_id, priority=priority, timeout_seconds=2
        )
        acquired.append(name)
        release.wait(timeout=2)
        lease.release()

    low = threading.Thread(target=worker, args=("low", "ws-low", 0), daemon=True)
    high = threading.Thread(target=worker, args=("high", "ws-high", 10), daemon=True)
    low.start()
    _wait_for(lambda: scheduler.snapshot()["queued"] == 1)
    high.start()
    _wait_for(lambda: scheduler.snapshot()["queued"] == 2)

    blocker.release()
    _wait_for(lambda: acquired == ["high"])
    release.set()
    high.join(timeout=2)
    low.join(timeout=2)
    assert acquired == ["high", "low"]


def test_queue_timeout_removes_waiter() -> None:
    scheduler = ProcessScheduler(
        QueuePolicy(global_limit=1, per_workspace_limit=1, queue_timeout_seconds=0.02)
    )
    lease = scheduler.acquire("ws-a")
    with pytest.raises(QueueTimeoutError, match="queue timeout"):
        scheduler.acquire("ws-b")
    assert scheduler.snapshot()["queued"] == 0
    lease.release()
