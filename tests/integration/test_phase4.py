"""End-to-end test for Phase 4: 基础验收 (basic acceptance).

Drives the running MCP server (127.0.0.1:8765) through the full loop:

  1. Verify list_checks and run_check are advertised
  2. Create a workspace
  3. list_checks — discover available checks
  4. Read a file (simulate GPT reading code)
  5. Apply a patch that adds a test file
  6. Run a check (unit_tests / git_tests) to verify the workspace
  7. Run a check that should fail (nonexistent check_id)
  8. Verify git status and diff
  9. Discard the workspace
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://127.0.0.1:8765/mcp"
REPO_ROOT = Path(__file__).resolve().parents[2]


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
            for tool in (
                "list_checks",
                "run_check",
                "replace_text",
                "get_project_status",
                "get_workspace_report",
            ):
                assert tool in tool_names, f"missing tool: {tool}"
            print("[ok] Phase 4 tools advertised")

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
                    "task_name": "phase4-acceptance",
                },
            )
            assert "workspace_id" in create, create
            workspace_id = create["workspace_id"]
            worktree_path = Path(create["worktree_path"])
            assert worktree_path.is_dir(), worktree_path
            print(f"[ok] worktree created at {worktree_path}")

            # 3. list_checks — discover available checks.
            checks = await _call_tool(
                session,
                "list_checks",
                {
                    "workspace_id": workspace_id,
                },
            )
            assert "checks" in checks, checks
            check_ids = [c["check_id"] for c in checks["checks"]]
            print(f"[ok] available checks: {check_ids}")
            assert "git_tests" in check_ids, f"expected git_tests check, got: {check_ids}"

            # 4. Read a file (simulate GPT reading code before modifying).
            read = await _call_tool(
                session,
                "read_files",
                {
                    "workspace_id": workspace_id,
                    "items": [{"path": "app/server.py", "start_line": 1, "end_line": 10}],
                },
            )
            assert "files" in read, read
            assert read["files"][0].get("content", ""), read
            server_sha256 = read["files"][0]["sha256"]
            print("[ok] file read successfully")

            # 4b. Prefer an exact, hash-guarded replacement for a small edit.
            replaced = await _call_tool(
                session,
                "replace_text",
                {
                    "workspace_id": workspace_id,
                    "path": "app/server.py",
                    "old_text": "Starts a FastMCP server",
                    "new_text": "Runs a FastMCP server",
                    "explanation": "exercise structured text replacement",
                    "expected_sha256": server_sha256,
                },
            )
            assert replaced.get("replacements") == 1, replaced
            assert replaced.get("sha256_after") != server_sha256, replaced

            stale = await _call_tool(
                session,
                "replace_text",
                {
                    "workspace_id": workspace_id,
                    "path": "app/server.py",
                    "old_text": "Runs a FastMCP server",
                    "new_text": "Boots a FastMCP server",
                    "explanation": "prove stale edits are rejected",
                    "expected_sha256": server_sha256,
                },
            )
            assert stale.get("error_type") == "stale_content", stale
            print("[ok] exact replacement and stale-content guard verified")

            # 5. Apply a patch that adds a test marker.
            patch = (
                "--- /dev/null\n"
                "+++ b/phase4_marker.txt\n"
                "@@ -0,0 +1 @@\n"
                "+phase4 acceptance test marker\n"
            )
            patched = await _call_tool(
                session,
                "apply_patch",
                {
                    "workspace_id": workspace_id,
                    "patch": patch,
                    "explanation": "create marker for phase4 acceptance",
                },
            )
            assert patched.get("applied") is True, patched
            print("[ok] patch applied successfully")

            # Verify the file was created.
            marker = worktree_path / "phase4_marker.txt"
            assert marker.is_file(), f"marker not found: {marker}"
            assert "phase4 acceptance test marker" in marker.read_text(encoding="utf-8")
            print("[ok] patched file verified on disk")

            # 6. Run a check (git_tests) to verify workspace state.
            result = await _call_tool(
                session,
                "run_check",
                {
                    "workspace_id": workspace_id,
                    "check_id": "git_tests",
                    "wait": True,
                },
            )
            # git_tests runs: git status --short; git diff --check
            # It should pass (exit code 0) even with untracked files.
            print(f"check result: status={result.get('status')} exit={result.get('exit_code')}")
            assert result.get("status") in ("passed", "failed"), result
            stdout = result.get("stdout_tail", "")
            assert "phase4_marker.txt" in stdout, (
                f"expected marker in git status output, got: {stdout}"
            )
            print("[ok] run_check executed git_tests successfully")

            # 7. Run a check with a nonexistent check_id.
            bad = await _call_tool(
                session,
                "run_check",
                {
                    "workspace_id": workspace_id,
                    "check_id": "nonexistent_check",
                    "wait": True,
                },
            )
            assert "error" in bad, bad
            print("[ok] nonexistent check correctly rejected")

            # 8. Verify git status and diff.
            status = await _call_tool(
                session,
                "git_status",
                {
                    "workspace_id": workspace_id,
                },
            )
            assert status.get("exit_code") == 0, status
            assert "phase4_marker.txt" in status.get("stdout", ""), status
            print("[ok] git_status shows the patch")

            diff = await _call_tool(
                session,
                "git_diff",
                {
                    "workspace_id": workspace_id,
                },
            )
            assert diff.get("exit_code") == 0, diff
            diff_stdout = diff.get("stdout", "")
            # For a new untracked file, git diff may be empty.
            # git_status already confirmed the file is visible.
            print(f"git_diff: {diff_stdout[:200]!r}")
            print("[ok] git_diff checked")

            # 8b. Verify project isolation and unified workspace evidence.
            project_status = await _call_tool(
                session, "get_project_status", {"project_id": project_id}
            )
            assert project_status.get("main_head") == create["base_commit"], project_status

            report = await _call_tool(
                session, "get_workspace_report", {"workspace_id": workspace_id}
            )
            assert report.get("main_repo_unchanged_since_creation") is True, report
            assert report.get("git", {}).get("working_tree_clean") is False, report
            checks_by_id = {check["check_id"]: check for check in report.get("checks", [])}
            assert checks_by_id["git_tests"]["latest_status"] == "passed", report
            assert report.get("audit_state") == "checks_incomplete", report
            assert set(report.get("missing_checks", [])) == {
                "unit_tests",
                "lint",
                "typecheck",
            }, report
            assert report.get("git", {}).get("commits_created") == [], report
            print("[ok] project isolation and workspace report verified")

            # 9. Discard the workspace.
            discard = await _call_tool(
                session,
                "discard_workspace",
                {
                    "workspace_id": workspace_id,
                },
            )
            assert discard.get("removed_path") is True, discard
            assert discard.get("filesystem_removed") is True, discard
            assert discard.get("git_registration_removed") is True, discard
            assert discard.get("database_record_removed") is True, discard
            assert not worktree_path.exists(), f"worktree should be gone, found: {worktree_path}"
            print(f"[ok] worktree removed: {worktree_path}")

    print("\n[ok] Phase 4 basic acceptance (4.0) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
