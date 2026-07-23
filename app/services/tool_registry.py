"""Central MCP tool contracts and execution middleware.

Every externally registered tool has one :class:`ToolSpec`. The registry is
the authoritative source for effects, idempotency, concurrency, permissions,
retry behaviour, and result limits. ``RegisteredToolMCP`` wraps FastMCP tool
registration so every invocation follows the same execution path.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from functools import wraps
from threading import Lock
from typing import Any, Literal, cast

from mcp.server.fastmcp import FastMCP

from app.services.envelope import (
    audit_event,
    elapsed_ms,
    error_result,
    generate_request_id,
    ok_result,
)
from app.services.workspace_manager import get_workspace
from app.storage.idempotency import (
    _input_hash,
    get_idempotent_result,
    store_idempotent_result,
)

log = logging.getLogger(__name__)

ToolResult = Any
ToolHandler = Callable[[], ToolResult | Awaitable[ToolResult]]
ConcurrencyKey = Callable[[Mapping[str, Any]], str | None]
PermissionEvaluator = Callable[["ToolSpec", Mapping[str, Any]], bool | str]


class Effect(str, Enum):
    """Observable effect classes used for policy and scheduling."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    GIT = "git"
    NETWORK = "network"
    PROCESS_CONTROL = "process_control"


class IdempotencyPolicy(str, Enum):
    """How an idempotency key is handled for a tool."""

    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry policy for middleware-mapped transient failures."""

    max_attempts: int = 1
    retryable_errors: frozenset[str] = frozenset()
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")


@dataclass(frozen=True)
class ToolSpec:
    """Stable execution contract for one MCP tool."""

    name: str
    effects: frozenset[Effect]
    concurrency_key: ConcurrencyKey
    idempotency: IdempotencyPolicy
    interrupt_behavior: Literal["cancel", "block"]
    max_result_chars: int
    permission_profile: str
    retry_policy: RetryPolicy

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must be non-empty")
        if not self.effects:
            raise ValueError(f"tool {self.name!r} must declare at least one effect")
        if self.max_result_chars < 1:
            raise ValueError("max_result_chars must be positive")
        if not self.permission_profile:
            raise ValueError("permission_profile must be non-empty")


class ToolRegistry:
    """Thread-safe registry of immutable tool contracts."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._lock = Lock()

    def register(self, spec: ToolSpec) -> ToolSpec:
        with self._lock:
            existing = self._specs.get(spec.name)
            if existing is not None and existing != spec:
                raise ValueError(f"conflicting ToolSpec registration: {spec.name}")
            self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        with self._lock:
            try:
                return self._specs[name]
            except KeyError as exc:
                raise KeyError(f"ToolSpec not registered: {name}") from exc

    def list(self) -> tuple[ToolSpec, ...]:
        with self._lock:
            return tuple(self._specs[name] for name in sorted(self._specs))

    def names(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._specs)


TOOL_REGISTRY = ToolRegistry()


def register_tool_spec(spec: ToolSpec) -> ToolSpec:
    return TOOL_REGISTRY.register(spec)


def get_tool_spec(name: str) -> ToolSpec:
    return TOOL_REGISTRY.get(name)


def list_tool_specs() -> tuple[ToolSpec, ...]:
    return TOOL_REGISTRY.list()


def _workspace_key(inputs: Mapping[str, Any]) -> str | None:
    value = inputs.get("workspace_id")
    return f"workspace:{value}" if value else None


def _project_key(inputs: Mapping[str, Any]) -> str | None:
    value = inputs.get("project_id")
    return f"project:{value}" if value else None


def _process_key(inputs: Mapping[str, Any]) -> str | None:
    value = inputs.get("process_id")
    return f"process:{value}" if value else None


def _global_key(_: Mapping[str, Any]) -> str:
    return "global:operator"


def _no_key(_: Mapping[str, Any]) -> None:
    return None


def _read_spec(
    name: str,
    *,
    key: ConcurrencyKey = _workspace_key,
    max_result_chars: int = 200_000,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        effects=frozenset({Effect.READ}),
        concurrency_key=key,
        idempotency=IdempotencyPolicy.NONE,
        interrupt_behavior="block",
        max_result_chars=max_result_chars,
        permission_profile="read",
        retry_policy=RetryPolicy(),
    )


def _write_spec(
    name: str,
    *,
    effects: frozenset[Effect] = frozenset({Effect.WRITE}),
    key: ConcurrencyKey = _workspace_key,
    idempotency: IdempotencyPolicy = IdempotencyPolicy.OPTIONAL,
    interrupt_behavior: Literal["cancel", "block"] = "block",
    permission_profile: str = "workspace_write",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        effects=effects,
        concurrency_key=key,
        idempotency=idempotency,
        interrupt_behavior=interrupt_behavior,
        max_result_chars=200_000,
        permission_profile=permission_profile,
        retry_policy=RetryPolicy(),
    )


def _default_specs() -> tuple[ToolSpec, ...]:
    global_reads = {
        "get_capabilities",
        "list_projects",
        "list_workspaces",
        "ping",
        "read_artifact",
        "view_artifact",
    }
    workspace_reads = {
        "get_repo_map",
        "get_workspace",
        "get_workspace_plan",
        "get_workspace_report",
        "git_diff",
        "git_status",
        "list_artifacts",
        "list_checks",
        "list_symbols",
        "find_definition",
        "find_references",
        "find_implementations",
        "get_call_hierarchy",
        "get_diagnostics",
        "get_changed_symbols",
        "read_files",
        "search_code",
        "view_image",
    }
    process_reads = {"get_process_result", "read_process_output"}

    specs = [_read_spec(name, key=_global_key) for name in sorted(global_reads)]
    specs.extend(_read_spec(name) for name in sorted(workspace_reads))
    specs.append(_read_spec("get_events", key=_no_key))
    specs.append(_read_spec("subscribe_process", key=_no_key))
    specs.extend(_read_spec(name, key=_process_key) for name in sorted(process_reads))
    specs.append(_read_spec("get_project_status", key=_project_key))
    specs.extend(
        [
            _write_spec(
                "create_workspace",
                effects=frozenset({Effect.WRITE, Effect.GIT}),
                key=_project_key,
            ),
            _write_spec(
                "discard_workspace",
                effects=frozenset({Effect.WRITE, Effect.GIT}),
            ),
            _write_spec("apply_patch", effects=frozenset({Effect.WRITE, Effect.GIT})),
            _write_spec("replace_text", effects=frozenset({Effect.WRITE, Effect.GIT})),
            _write_spec(
                "run_pwsh",
                effects=frozenset({Effect.WRITE, Effect.EXECUTE, Effect.NETWORK}),
                interrupt_behavior="cancel",
                permission_profile="process_execute",
            ),
            _write_spec(
                "run_check",
                effects=frozenset({Effect.READ, Effect.EXECUTE}),
                interrupt_behavior="cancel",
                permission_profile="process_execute",
            ),
            _write_spec(
                "run_command",
                effects=frozenset({Effect.WRITE, Effect.EXECUTE, Effect.NETWORK}),
                idempotency=IdempotencyPolicy.NONE,
                interrupt_behavior="cancel",
                permission_profile="process_execute",
            ),
            _write_spec(
                "cancel_process",
                effects=frozenset({Effect.PROCESS_CONTROL}),
                key=_process_key,
                permission_profile="process_control",
            ),
            _write_spec(
                "write_process_input",
                effects=frozenset({Effect.PROCESS_CONTROL}),
                key=_process_key,
                idempotency=IdempotencyPolicy.NONE,
                permission_profile="process_control",
            ),
            _write_spec(
                "send_process_signal",
                effects=frozenset({Effect.PROCESS_CONTROL}),
                key=_process_key,
                idempotency=IdempotencyPolicy.NONE,
                permission_profile="process_control",
            ),
            _write_spec(
                "resize_terminal",
                effects=frozenset({Effect.PROCESS_CONTROL}),
                key=_process_key,
                idempotency=IdempotencyPolicy.NONE,
                permission_profile="process_control",
            ),
            _write_spec("update_workspace_plan", idempotency=IdempotencyPolicy.NONE),
            _write_spec("update_workspace_plan_step", idempotency=IdempotencyPolicy.NONE),
        ]
    )
    return tuple(specs)


for _spec in _default_specs():
    register_tool_spec(_spec)


_ALLOWED_PERMISSION_PROFILES = frozenset(
    {"read", "workspace_write", "process_execute", "process_control"}
)


def _default_permission_evaluator(spec: ToolSpec, _: Mapping[str, Any]) -> bool | str:
    if spec.permission_profile not in _ALLOWED_PERMISSION_PROFILES:
        return f"unknown permission profile: {spec.permission_profile}"
    return True


_permission_evaluator: PermissionEvaluator = _default_permission_evaluator


def set_permission_evaluator(evaluator: PermissionEvaluator) -> None:
    global _permission_evaluator
    _permission_evaluator = evaluator


def reset_permission_evaluator() -> None:
    global _permission_evaluator
    _permission_evaluator = _default_permission_evaluator


_concurrency_locks: dict[str, asyncio.Lock] = {}
_concurrency_locks_guard = Lock()


def _get_concurrency_lock(key: str) -> asyncio.Lock:
    with _concurrency_locks_guard:
        lock = _concurrency_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _concurrency_locks[key] = lock
        return lock


@asynccontextmanager
async def _scheduled(key: str | None) -> Any:
    if key is None:
        yield
        return
    lock = _get_concurrency_lock(key)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _validate_inputs(inputs: Mapping[str, Any]) -> str | None:
    if any(not isinstance(key, str) or not key for key in inputs):
        return "tool input keys must be non-empty strings"
    workspace_id = inputs.get("workspace_id")
    if workspace_id is not None and (
        not isinstance(workspace_id, str) or not workspace_id.strip()
    ):
        return "workspace_id must be a non-empty string"
    return None


def _workspace_preflight(inputs: Mapping[str, Any]) -> dict[str, Any] | None:
    workspace_id = inputs.get("workspace_id")
    if not isinstance(workspace_id, str):
        return None
    if get_workspace(workspace_id) is not None:
        return None
    return error_result(
        "WORKSPACE_NOT_FOUND",
        f"workspace not found: {workspace_id}",
        workspace_id=workspace_id,
    )


def _input_summary(inputs: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in sorted(inputs):
        value = inputs[key]
        if key == "idempotency_key":
            parts.append(f"{key}={'present' if value else 'absent'}")
        elif isinstance(value, str):
            parts.append(f"{key}_len={len(value)}")
        elif isinstance(value, (bytes, bytearray, list, tuple, set, frozenset, dict)):
            parts.append(f"{key}_count={len(value)}")
        elif isinstance(value, (bool, int, float)) or value is None:
            parts.append(f"{key}={value!r}")
        else:
            parts.append(f"{key}_type={type(value).__name__}")
    return " ".join(parts)[:500]


def _infer_error_code(message: str) -> str:
    lowered = message.lower()
    if "workspace" in lowered and "not found" in lowered:
        return "WORKSPACE_NOT_FOUND"
    if "process" in lowered and "not running" in lowered:
        return "PROCESS_NOT_RUNNING"
    if "pty" in lowered and ("not active" in lowered or "not a tty" in lowered):
        return "PTY_NOT_ACTIVE"
    if "process" in lowered and "not found" in lowered:
        return "PROCESS_NOT_FOUND"
    if "timed out" in lowered or "timeout" in lowered:
        return "PROCESS_TIMEOUT"
    if "denied" in lowered or "permission" in lowered:
        return "PERMISSION_DENIED"
    if "invalid" in lowered or "must be" in lowered or "required" in lowered:
        return "INVALID_INPUT"
    return "INTERNAL_ERROR"


def _normalise_envelope(
    envelope: dict[str, Any],
    request_id: str,
    workspace_id: str | None,
) -> dict[str, Any]:
    result = dict(envelope)
    result["request_id"] = request_id
    if workspace_id is not None:
        result.setdefault("workspace_id", workspace_id)
    if result.get("ok") is True:
        result.setdefault("warnings", [])
        result.setdefault("truncated", False)
        result.setdefault("next_cursor", None)
    elif result.get("ok") is False:
        error = result.get("error")
        if not isinstance(error, dict):
            message = str(error or "tool execution failed")
            result["error"] = {
                "code": _infer_error_code(message),
                "message": message,
                "retryable": False,
            }
        else:
            error.setdefault("code", _infer_error_code(str(error.get("message", ""))))
            error.setdefault("message", "tool execution failed")
            error.setdefault("retryable", False)
    return result


def _normalise_result(
    result: ToolResult,
    request_id: str,
    workspace_id: str | None,
) -> ToolResult:
    if isinstance(result, list) and result and isinstance(result[0], dict):
        first = cast(dict[str, Any], result[0])
        if "ok" in first:
            return [_normalise_envelope(first, request_id, workspace_id), *result[1:]]
    if isinstance(result, dict) and "ok" in result:
        return _normalise_envelope(cast(dict[str, Any], result), request_id, workspace_id)
    if isinstance(result, dict) and "error" in result:
        raw_error = result.get("error")
        if isinstance(raw_error, dict):
            message = str(raw_error.get("message", "tool execution failed"))
            code = str(raw_error.get("code") or _infer_error_code(message))
            retryable = bool(raw_error.get("retryable", False))
            extra = {
                key: value
                for key, value in raw_error.items()
                if key not in {"code", "message", "retryable"}
            }
        else:
            message = str(raw_error)
            code = str(result.get("error_code") or _infer_error_code(message))
            retryable = False
            extra = {
                key: value
                for key, value in result.items()
                if key not in {"error", "error_code", "workspace_id"}
            }
        return error_result(
            code,
            message,
            retryable=retryable,
            workspace_id=workspace_id,
            request_id=request_id,
            extra=extra or None,
        )
    return ok_result(result, workspace_id=workspace_id, request_id=request_id)


def _exception_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, asyncio.CancelledError):
        return "PROCESS_CANCELLED", False
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "PROCESS_TIMEOUT", True
    if isinstance(exc, PermissionError):
        return "PERMISSION_DENIED", False
    if isinstance(exc, FileNotFoundError):
        return "FILE_NOT_FOUND", False
    if isinstance(exc, (TypeError, ValueError)):
        return "INVALID_INPUT", False
    return "INTERNAL_ERROR", False


async def _invoke(handler: ToolHandler) -> ToolResult:
    value = handler()
    if inspect.isawaitable(value):
        return await value
    return value


def _result_state(result: ToolResult) -> tuple[bool, str | None]:
    envelope: dict[str, Any] | None = None
    if isinstance(result, dict):
        envelope = result
    elif isinstance(result, list) and result and isinstance(result[0], dict):
        envelope = cast(dict[str, Any], result[0])
    if envelope is None or envelope.get("ok") is not False:
        return True, None
    error = envelope.get("error")
    code = str(error.get("code")) if isinstance(error, dict) and error.get("code") else None
    return False, code


def _limit_result(result: ToolResult, spec: ToolSpec, request_id: str) -> ToolResult:
    if not isinstance(result, dict):
        return result
    rendered = json.dumps(result, ensure_ascii=False, default=str)
    if len(rendered) <= spec.max_result_chars:
        return result
    workspace = result.get("workspace_id")
    return ok_result(
        {
            "preview": rendered[: max(1, spec.max_result_chars // 2)],
            "original_chars": len(rendered),
            "limit_chars": spec.max_result_chars,
        },
        workspace_id=str(workspace) if workspace is not None else None,
        request_id=request_id,
        warnings=[f"middleware truncated {spec.name} result to its ToolSpec limit"],
        truncated=True,
    )


async def execute_tool(
    spec_or_name: ToolSpec | str,
    inputs: Mapping[str, Any],
    handler: ToolHandler,
    *,
    idempotency_key: str | None = None,
    input_summary: str | None = None,
) -> ToolResult:
    """Execute one tool through the shared middleware pipeline."""
    spec = get_tool_spec(spec_or_name) if isinstance(spec_or_name, str) else spec_or_name
    request_id = generate_request_id()
    started = time.monotonic()
    workspace_value = inputs.get("workspace_id")
    workspace_id = workspace_value if isinstance(workspace_value, str) else None
    summary = input_summary or _input_summary(inputs)

    def audited(result: ToolResult, status: str = "success") -> ToolResult:
        success, error_code = _result_state(result)
        audit_event(
            tool_name=spec.name,
            request_id=request_id,
            workspace_id=workspace_id,
            input_summary=summary,
            success=success,
            duration_ms=elapsed_ms(started),
            error_code=error_code,
            result_status=status if success else "error",
        )
        return _limit_result(result, spec, request_id)

    validation_error = _validate_inputs(inputs)
    if validation_error is not None:
        return audited(
            error_result(
                "INVALID_INPUT",
                validation_error,
                workspace_id=workspace_id,
                request_id=request_id,
            )
        )

    key = idempotency_key
    if key is None:
        raw_key = inputs.get("idempotency_key")
        key = raw_key if isinstance(raw_key, str) and raw_key else None
    if spec.idempotency is IdempotencyPolicy.REQUIRED and key is None:
        return audited(
            error_result(
                "INVALID_INPUT",
                f"{spec.name} requires idempotency_key",
                workspace_id=workspace_id,
                request_id=request_id,
            )
        )

    idem_inputs = {name: value for name, value in inputs.items() if name != "idempotency_key"}
    idem_hash: str | None = None
    if key is not None and spec.idempotency is not IdempotencyPolicy.NONE:
        idem_hash = _input_hash(spec.name, **idem_inputs)
        cached = get_idempotent_result(key, spec.name, idem_hash)
        if cached is not None:
            if cached.get("_mismatch"):
                result = error_result(
                    "IDEMPOTENCY_KEY_MISMATCH",
                    f"idempotency_key {key!r} was used with different inputs",
                    workspace_id=workspace_id,
                    request_id=request_id,
                    extra={"idempotency_key": key, "stored_hash": cached.get("stored_hash")},
                )
                return audited(result)
            return audited(
                _normalise_result(cached, request_id, workspace_id),
                "idempotent_replay",
            )

    preflight = _workspace_preflight(inputs)
    if preflight is not None:
        return audited(_normalise_result(preflight, request_id, workspace_id))

    permission = _permission_evaluator(spec, inputs)
    if permission is not True:
        message = str(permission) if permission else f"permission denied for {spec.name}"
        return audited(
            error_result(
                "PERMISSION_DENIED",
                message,
                workspace_id=workspace_id,
                request_id=request_id,
            )
        )

    raw_result: ToolResult = None
    attempt = 0
    async with _scheduled(spec.concurrency_key(inputs)):
        while attempt < spec.retry_policy.max_attempts:
            attempt += 1
            try:
                raw_result = await _invoke(handler)
                break
            except Exception as exc:
                code, retryable = _exception_error(exc)
                can_retry = (
                    retryable
                    and code in spec.retry_policy.retryable_errors
                    and attempt < spec.retry_policy.max_attempts
                )
                if can_retry:
                    if spec.retry_policy.backoff_seconds:
                        await asyncio.sleep(spec.retry_policy.backoff_seconds)
                    continue
                log.exception("tool execution failed tool=%s request_id=%s", spec.name, request_id)
                raw_result = error_result(
                    code,
                    str(exc) or type(exc).__name__,
                    retryable=retryable,
                    workspace_id=workspace_id,
                    request_id=request_id,
                )
                break

    result = _normalise_result(raw_result, request_id, workspace_id)
    result = _limit_result(result, spec, request_id)
    if key is not None and idem_hash is not None:
        store_idempotent_result(
            key,
            spec.name,
            idem_hash,
            json.dumps(result, ensure_ascii=False, default=str),
        )
    return audited(result)


def tool_contracts() -> list[dict[str, Any]]:
    """Return a JSON-safe description of every registered contract."""
    contracts: list[dict[str, Any]] = []
    for spec in list_tool_specs():
        retry = asdict(spec.retry_policy)
        retry["retryable_errors"] = sorted(spec.retry_policy.retryable_errors)
        contracts.append(
            {
                "name": spec.name,
                "effects": sorted(effect.value for effect in spec.effects),
                "idempotency": spec.idempotency.value,
                "interrupt_behavior": spec.interrupt_behavior,
                "max_result_chars": spec.max_result_chars,
                "permission_profile": spec.permission_profile,
                "retry_policy": retry,
            }
        )
    return contracts


class RegisteredToolMCP:
    """FastMCP registration proxy that installs execution middleware."""

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp
        self._registered_names: set[str] = set()

    def tool(
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: Any = None,
        icons: list[Any] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        delegate = self._mcp.tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )

        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or function.__name__
            get_tool_spec(tool_name)
            signature = inspect.signature(function)

            @wraps(function)
            async def mediated(*args: Any, **kwargs: Any) -> ToolResult:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                inputs = dict(bound.arguments)
                raw_key = inputs.get("idempotency_key")
                idempotency_key = raw_key if isinstance(raw_key, str) else None
                return await execute_tool(
                    tool_name,
                    inputs,
                    lambda: function(*args, **kwargs),
                    idempotency_key=idempotency_key,
                )

            cast(Any, mediated).__signature__ = signature
            self._registered_names.add(tool_name)
            return delegate(mediated)

        return decorate

    def validate_coverage(self) -> None:
        missing = sorted(TOOL_REGISTRY.names() - self._registered_names)
        if missing:
            raise RuntimeError(
                "ToolSpec entries lack MCP implementations: " + ", ".join(missing)
            )

    @property
    def registered_names(self) -> frozenset[str]:
        return frozenset(self._registered_names)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mcp, name)
