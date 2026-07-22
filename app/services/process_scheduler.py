"""Fair, keyed scheduling for managed subprocesses.

The scheduler separates admission control from process lifecycle management.
It provides:

* global and per-workspace quotas;
* FIFO ordering within a workspace and round-robin fairness across workspaces;
* integer command priorities without starving equal-priority workspaces;
* read/write concurrency keys; and
* a bounded queue wait with an explicit lease that is released exactly once.

The module is deliberately independent of ``subprocess`` so it can be tested
deterministically and reused by non-PowerShell process tools.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class AccessMode(str, Enum):
    """Access mode used by a :class:`ConcurrencyKey`."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ConcurrencyKey:
    """A resource key whose readers may share and writers are exclusive."""

    scope: str
    resource: str
    mode: AccessMode = AccessMode.WRITE

    def __post_init__(self) -> None:
        if not self.scope.strip() or not self.resource.strip():
            raise ValueError("concurrency key scope and resource must be non-empty")

    @classmethod
    def workspace(cls, workspace_id: str, *, write: bool = True) -> ConcurrencyKey:
        return cls(
            "workspace",
            workspace_id,
            AccessMode.WRITE if write else AccessMode.READ,
        )

    @classmethod
    def file(
        cls,
        workspace_id: str,
        path: str | Path,
        *,
        write: bool = False,
    ) -> ConcurrencyKey:
        normalised = Path(path).as_posix().casefold()
        return cls(
            f"file:{workspace_id}",
            normalised,
            AccessMode.WRITE if write else AccessMode.READ,
        )

    @classmethod
    def process(cls, process_id: str) -> ConcurrencyKey:
        return cls("process", process_id, AccessMode.WRITE)

    @classmethod
    def global_git(cls) -> ConcurrencyKey:
        return cls("global", "git", AccessMode.WRITE)

    def conflicts_with(self, other: ConcurrencyKey) -> bool:
        """Return whether two active keys may not overlap."""
        if self.scope != other.scope or self.resource != other.resource:
            return False
        return self.mode is AccessMode.WRITE or other.mode is AccessMode.WRITE


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Resource ceilings applied to one managed process tree.

    ``None`` means the operating-system or project default is left unchanged.
    The scheduler itself enforces output and disk-accounting limits; platform
    process adapters may additionally enforce CPU, memory, and child-process
    ceilings (for example with a Windows Job Object).
    """

    cpu_time_seconds: int | None = None
    memory_bytes: int | None = None
    max_processes: int | None = None
    max_output_bytes: int | None = None
    max_disk_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "cpu_time_seconds",
            "memory_bytes",
            "max_processes",
            "max_output_bytes",
            "max_disk_bytes",
        ):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when configured")

    def output_usage(self, paths: Iterable[Path]) -> int:
        total = 0
        for path in paths:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def output_limit_exceeded(self, paths: Iterable[Path]) -> bool:
        return (
            self.max_output_bytes is not None
            and self.output_usage(paths) > self.max_output_bytes
        )


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """Admission and fairness policy for :class:`ProcessScheduler`."""

    global_limit: int = 3
    per_workspace_limit: int = 2
    queue_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.global_limit < 1:
            raise ValueError("global_limit must be at least 1")
        if self.per_workspace_limit < 1:
            raise ValueError("per_workspace_limit must be at least 1")
        if self.per_workspace_limit > self.global_limit:
            raise ValueError("per_workspace_limit cannot exceed global_limit")
        if self.queue_timeout_seconds < 0:
            raise ValueError("queue_timeout_seconds must not be negative")


class QueueTimeoutError(RuntimeError):
    """Raised when a process cannot acquire a scheduler lease in time."""


@dataclass(slots=True)
class _Waiter:
    workspace_id: str
    key: ConcurrencyKey | None
    priority: int
    sequence: int
    granted: bool = False
    lease_id: str | None = None


class ProcessLease:
    """One scheduler admission, released idempotently."""

    def __init__(
        self,
        scheduler: ProcessScheduler,
        lease_id: str,
        workspace_id: str,
        key: ConcurrencyKey | None,
    ) -> None:
        self._scheduler = scheduler
        self.lease_id = lease_id
        self.workspace_id = workspace_id
        self.key = key
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> bool:
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._scheduler.release(self.lease_id)
        return True

    def __enter__(self) -> ProcessLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class ProcessScheduler:
    """Thread-safe fair scheduler for subprocess slots."""

    def __init__(
        self,
        policy: QueuePolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy or QueuePolicy()
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._queues: dict[str, deque[_Waiter]] = defaultdict(deque)
        self._workspace_order: deque[str] = deque()
        self._active: dict[str, tuple[str, ConcurrencyKey | None]] = {}
        self._active_by_workspace: dict[str, int] = defaultdict(int)
        self._sequence = 0

    def acquire(
        self,
        workspace_id: str,
        *,
        key: ConcurrencyKey | None = None,
        priority: int = 0,
        timeout_seconds: float | None = None,
    ) -> ProcessLease:
        """Wait for and return a process lease.

        A timeout of zero performs non-blocking admission while still using
        the same fairness and key-conflict rules as queued callers.
        """
        if not workspace_id.strip():
            raise ValueError("workspace_id must be non-empty")
        timeout = (
            self.policy.queue_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if timeout < 0:
            raise ValueError("timeout_seconds must not be negative")

        with self._condition:
            self._sequence += 1
            waiter = _Waiter(workspace_id, key, int(priority), self._sequence)
            queue = self._queues[workspace_id]
            if not queue:
                self._workspace_order.append(workspace_id)
            queue.append(waiter)
            deadline = self._clock() + timeout
            self._grant_waiters()

            while not waiter.granted:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._remove_waiter(waiter)
                    self._grant_waiters()
                    raise QueueTimeoutError(
                        "Process queue timeout: no eligible slot became available "
                        f"for workspace {workspace_id!r} within {timeout:g}s"
                    )
                self._condition.wait(timeout=remaining)
                self._grant_waiters()

            assert waiter.lease_id is not None
            return ProcessLease(self, waiter.lease_id, workspace_id, key)

    def release(self, lease_id: str) -> None:
        """Release a previously granted lease."""
        with self._condition:
            active = self._active.pop(lease_id, None)
            if active is None:
                return
            workspace_id, _ = active
            current = self._active_by_workspace.get(workspace_id, 0)
            if current <= 1:
                self._active_by_workspace.pop(workspace_id, None)
            else:
                self._active_by_workspace[workspace_id] = current - 1
            self._grant_waiters()
            self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        """Return queue and active counts for diagnostics."""
        with self._condition:
            return {
                "active": len(self._active),
                "queued": sum(len(queue) for queue in self._queues.values()),
                "active_by_workspace": dict(self._active_by_workspace),
                "queued_by_workspace": {
                    workspace_id: len(queue)
                    for workspace_id, queue in self._queues.items()
                    if queue
                },
            }

    def _grant_waiters(self) -> None:
        while len(self._active) < self.policy.global_limit:
            candidate = self._next_candidate()
            if candidate is None:
                return
            workspace_id, waiter = candidate
            queue = self._queues[workspace_id]
            popped = queue.popleft()
            assert popped is waiter
            if not queue:
                self._queues.pop(workspace_id, None)
                self._remove_workspace_from_order(workspace_id)

            lease_id = "lease-" + secrets.token_hex(8)
            waiter.lease_id = lease_id
            waiter.granted = True
            self._active[lease_id] = (workspace_id, waiter.key)
            self._active_by_workspace[workspace_id] += 1
            self._condition.notify_all()

    def _next_candidate(self) -> tuple[str, _Waiter] | None:
        eligible: list[tuple[str, _Waiter]] = []
        for workspace_id in tuple(self._workspace_order):
            queue = self._queues.get(workspace_id)
            if not queue:
                continue
            waiter = queue[0]
            if self._eligible(waiter):
                eligible.append((workspace_id, waiter))
        if not eligible:
            return None

        highest_priority = max(waiter.priority for _, waiter in eligible)
        eligible_workspaces = {
            workspace_id
            for workspace_id, waiter in eligible
            if waiter.priority == highest_priority
        }
        for _ in range(len(self._workspace_order)):
            workspace_id = self._workspace_order.popleft()
            self._workspace_order.append(workspace_id)
            if workspace_id in eligible_workspaces:
                queue = self._queues[workspace_id]
                return workspace_id, queue[0]
        return None

    def _eligible(self, waiter: _Waiter) -> bool:
        if (
            self._active_by_workspace.get(waiter.workspace_id, 0)
            >= self.policy.per_workspace_limit
        ):
            return False
        if waiter.key is None:
            return True
        return not any(
            active_key is not None and waiter.key.conflicts_with(active_key)
            for _, active_key in self._active.values()
        )

    def _remove_waiter(self, waiter: _Waiter) -> None:
        queue = self._queues.get(waiter.workspace_id)
        if queue is None:
            return
        try:
            queue.remove(waiter)
        except ValueError:
            return
        if not queue:
            self._queues.pop(waiter.workspace_id, None)
            self._remove_workspace_from_order(waiter.workspace_id)

    def _remove_workspace_from_order(self, workspace_id: str) -> None:
        try:
            self._workspace_order.remove(workspace_id)
        except ValueError:
            pass
