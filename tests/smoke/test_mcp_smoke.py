"""Small, repeatable smoke tests for the live MCP endpoint.

The default tests are read-only and validate the contract that a GPT client
depends on:

* local health and protected-resource discovery;
* MCP initialization and tool discovery;
* capability negotiation and stable response envelopes;
* a lightweight ping and project-registry read.

The workspace lifecycle test is deliberately opt-in because it creates and
deletes a Git worktree.  Enable it with ``SMOKE_MUTATING=1`` and provide a
valid ``SMOKE_PROJECT_ID``.

Run through ``scripts/smoke-test.ps1`` or directly with:

    python -m pytest -q tests/smoke -m smoke
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_MCP_URL = "http://127.0.0.1:8765/mcp"
REQUIRED_TOOLS = {
    "ping",
    "list_projects",
    "get_capabilities",
    "get_project_status",
    "get_workspace_report",
    "get_repo_map",
    "search_code",
    "read_files",
    "git_status",
    "git_diff",
    "apply_patch",
    "run_pwsh",
    "get_process_result",
    "create_workspace",
    "discard_workspace",
}
REQUIRED_CAPABILITIES = {
    "supports_expected_hash",
    "supports_idempotency",
    "supports_async_process",
    "supports_workspace_plan",
}
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _mcp_url() -> str:
    """Return the endpoint under test, allowing non-default local ports."""
    return os.environ.get("SMOKE_MCP_URL", DEFAULT_MCP_URL).rstrip("/")


def _origin_and_path(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"SMOKE_MCP_URL must be an absolute HTTP(S) URL: {url!r}")
    path = "/" + parsed.path.strip("/") if parsed.path.strip("/") else "/"
    return f"{parsed.scheme}://{parsed.netloc}", path


def _metadata_urls(url: str) -> list[str]:
    """Return both RFC 9728 discovery candidates served by this project."""
    origin, path = _origin_and_path(url)
    candidates = {
        f"{origin}/.well-known/oauth-protected-resource",
        f"{origin}/.well-known/oauth-protected-resource{path}",
    }
    return sorted(candidates)


def _client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Build an httpx client that never sends loopback traffic through a proxy."""
    transport = httpx.AsyncHTTPTransport(proxy=None, trust_env=False)
    return httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        headers=headers,
        timeout=timeout or httpx.Timeout(30),
        auth=auth,
    )


def _decode_tool_result(response: Any, tool_name: str) -> Any:
    """Decode structuredContent, with a text fallback for older MCP clients."""
    if getattr(response, "isError", False):
        raise AssertionError(f"MCP tool {tool_name!r} returned isError=true: {response!r}")

    structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        # FastMCP versions have wrapped a single dictionary return value under
        # ``result``.  Accept both forms so this test checks the server contract
        # instead of coupling to one SDK serialization detail.
        if set(structured) == {"result"}:
            return structured["result"]
        return structured

    for item in getattr(response, "content", []):
        text = getattr(item, "text", "")
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"MCP tool {tool_name!r} returned non-JSON text: {text[:500]!r}"
                ) from exc
    raise AssertionError(f"MCP tool {tool_name!r} returned no decodable content")


async def _call_raw(session: ClientSession, tool_name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool and decode its MCP content without imposing an envelope."""
    response = await session.call_tool(tool_name, arguments)
    return _decode_tool_result(response, tool_name)


async def _call_ok(session: ClientSession, tool_name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool and unwrap the project's stable ``ok_result`` envelope."""
    payload = await _call_raw(session, tool_name, arguments)
    assert isinstance(payload, dict), (tool_name, payload)
    assert payload.get("ok") is True, (tool_name, payload)
    assert isinstance(payload.get("request_id"), str), (tool_name, payload)
    assert "result" in payload, (tool_name, payload)
    return payload["result"]


async def _call_capabilities(session: ClientSession) -> dict[str, Any]:
    """Return capabilities in either the current raw or future enveloped form."""
    payload = await _call_raw(session, "get_capabilities", {})
    if isinstance(payload, dict) and payload.get("ok") is True:
        payload = payload.get("result")
    assert isinstance(payload, dict), payload
    return payload


@asynccontextmanager
async def _open_mcp_session() -> AsyncIterator[ClientSession]:
    """Open one initialized Streamable HTTP session in the caller's task."""
    headers = {"Accept": "application/json, text/event-stream"}
    client = _client_factory(headers=headers)
    try:
        async with streamable_http_client(
            _mcp_url(),
            http_client=client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
    finally:
        await client.aclose()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_health_and_protected_resource_discovery() -> None:
    """Verify the local health route and both discovery URL variants."""
    mcp_url = _mcp_url()
    origin, _ = _origin_and_path(mcp_url)
    async with _client_factory() as client:
        health = await client.get(f"{origin}/healthz")
        assert health.status_code == 200, (health.status_code, health.text)
        assert health.json() == {
            "status": "ok",
            "service": "gpt-local-code-operator",
        }

        for metadata_url in _metadata_urls(mcp_url):
            response = await client.get(metadata_url)
            assert response.status_code == 200, (metadata_url, response.status_code, response.text)
            metadata = response.json()
            assert metadata["resource"] == mcp_url
            assert metadata["authorization_servers"] == []


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_mcp_initialization_and_tool_inventory() -> None:
    """Verify MCP initialization and the tools required by the agent workflow."""
    async with _open_mcp_session() as session:
        listed = await session.list_tools()
        names = [tool.name for tool in listed.tools]
        assert len(names) == len(set(names)), f"duplicate MCP tool names: {names}"
        assert REQUIRED_TOOLS <= set(names), f"missing tools: {sorted(REQUIRED_TOOLS - set(names))}"

        for tool in listed.tools:
            assert isinstance(tool.inputSchema, dict), tool.name
            assert tool.inputSchema.get("type") == "object", tool.name


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_capabilities_ping_and_project_registry() -> None:
    """Verify the compatibility handshake and representative read-only calls."""
    async with _open_mcp_session() as session:
        capabilities = await _call_capabilities(session)
        assert capabilities["server_name"] == "gpt-local-code-operator"
        assert SEMVER_RE.fullmatch(str(capabilities["server_version"])), capabilities
        assert SEMVER_RE.fullmatch(str(capabilities["schema_version"])), capabilities
        flags = capabilities["capabilities"]
        assert isinstance(flags, dict), capabilities
        assert REQUIRED_CAPABILITIES <= set(flags), capabilities
        assert all(flags[name] is True for name in REQUIRED_CAPABILITIES), capabilities

        limits = capabilities["limits"]
        assert isinstance(limits, dict), capabilities
        for name in ("max_read_chars", "max_output_chars", "max_timeout_seconds"):
            assert int(limits[name]) > 0, (name, limits)

        ping = await _call_ok(session, "ping", {})
        assert ping["status"] == "ok"
        assert ping["service"] == "gpt-local-code-operator"
        assert SEMVER_RE.fullmatch(str(ping["version"])), ping

        projects = await _call_ok(session, "list_projects", {})
        assert isinstance(projects, list) and projects, projects
        assert all(
            isinstance(project, dict)
            and isinstance(project.get("project_id"), str)
            and isinstance(project.get("name"), str)
            for project in projects
        ), projects

        configured_project = os.environ.get("SMOKE_PROJECT_ID")
        if configured_project:
            project_ids = {str(project["project_id"]) for project in projects}
            assert configured_project in project_ids, projects


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_configured_project_status() -> None:
    """Optionally validate a real project repository without creating a worktree."""
    project_id = os.environ.get("SMOKE_PROJECT_ID")
    if not project_id:
        pytest.skip("set SMOKE_PROJECT_ID to smoke-test a configured repository")

    async with _open_mcp_session() as session:
        status = await _call_ok(session, "get_project_status", {"project_id": project_id})
        assert status["project_id"] == project_id
        assert isinstance(status.get("main_worktree"), str)
        assert isinstance(status.get("main_head"), str) and status["main_head"]
        assert isinstance(status.get("workspaces"), list)


@pytest.mark.smoke
@pytest.mark.smoke_mutating
@pytest.mark.asyncio
async def test_optional_workspace_lifecycle() -> None:
    """Create, inspect, and delete one isolated worktree when explicitly enabled."""
    enabled = os.environ.get("SMOKE_MUTATING", "").lower() in {"1", "true", "yes"}
    if not enabled:
        pytest.skip("set SMOKE_MUTATING=1 to enable worktree lifecycle smoke test")

    project_id = os.environ.get("SMOKE_PROJECT_ID")
    if not project_id:
        pytest.fail("SMOKE_PROJECT_ID is required when SMOKE_MUTATING=1")

    suffix = uuid.uuid4().hex[:10]
    task_name = f"smoke-{suffix}"
    create_key = f"smoke-create-{suffix}"
    discard_key = f"smoke-discard-{suffix}"
    async with _open_mcp_session() as session:
        created = await _call_ok(
            session,
            "create_workspace",
            {
                "project_id": project_id,
                "task_name": task_name,
                "idempotency_key": create_key,
            },
        )
        workspace_id = str(created["workspace_id"])
        worktree_path = Path(str(created["worktree_path"]))
        assert worktree_path.is_dir(), worktree_path

        cleanup_error: BaseException | None = None
        try:
            workspace = await _call_ok(
                session,
                "get_workspace",
                {"workspace_id": workspace_id},
            )
            assert workspace["workspace_id"] == workspace_id
            assert Path(str(workspace["worktree_path"])) == worktree_path

            repo_map = await _call_ok(
                session,
                "get_repo_map",
                {"workspace_id": workspace_id},
            )
            assert repo_map["workspace_id"] == workspace_id
            tree = repo_map.get("tree")
            assert isinstance(tree, dict)
            assert isinstance(tree.get("children"), list)

            git_status = await _call_ok(
                session,
                "git_status",
                {"workspace_id": workspace_id},
            )
            assert git_status["exit_code"] == 0, git_status

            git_diff = await _call_ok(
                session,
                "git_diff",
                {"workspace_id": workspace_id, "stat_only": True},
            )
            assert git_diff["exit_code"] == 0, git_diff

            execution = await _call_ok(
                session,
                "run_pwsh",
                {
                    "workspace_id": workspace_id,
                    "script": "Write-Output 'mcp-smoke-ok'",
                    "wait": True,
                    "timeout_seconds": 10,
                },
            )
            assert execution["status"] == "passed", execution
            assert "mcp-smoke-ok" in str(execution.get("stdout_tail")), execution
            process_id = execution.get("process_id")
            if process_id:
                execution_again = await _call_ok(
                    session,
                    "get_process_result",
                    {"process_id": str(process_id), "tail_chars": 1000},
                )
                assert execution_again["status"] == "passed", execution_again
        finally:
            try:
                discarded = await _call_ok(
                    session,
                    "discard_workspace",
                    {
                        "workspace_id": workspace_id,
                        "idempotency_key": discard_key,
                    },
                )
                assert discarded.get("removed_path") is True, discarded
                assert not worktree_path.exists(), worktree_path
            except BaseException as exc:  # preserve cleanup failure for a clear test error
                cleanup_error = exc

        if cleanup_error is not None:
            raise cleanup_error
