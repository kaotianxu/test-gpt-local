"""End-to-end test of the Phase 3 tool surface.

Drives the running MCP server (127.0.0.1:8765) through:

  1. Verify run_pwsh, get_process_result, cancel_process are advertised
  2. Create a workspace
  3. Run a simple pwsh command (echo) with wait=True
  4. Run a command with wait=False, then poll with get_process_result
  5. Run a command that should fail (non-zero exit code)
  6. Run a command that writes to a file and verify git_status_after
  7. Cancel a long-running command
  8. Discard the workspace
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://127.0.0.1:8765/mcp"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _main_repo_status() -> str:
    """Return the main repository status for the isolation assertion."""
    proc = subprocess.run(
        ["git", "status", "--short", "--branch", "--untracked-files=all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout


def _build_proxy_bypass_factory():
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(proxy=None, trust_env=False)
        kwargs: dict = {"transport": transport, "trust_env": False}
        if headers is not None:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory


async def _call_tool(session: ClientSession, name: str, args: dict) -> dict:
    print(f"\n=== call {name}({json.dumps(args)}) ===")
    result = await session.call_tool(name, args)
    if not result.content:
        print("  (no content)")
        return {}
    for chunk in result.content:
        text = getattr(chunk, "text", "")
        if text:
            print(text[:2000])
            if len(text) > 2000:
                print("  ... [truncated]")
    sc = getattr(result, "structuredContent", None)
    if sc is not None and isinstance(sc, dict):
        if "result" in sc and len(sc) == 1:
            return sc["result"]
        return sc
    for chunk in result.content:
        text = getattr(chunk, "text", "")
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw": text}
    return {}


async def main() -> int:
    main_status_before = _main_repo_status()
    headers = {
        "Accept": "application/json, text/event-stream",
    }
    client_factory = _build_proxy_bypass_factory()

    async with streamablehttp_client(
        MCP_URL, headers=headers, httpx_client_factory=client_factory
    ) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # 1. Verify tools are advertised.
            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)
            print("tools advertised:", tool_names)
            for tool in ("run_pwsh", "get_process_result", "cancel_process"):
                assert tool in tool_names, f"missing tool: {tool}"
            print("[ok] Phase 3 tools advertised")

            # 2. Create a workspace.
            projects = await _call_tool(session, "list_projects", {})
            assert isinstance(projects, list) and projects, projects
            project_id = next(
                project["project_id"]
                for project in projects
                if project["project_id"] == "Gpt-Local"
            )
            print(f"[ok] using project_id={project_id!r}")

            create = await _call_tool(
                session,
                "create_workspace",
                {
                    "project_id": project_id,
                    "task_name": "phase3-acceptance",
                },
            )
            assert "workspace_id" in create, create
            workspace_id = create["workspace_id"]
            worktree_path = Path(create["worktree_path"])
            assert worktree_path.is_dir(), worktree_path
            print(f"[ok] worktree created at {worktree_path}")

            # 3. Run a simple pwsh command with wait=True.
            echo = await _call_tool(
                session,
                "run_pwsh",
                {
                    "workspace_id": workspace_id,
                    "script": "Write-Output 'hello from pwsh'",
                    "wait": True,
                    "timeout_seconds": 30,
                },
            )
            assert echo.get("status") == "passed", echo
            assert echo.get("exit_code") == 0, echo
            stdout = echo.get("stdout_tail", "")
            assert "hello from pwsh" in stdout, f"expected output, got: {stdout}"
            print("[ok] simple pwsh command executed with wait=True")

            # 4. Run a command with wait=False, then poll.
            async_pwsh = await _call_tool(
                session,
                "run_pwsh",
                {
                    "workspace_id": workspace_id,
                    "script": (
                        "Write-Output 'async test'; Start-Sleep -Seconds 2; Write-Output 'done'"
                    ),
                    "wait": False,
                    "timeout_seconds": 30,
                },
            )
            assert "process_id" in async_pwsh, async_pwsh
            async_pid = async_pwsh["process_id"]
            assert async_pwsh.get("status") == "running", async_pwsh
            print(f"[ok] async process started: {async_pid}")

            # Poll until done.
            import asyncio as _asyncio

            for _ in range(20):
                await _asyncio.sleep(0.5)
                poll = await _call_tool(
                    session,
                    "get_process_result",
                    {
                        "process_id": async_pid,
                        "tail_chars": 1000,
                    },
                )
                if poll.get("status") in ("passed", "failed", "timed_out", "cancelled"):
                    break
            assert poll.get("status") == "passed", f"async process should have passed: {poll}"
            poll_stdout = poll.get("stdout_tail", "")
            assert "async test" in poll_stdout, poll_stdout
            print("[ok] async process polled and completed")

            # 5. Run a command that should fail (non-zero exit code).
            failing = await _call_tool(
                session,
                "run_pwsh",
                {
                    "workspace_id": workspace_id,
                    "script": "exit 42",
                    "wait": True,
                    "timeout_seconds": 30,
                },
            )
            assert failing.get("status") == "failed", failing
            assert failing.get("exit_code") == 42, failing
            print("[ok] failing command correctly reported exit code 42")

            # 6. Run a command that creates a file, verify git_status_after.
            file_cmd = await _call_tool(
                session,
                "run_pwsh",
                {
                    "workspace_id": workspace_id,
                    "script": "Set-Content -Path phase3_marker.txt -Value 'phase3 marker'",
                    "wait": True,
                    "timeout_seconds": 30,
                },
            )
            assert file_cmd.get("status") == "passed", file_cmd
            # Verify git status is present.
            gs = file_cmd.get("git_status_after")
            print(f"git_status_after={gs!r}")
            assert gs is not None, (
                f"git_status_after should be present after a modifying command: {file_cmd}"
            )
            assert "phase3_marker.txt" in gs, f"expected phase3_marker.txt in git status, got: {gs}"
            # Also verify the file was actually created.
            marker = worktree_path / "phase3_marker.txt"
            assert marker.is_file(), f"marker file not found: {marker}"
            content = marker.read_text(encoding="utf-8")
            assert "phase3 marker" in content, content
            print("[ok] modifying command returned git_status_after")

            # 7. Cancel a long-running command.
            long_cmd = await _call_tool(
                session,
                "run_pwsh",
                {
                    "workspace_id": workspace_id,
                    "script": "Start-Sleep -Seconds 60; Write-Output 'should not see this'",
                    "wait": False,
                    "timeout_seconds": 120,
                },
            )
            assert "process_id" in long_cmd, long_cmd
            long_pid = long_cmd["process_id"]
            print(f"[ok] long-running process started: {long_pid}")

            # Briefly wait then cancel.
            await _asyncio.sleep(1)
            cancelled = await _call_tool(
                session,
                "cancel_process",
                {
                    "process_id": long_pid,
                },
            )
            assert cancelled.get("status") == "cancelled", cancelled
            assert cancelled.get("process_tree_terminated") is True, cancelled
            print("[ok] long-running process cancelled")

            # Verify the process is now cancelled.
            post_cancel = await _call_tool(
                session,
                "get_process_result",
                {
                    "process_id": long_pid,
                    "tail_chars": 1000,
                },
            )
            assert post_cancel.get("status") == "cancelled", post_cancel
            print("[ok] cancelled process confirmed via get_process_result")

            # 8. Discard workspace.
            discard = await _call_tool(
                session,
                "discard_workspace",
                {
                    "workspace_id": workspace_id,
                },
            )
            assert discard.get("removed_path") is True, discard
            assert not worktree_path.exists(), f"worktree should be gone, found: {worktree_path}"
            print(f"[ok] worktree removed: {worktree_path}")

            assert _main_repo_status() == main_status_before, "main repository changed"

    print("\n[ok] All Phase 3 acceptance tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
