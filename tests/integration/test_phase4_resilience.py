"""Live Phase 4 resilience and state-consistency acceptance checks."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://127.0.0.1:8765/mcp"
PROJECT_ID = "phase4-order-fixture"


def _client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(proxy=None, trust_env=False)
    return httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        headers=headers,
        timeout=timeout or httpx.Timeout(30),
        auth=auth,
    )


async def _call(session: ClientSession, name: str, args: dict[str, object]) -> dict:
    result = await session.call_tool(name, args)
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        value = structured.get("result", structured)
        if isinstance(value, dict):
            return value
    for item in result.content:
        text = getattr(item, "text", "")
        if text:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    return {}


async def _create(session: ClientSession, task_name: str) -> dict:
    result = await _call(
        session,
        "create_workspace",
        {"project_id": PROJECT_ID, "task_name": task_name},
    )
    assert result.get("workspace_id"), result
    return result


async def main() -> int:
    headers = {"Accept": "application/json, text/event-stream"}
    async with streamablehttp_client(
        MCP_URL,
        headers=headers,
        httpx_client_factory=_client_factory,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            workspaces = [
                await _create(session, "phase4-resilience-a"),
                await _create(session, "phase4-resilience-b"),
                await _create(session, "phase4-resilience-c"),
            ]
            ids = [str(item["workspace_id"]) for item in workspaces]
            assert len(set(ids)) == 3
            paths = [Path(str(item["worktree_path"])) for item in workspaces]

            # Workspace isolation: a marker in B must not appear in A or C.
            marker = await _call(
                session,
                "run_pwsh",
                {
                    "workspace_id": ids[1],
                    "script": "Set-Content isolation-marker.txt 'workspace-b'",
                    "wait": True,
                    "timeout_seconds": 10,
                },
            )
            assert marker.get("status") == "passed", marker
            assert (paths[1] / "isolation-marker.txt").is_file()
            assert not (paths[0] / "isolation-marker.txt").exists()
            assert not (paths[2] / "isolation-marker.txt").exists()

            # Structured invalid-check rejection without process creation.
            invalid = await _call(
                session,
                "run_check",
                {"workspace_id": ids[0], "check_id": "does-not-exist", "wait": True},
            )
            assert "error" in invalid and "Available checks" in str(invalid["error"])
            assert "process_id" not in invalid

            # run_check async mode returns a queryable process with a stable terminal state.
            async_check = await _call(
                session,
                "run_check",
                {"workspace_id": ids[0], "check_id": "unit_tests", "wait": False},
            )
            assert async_check.get("status") == "running", async_check
            async_check_id = str(async_check["process_id"])
            for _ in range(50):
                async_result = await _call(
                    session,
                    "get_process_result",
                    {"process_id": async_check_id, "tail_chars": 2000},
                )
                if async_result.get("status") in {
                    "passed",
                    "failed",
                    "timed_out",
                    "cancelled",
                }:
                    break
                await asyncio.sleep(0.2)
            assert async_result.get("status") == "failed", async_result
            assert async_result.get("exit_code") == 1, async_result
            assert async_result.get("started_at"), async_result
            async_result_again = await _call(
                session,
                "get_process_result",
                {"process_id": async_check_id, "tail_chars": 2000},
            )
            assert async_result_again.get("status") == "failed", async_result_again
            assert async_result_again.get("exit_code") == 1, async_result_again

            hygiene = await _call(
                session,
                "git_status",
                {"workspace_id": ids[0]},
            )
            assert hygiene.get("exit_code") == 0, hygiene
            assert "__pycache__" not in str(hygiene.get("stdout")), hygiene
            assert ".pytest_cache" not in str(hygiene.get("stdout")), hygiene
            assert not list(paths[0].rglob("__pycache__"))
            assert not (paths[0] / ".pytest_cache").exists()

            # Non-zero exit preserves both streams and the real exit code.
            failed = await _call(
                session,
                "run_pwsh",
                {
                    "workspace_id": ids[0],
                    "script": (
                        "Write-Output 'failure-out'; "
                        "[Console]::Error.WriteLine('failure-err'); exit 7"
                    ),
                    "wait": True,
                    "timeout_seconds": 10,
                },
            )
            assert failed.get("status") == "failed", failed
            assert failed.get("exit_code") == 7, failed
            assert "failure-out" in str(failed.get("stdout_tail"))
            assert "failure-err" in str(failed.get("stderr_tail"))
            assert failed.get("started_at"), failed

            # Timeout is a stable terminal state and the workspace remains usable.
            timed_out = await _call(
                session,
                "run_pwsh",
                {
                    "workspace_id": ids[0],
                    "script": "Start-Sleep -Seconds 30",
                    "wait": True,
                    "timeout_seconds": 1,
                },
            )
            assert timed_out.get("status") == "timed_out", timed_out
            timeout_id = str(timed_out["process_id"])
            for _ in range(3):
                again = await _call(
                    session,
                    "get_process_result",
                    {"process_id": timeout_id, "tail_chars": 1000},
                )
                assert again.get("status") == "timed_out", again

            recovered = await _call(
                session,
                "run_pwsh",
                {
                    "workspace_id": ids[0],
                    "script": "Write-Output 'after-timeout'",
                    "wait": True,
                    "timeout_seconds": 10,
                },
            )
            assert recovered.get("status") == "passed", recovered

            # Active cancellation is idempotent and remains cancelled.
            running = await _call(
                session,
                "run_pwsh",
                {
                    "workspace_id": ids[2],
                    "script": "Start-Sleep -Seconds 30",
                    "wait": False,
                    "timeout_seconds": 60,
                },
            )
            process_id = str(running["process_id"])
            cancelled = await _call(session, "cancel_process", {"process_id": process_id})
            assert cancelled.get("status") == "cancelled", cancelled
            cancelled_again = await _call(
                session,
                "cancel_process",
                {"process_id": process_id},
            )
            assert cancelled_again.get("status") == "cancelled", cancelled_again

            # Oversized output is bounded, marked truncated, and retains its tail.
            oversized = await _call(
                session,
                "run_pwsh",
                {
                    "workspace_id": ids[2],
                    "script": "Write-Output ('x' * 250000); Write-Output 'OUTPUT-END'",
                    "wait": True,
                    "timeout_seconds": 30,
                },
            )
            output_id = str(oversized["process_id"])
            tail = await _call(
                session,
                "get_process_result",
                {"process_id": output_id, "tail_chars": 1000},
            )
            assert tail.get("status") == "passed", tail
            assert tail.get("truncated") is True, tail
            assert "OUTPUT-END" in str(tail.get("stdout_tail")), tail
            assert len(str(tail.get("stdout_tail"))) <= 1000

            # Discard B while A and C stay intact, then clean them up too.
            discarded_b = await _call(
                session,
                "discard_workspace",
                {"workspace_id": ids[1]},
            )
            assert discarded_b.get("removed_path") is True, discarded_b
            assert not paths[1].exists() and paths[0].exists() and paths[2].exists()

            for index in (0, 2):
                discarded = await _call(
                    session,
                    "discard_workspace",
                    {"workspace_id": ids[index]},
                )
                assert discarded.get("removed_path") is True, discarded
                assert not paths[index].exists()

    print("[ok] Phase 4 resilience, terminal-state, and isolation acceptance passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
