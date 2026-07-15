"""End-to-end test of the Phase 2 tool surface.

Drives the running MCP server (127.0.0.1:8765) through:

  1. Verify apply_patch is advertised
  2. Create a workspace
  3. Apply a valid patch that modifies a file
  4. Verify the changes via git_status and git_diff
  5. Apply an invalid patch (syntax error) — must be rejected
  6. Apply a patch with absolute paths — must be rejected
  7. Apply a patch with path traversal — must be rejected
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

            # 1. Verify apply_patch is advertised.
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
            }
            missing = expected - set(tool_names)
            unexpected = set(tool_names) - expected
            assert not missing, f"missing tools: {missing}"
            if unexpected:
                print(f"[note] unexpected tools: {unexpected}")
            print("[ok] apply_patch advertised")

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
                    "task_name": "phase2-acceptance",
                },
            )
            assert "workspace_id" in create, create
            workspace_id = create["workspace_id"]
            worktree_path = Path(create["worktree_path"])
            assert worktree_path.is_dir(), worktree_path
            print(f"[ok] worktree created at {worktree_path}")

            # 3. Apply a valid patch that creates a new file.
            # For a new file, git diff --stat is empty (file is untracked),
            # but git_status will show it.
            valid_patch = (
                "--- /dev/null\n"
                "+++ b/phase2_applied_marker.txt\n"
                "@@ -0,0 +1 @@\n"
                "+phase2 patch applied successfully\n"
            )
            patched = await _call_tool(
                session,
                "apply_patch",
                {
                    "workspace_id": workspace_id,
                    "patch": valid_patch,
                    "explanation": "create marker file for phase2 test",
                },
            )
            assert patched.get("applied") is True, patched
            changed = patched.get("changed_files", [])
            assert "phase2_applied_marker.txt" in changed, changed
            # git_status should be present even if diff_stat is empty (new file).
            assert patched.get("git_status", ""), "git_status should be non-empty"
            print("[ok] valid new-file patch applied successfully")

            # Verify the file was actually created.
            created_file = worktree_path / "phase2_applied_marker.txt"
            assert created_file.is_file(), f"patched file not found: {created_file}"
            content = created_file.read_text(encoding="utf-8")
            assert "phase2 patch applied successfully" in content, content
            print("[ok] patched file content verified")

            # 4. Apply a second patch that modifies an existing tracked file.
            # We'll modify the newly created file (stage it first so it's tracked,
            # then apply a patch against the tracked version).
            subprocess.run(
                ["git", "add", "phase2_applied_marker.txt"],
                cwd=worktree_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "stage marker for modification test"],
                cwd=worktree_path,
                check=True,
                capture_output=True,
            )

            modify_patch = (
                "--- a/phase2_applied_marker.txt\n"
                "+++ b/phase2_applied_marker.txt\n"
                "@@ -1 +1,2 @@\n"
                " phase2 patch applied successfully\n"
                "+added second line via patch\n"
            )
            modified = await _call_tool(
                session,
                "apply_patch",
                {
                    "workspace_id": workspace_id,
                    "patch": modify_patch,
                    "explanation": "modify marker file to verify diff_stat",
                },
            )
            assert modified.get("applied") is True, modified
            changed2 = modified.get("changed_files", [])
            assert "phase2_applied_marker.txt" in changed2, changed2
            diff_stat = modified.get("diff_stat", "")
            assert diff_stat, (
                f"diff_stat should be non-empty when modifying a tracked file, got: {diff_stat!r}"
            )
            assert "1 file changed" in diff_stat or "1 insertion" in diff_stat, diff_stat
            print("[ok] tracked-file patch applied, diff_stat is present")

            # Verify the combined content.
            content2 = created_file.read_text(encoding="utf-8")
            assert "added second line" in content2, content2
            print("[ok] modified file content verified")

            # 5. Apply an invalid patch (syntax error) — must be rejected.
            bad_patch = "--- a/foo.py\n+++ b/foo.py\n@@ -this is not valid @@\n-old\n+new\n"
            rejected = await _call_tool(
                session,
                "apply_patch",
                {
                    "workspace_id": workspace_id,
                    "patch": bad_patch,
                    "explanation": "should fail",
                },
            )
            assert rejected.get("applied") is False, (
                f"invalid patch should have been rejected: {rejected}"
            )
            assert "error" in rejected, rejected
            print("[ok] invalid patch correctly rejected")

            # 6. Patch with absolute path — must be rejected.
            abs_patch = "--- a/foo.py\n+++ C:/Windows/evil.py\n@@ -1 +1 @@\n-old\n+new\n"
            abs_rejected = await _call_tool(
                session,
                "apply_patch",
                {
                    "workspace_id": workspace_id,
                    "patch": abs_patch,
                    "explanation": "should fail",
                },
            )
            assert abs_rejected.get("applied") is False, (
                f"absolute path patch should have been rejected: {abs_rejected}"
            )
            assert "error" in abs_rejected, abs_rejected
            print("[ok] absolute path patch correctly rejected")

            # 7. Patch with path traversal — must be rejected.
            traversal_patch = (
                "--- a/../../etc/passwd\n+++ b/../../etc/shadow\n@@ -1 +1 @@\n-old\n+new\n"
            )
            trav_rejected = await _call_tool(
                session,
                "apply_patch",
                {
                    "workspace_id": workspace_id,
                    "patch": traversal_patch,
                    "explanation": "should fail",
                },
            )
            assert trav_rejected.get("applied") is False, (
                f"traversal patch should have been rejected: {trav_rejected}"
            )
            assert "error" in trav_rejected, trav_rejected
            print("[ok] path traversal patch correctly rejected")

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

    print("\n[ok] All Phase 2 acceptance tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
