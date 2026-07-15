"""End-to-end test of the Phase 1 tool surface.

Drives the running MCP server (127.0.0.1:8765) through:

  1. ping
  2. list_projects
  3. create_workspace  -> verifies the new detached worktree
  4. get_workspace
  5. get_repo_map
  6. search_code       -> verifies ChatGPT can locate a call chain
  7. read_files        -> confirms line-range reads
  8. git_status / git_diff on a clean worktree
  9. discard_workspace -> verifies the worktree is removed and the
                          main branch is untouched
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
PRMD_URL = "http://127.0.0.1:8765/.well-known/oauth-protected-resource/mcp"
PRMD_ROOT_URL = "http://127.0.0.1:8765/.well-known/oauth-protected-resource"

# A function name that exists in this repo and has multiple call sites,
# so search_code has something useful to find.
TARGET_SYMBOL = "_now_iso"
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
    """Return a factory that builds an httpx client bypassing the proxy."""

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
            print(text[:1500])
            if len(text) > 1500:
                print("  ... [truncated]")
    # Structured content (FastMCP puts dict-shaped results here).
    sc = getattr(result, "structuredContent", None)
    if sc is not None and isinstance(sc, dict):
        # FastMCP wraps the return value under "result" for single-key
        # dicts; unwrap one level for convenience.
        if "result" in sc and len(sc) == 1:
            return sc["result"]
        return sc
    # Otherwise parse the first text chunk as JSON if possible.
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

    async with client_factory() as probe:
        for metadata_url in (PRMD_URL, PRMD_ROOT_URL):
            metadata_response = await probe.get(metadata_url)
            assert metadata_response.status_code == 200, (
                metadata_url,
                metadata_response.status_code,
                metadata_response.text,
            )
            metadata = metadata_response.json()
            assert metadata["resource"] == MCP_URL, metadata
            assert metadata["authorization_servers"] == [], metadata
        print("[ok] no-auth protected-resource metadata is available")

    async with streamablehttp_client(
        MCP_URL, headers=headers, httpx_client_factory=client_factory
    ) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)
            print("tools advertised:", tool_names)
            expected = {
                "ping",
                "list_projects",
                "create_workspace",
                "get_workspace",
                "list_workspaces",
                "discard_workspace",
                "get_repo_map",
                "search_code",
                "read_files",
                "git_status",
                "git_diff",
                "apply_patch",
                "run_pwsh",
                "get_process_result",
                "cancel_process",
                "list_checks",
                "run_check",
            }
            missing = expected - set(tool_names)
            assert not missing, f"missing tools: {missing}"
            print("[ok] all 17 tools advertised")

            # 1. ping
            await _call_tool(session, "ping", {})

            # 2. list_projects
            projects = await _call_tool(session, "list_projects", {})
            assert isinstance(projects, list) and projects, projects
            project_id = next(
                project["project_id"]
                for project in projects
                if project["project_id"] == "Gpt-Local"
            )
            print(f"[ok] using project_id={project_id!r}")

            # 3. create_workspace
            create = await _call_tool(
                session,
                "create_workspace",
                {
                    "project_id": project_id,
                    "task_name": "phase1-acceptance",
                },
            )
            assert "workspace_id" in create, create
            workspace_id = create["workspace_id"]
            worktree_path = Path(create["worktree_path"])
            base_commit = create["base_commit"]
            assert worktree_path.is_dir(), worktree_path
            assert (worktree_path / ".git").exists() or Path(worktree_path / ".git").is_file(), (
                "not a worktree"
            )
            print(f"[ok] worktree created at {worktree_path}, base={base_commit[:8]}")

            # 4. get_workspace
            got = await _call_tool(session, "get_workspace", {"workspace_id": workspace_id})
            assert got.get("workspace_id") == workspace_id

            # 5. list_workspaces
            listed = await _call_tool(session, "list_workspaces", {"project_id": project_id})
            assert any(w.get("workspace_id") == workspace_id for w in listed), listed

            # 6. get_repo_map
            await _call_tool(
                session,
                "get_repo_map",
                {
                    "workspace_id": workspace_id,
                    "path": "app/services",
                },
            )

            # 7. search_code — should find the call chain for _now_iso
            search = await _call_tool(
                session,
                "search_code",
                {
                    "workspace_id": workspace_id,
                    "query": TARGET_SYMBOL,
                    "path": "app",
                    "max_results": 25,
                },
            )
            assert "matches" in search, search
            assert search["match_count"] >= 2, (
                f"search should return the call chain for {TARGET_SYMBOL}"
            )
            paths = {m["path"] for m in search["matches"]}
            print(f"[ok] search_code found {search['match_count']} matches in {len(paths)} files")
            assert any(
                p.endswith("app/storage/database.py") or p.endswith("app\\storage\\database.py")
                for p in paths
            ), paths

            # 8. read_files
            read = await _call_tool(
                session,
                "read_files",
                {
                    "workspace_id": workspace_id,
                    "items": [{"path": "app/server.py", "start_line": 1, "end_line": 5}],
                },
            )
            files = read.get("files", [])
            assert files and "content" in files[0], read
            assert "app.server" in files[0]["content"] or "FastMCP" in files[0]["content"], files[0]

            # 9. git_status on a clean worktree
            status = await _call_tool(session, "git_status", {"workspace_id": workspace_id})
            print(
                f"git_status exit={status.get('exit_code')} "
                f"stdout={status.get('stdout', '')[:200]!r}"
            )
            assert status.get("exit_code") == 0, status
            assert "HEAD (no branch)" in status.get("stdout", ""), status

            # The file tools must not expose obvious repository secrets.
            denied = await _call_tool(
                session,
                "read_files",
                {
                    "workspace_id": workspace_id,
                    "items": [{"path": ".git"}],
                },
            )
            assert "error" in denied.get("files", [{}])[0], denied
            assert "content" not in denied["files"][0], denied

            # 10. git_diff on a clean worktree
            diff = await _call_tool(session, "git_diff", {"workspace_id": workspace_id})
            assert diff.get("exit_code") == 0, diff
            assert diff.get("stdout", "") == "", (
                f"worktree should be clean, got: {diff['stdout']!r}"
            )

            # Discard must remove uncommitted worktree changes as well.
            marker = worktree_path / "phase1-acceptance-marker.txt"
            marker.write_text("discard me\n", encoding="utf-8")
            dirty = await _call_tool(session, "git_status", {"workspace_id": workspace_id})
            assert "phase1-acceptance-marker.txt" in dirty.get("stdout", ""), dirty

            # 11. discard_workspace
            discard = await _call_tool(session, "discard_workspace", {"workspace_id": workspace_id})
            assert discard.get("removed_path") is True, discard
            assert not worktree_path.exists(), f"worktree should be gone, found: {worktree_path}"
            print(f"[ok] worktree removed: {worktree_path}")

            assert _main_repo_status() == main_status_before, "main repository changed"

            # 12. confirm the main repository was not modified
            after = await _call_tool(session, "get_workspace", {"workspace_id": workspace_id})
            assert after.get("error") or after.get("workspace_id") is None, after

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
