"""Tests for the Section 2 central tool contract and middleware."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from app.services.tool_registry import (
    TOOL_REGISTRY,
    Effect,
    IdempotencyPolicy,
    RegisteredToolMCP,
    RetryPolicy,
    ToolSpec,
    execute_tool,
    reset_permission_evaluator,
    set_permission_evaluator,
    tool_contracts,
)


def _test_spec(
    name: str,
    *,
    idempotency: IdempotencyPolicy = IdempotencyPolicy.NONE,
    max_result_chars: int = 10_000,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        effects=frozenset({Effect.READ}),
        concurrency_key=lambda _: None,
        idempotency=idempotency,
        interrupt_behavior="block",
        max_result_chars=max_result_chars,
        permission_profile="read",
        retry_policy=RetryPolicy(),
    )


def test_registry_exactly_covers_mcp_tool_surface() -> None:
    root = Path(__file__).resolve().parents[2] / "app" / "tools"
    implementation_names: set[str] = set()
    for path in root.glob("*.py"):
        implementation_names.update(
            re.findall(
                r'@mcp\.tool\(\s*name="([^"]+)"',
                path.read_text(encoding="utf-8"),
            )
        )

    assert len(implementation_names) == 49
    assert TOOL_REGISTRY.names() == implementation_names


def test_contract_descriptions_are_json_safe_and_complete() -> None:
    contracts = tool_contracts()
    assert {contract["name"] for contract in contracts} == TOOL_REGISTRY.names()
    assert all(contract["effects"] for contract in contracts)
    assert all(contract["permission_profile"] for contract in contracts)


async def test_execute_tool_normalises_raw_success_and_error() -> None:
    success = await execute_tool(_test_spec("raw_success"), {}, lambda: {"value": 7})
    assert success["ok"] is True
    assert success["result"] == {"value": 7}
    assert success["request_id"].startswith("req_")

    failure = await execute_tool(
        _test_spec("raw_error"),
        {},
        lambda: {"error": "process is not running"},
    )
    assert failure["ok"] is False
    assert failure["error"]["code"] == "PROCESS_NOT_RUNNING"


async def test_execute_tool_maps_exceptions_to_stable_errors() -> None:
    def fail() -> dict[str, object]:
        raise ValueError("bad input")

    result = await execute_tool(_test_spec("exception"), {}, fail)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


async def test_idempotency_fingerprint_uses_all_inputs() -> None:
    calls = 0
    spec = _test_spec("complete_fingerprint", idempotency=IdempotencyPolicy.OPTIONAL)

    def run() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"call": calls}

    first = await execute_tool(
        spec,
        {"script": "pwd", "working_directory": "a"},
        run,
        idempotency_key="complete-input-key",
    )
    second = await execute_tool(
        spec,
        {"script": "pwd", "working_directory": "a"},
        run,
        idempotency_key="complete-input-key",
    )
    mismatch = await execute_tool(
        spec,
        {"script": "pwd", "working_directory": "b"},
        run,
        idempotency_key="complete-input-key",
    )

    assert first["result"]["call"] == 1
    assert second["result"]["call"] == 1
    assert calls == 1
    assert mismatch["ok"] is False
    assert mismatch["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


async def test_permission_hook_blocks_before_execution() -> None:
    called = False

    def run() -> dict[str, bool]:
        nonlocal called
        called = True
        return {"called": True}

    set_permission_evaluator(lambda _spec, _inputs: "approval required")
    try:
        result = await execute_tool(_test_spec("denied"), {}, run)
    finally:
        reset_permission_evaluator()

    assert called is False
    assert result["ok"] is False
    assert result["error"]["code"] == "PERMISSION_DENIED"


async def test_result_limit_is_enforced_centrally() -> None:
    result = await execute_tool(
        _test_spec("limited", max_result_chars=200),
        {},
        lambda: {"payload": "x" * 1_000},
    )
    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["result"]["original_chars"] > 200


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(
        self,
        name: str | None = None,
        **_: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name or function.__name__] = function
            return function

        return decorate


async def test_registration_proxy_preserves_signature_and_executes_middleware() -> None:
    fake = _FakeMCP()
    proxy = RegisteredToolMCP(cast(FastMCP, fake))

    @proxy.tool(name="ping")
    async def ping(value: int = 1) -> dict[str, int]:
        return {"value": value}

    registered = fake.tools["ping"]
    assert inspect.signature(registered) == inspect.signature(ping)
    result = await registered(9)
    assert result["ok"] is True
    assert result["result"] == {"value": 9}
