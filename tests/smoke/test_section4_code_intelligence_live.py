"""Opt-in live acceptance for Section 4 code-intelligence MCP tools.

Enable explicitly with ``SECTION4_LIVE=1``.  A successful run discards its
detached workspace.  A failed run prints and retains the workspace so its
state remains available for diagnosis.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession

from tests.smoke.test_mcp_smoke import (
    _call_capabilities,
    _call_ok,
    _open_mcp_session,
)

SECTION4_TOOLS = {
    "list_symbols",
    "find_definition",
    "find_references",
    "find_implementations",
    "get_call_hierarchy",
    "get_diagnostics",
    "get_changed_symbols",
}
SOURCE_PATH = "app/services/process_recovery.py"
SCHEDULER_PATH = "app/services/process_scheduler.py"


def _enabled() -> bool:
    return os.environ.get("SECTION4_LIVE", "").lower() in {"1", "true", "yes"}


def _qualified_names(items: object) -> set[str]:
    assert isinstance(items, list), items
    return {
        str(item["qualified_name"])
        for item in items
        if isinstance(item, dict) and "qualified_name" in item
    }


def _assert_clean(status: dict[str, Any]) -> None:
    assert status.get("exit_code") == 0, status
    stdout = str(status.get("stdout", ""))
    dirty_lines = [line for line in stdout.splitlines() if line and not line.startswith("##")]
    assert dirty_lines == [], status


async def _exercise_tools(session: ClientSession, workspace_id: str) -> None:
    symbols = await _call_ok(
        session,
        "list_symbols",
        {"workspace_id": workspace_id, "path": SOURCE_PATH},
    )
    assert "recover_processes" in _qualified_names(symbols.get("symbols")), symbols

    definitions = await _call_ok(
        session,
        "find_definition",
        {
            "workspace_id": workspace_id,
            "path": SOURCE_PATH,
            "symbol": "recover_processes",
        },
    )
    definition_names = _qualified_names(definitions.get("definitions"))
    assert definition_names == {"recover_processes"}, definitions

    references = await _call_ok(
        session,
        "find_references",
        {
            "workspace_id": workspace_id,
            "path": "app",
            "symbol": "recover_processes",
        },
    )
    assert int(references.get("count", 0)) >= 2, references

    implementations = await _call_ok(
        session,
        "find_implementations",
        {
            "workspace_id": workspace_id,
            "path": SCHEDULER_PATH,
            "symbol": "Enum",
        },
    )
    assert "AccessMode" in _qualified_names(implementations.get("implementations")), implementations

    hierarchy = await _call_ok(
        session,
        "get_call_hierarchy",
        {
            "workspace_id": workspace_id,
            "path": SOURCE_PATH,
            "symbol": "recover_processes",
        },
    )
    assert "recover_processes" in _qualified_names(hierarchy.get("definitions")), hierarchy
    outgoing = _qualified_names(hierarchy.get("outgoing"))
    assert {"_terminal", "_now_iso"} <= outgoing, hierarchy

    diagnostics = await _call_ok(
        session,
        "get_diagnostics",
        {"workspace_id": workspace_id, "path": SOURCE_PATH},
    )
    assert diagnostics.get("files_checked") == 1, diagnostics
    assert diagnostics.get("diagnostics") == [], diagnostics

    changed = await _call_ok(
        session,
        "get_changed_symbols",
        {
            "workspace_id": workspace_id,
            "base": "HEAD~1",
            "head": "HEAD",
        },
    )
    changed_names = _qualified_names(changed.get("symbols"))
    assert "recover_processes" in changed_names, changed
    assert SOURCE_PATH in changed.get("changed_files", []), changed

    _assert_clean(await _call_ok(session, "git_status", {"workspace_id": workspace_id}))


@pytest.mark.smoke
@pytest.mark.smoke_mutating
@pytest.mark.section4_live
@pytest.mark.asyncio
async def test_section4_code_intelligence_through_live_mcp() -> None:
    """Exercise every Section 4 tool through the running MCP endpoint."""
    if not _enabled():
        pytest.skip("set SECTION4_LIVE=1 to enable the Section 4 live acceptance")

    project_id = os.environ.get("SMOKE_PROJECT_ID", "Gpt-Local")
    suffix = uuid.uuid4().hex[:10]
    async with _open_mcp_session() as session:
        listed = await session.list_tools()
        advertised = {tool.name for tool in listed.tools}
        assert SECTION4_TOOLS <= advertised, sorted(SECTION4_TOOLS - advertised)

        capabilities = await _call_capabilities(session)
        flags = capabilities.get("capabilities")
        assert isinstance(flags, dict), capabilities
        assert flags.get("supports_code_intelligence") is True, capabilities

        created = await _call_ok(
            session,
            "create_workspace",
            {
                "project_id": project_id,
                "task_name": f"section4-live-{suffix}",
                "idempotency_key": f"section4-live-create-{suffix}",
            },
        )
        workspace_id = str(created["workspace_id"])
        worktree_path = Path(str(created["worktree_path"]))
        print(f"[section4-live] workspace_id={workspace_id} path={worktree_path}")
        assert worktree_path.is_dir(), created

        passed = False
        try:
            await _exercise_tools(session, workspace_id)
            passed = True
        finally:
            if passed:
                discarded = await _call_ok(
                    session,
                    "discard_workspace",
                    {
                        "workspace_id": workspace_id,
                        "idempotency_key": f"section4-live-discard-{suffix}",
                    },
                )
                assert discarded.get("removed_path") is True, discarded
                assert not worktree_path.exists(), worktree_path
                print(f"[section4-live] PASS; discarded {workspace_id}")
            else:
                print(
                    f"[section4-live] FAIL; retained {workspace_id} at {worktree_path}",
                    flush=True,
                )
