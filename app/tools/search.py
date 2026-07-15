"""MCP tool: search_code.

Wraps ripgrep (``rg --json``) so GPT can locate symbols, strings, and
call chains. Results are returned in a compact form: one record per
match, with path, line number, and a small context window.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.services.envelope import error_result, ok_result
from app.services.path_guard import is_denied, resolve_within
from app.services.workspace_manager import get_workspace

log = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 5 * 1024 * 1024  # 5 MB hard cap on rg output.
_DEFAULT_MAX_RESULTS = 100
_HARD_MAX_RESULTS = 1000


def _resolve_rg() -> str:
    rg = shutil.which("rg")
    if rg is None:
        raise RuntimeError(
            "ripgrep (rg) is not installed or not on PATH. "
            "Install it from https://github.com/BurntSushi/ripgrep."
        )
    return rg


def _validate_query(query: str) -> str:
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    return query


def _validate_path(worktree: Path, path: str) -> Path:
    if not path:
        return worktree
    candidate = resolve_within(worktree, path)
    if is_denied(candidate, worktree):
        raise ValueError("path is denied by policy")
    return candidate


def _search(
    workspace_id: str,
    query: str = "",
    path: str = "",
    globs: list[str] | None = None,
    context_lines: int = 2,
    max_results: int = _DEFAULT_MAX_RESULTS,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    record = get_workspace(workspace_id)
    if record is None:
        return {"error": f"workspace not found: {workspace_id}", "workspace_id": workspace_id}
    worktree = Path(record["worktree_path"])

    # Multi-query mode: run several searches and aggregate results.
    if queries and len(queries) > 1:
        return _search_multi(workspace_id, worktree, queries, path, globs, context_lines, max_results)

    try:
        q = _validate_query(query) if query else ""
        target = _validate_path(worktree, path)
    except ValueError as exc:
        return {"error": str(exc), "workspace_id": workspace_id, "query": query}

    if not q:
        return {"error": "query or queries must be provided", "workspace_id": workspace_id}

    context_lines = max(0, min(int(context_lines), 10))
    if max_results <= 0:
        max_results = _DEFAULT_MAX_RESULTS
    max_results = min(max_results, _HARD_MAX_RESULTS)

    rg = _resolve_rg()
    cmd = [
        rg,
        "--json",
        "--line-number",
        "--no-heading",
        f"--context={context_lines}",
    ]
    for pattern in globs or []:
        if not pattern:
            continue
        cmd.extend(["--glob", pattern])
    cmd.extend(["--", q, str(target)])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {
            "workspace_id": workspace_id,
            "query": q,
            "path": path or ".",
            "error": "ripgrep timed out after 30s",
        }

    # rg exits 1 when no matches are found, 0 when matches are found,
    # 2 on real errors. Map all to a structured result.
    if proc.returncode not in (0, 1) and not proc.stdout:
        return {
            "workspace_id": workspace_id,
            "query": q,
            "path": path or ".",
            "error": (proc.stderr or "").strip() or f"rg exited {proc.returncode}",
        }

    raw_matches: list[dict[str, Any]] = []
    contexts: dict[str, dict[int, str]] = {}
    match_lines: dict[str, set[int]] = {}
    out = proc.stdout[:_MAX_OUTPUT_BYTES]

    # Group rg JSON events by file. rg emits one `end` event per file, not
    # per match, so each match must be retained as it arrives.
    worktree_str = str(worktree).rstrip("\\/").lower()

    def relative_path(raw_path: str) -> str:
        if not raw_path:
            return ""
        rp = raw_path.replace("/", "\\")
        if rp.lower().startswith(worktree_str + "\\"):
            return rp[len(worktree_str) + 1 :]
        if rp.lower() == worktree_str:
            return "."
        return raw_path

    for line in out.splitlines():
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        data = event.get("data", {}) or {}
        if etype == "match":
            rel = relative_path(data.get("path", {}).get("text", ""))
            line_number = data.get("line_number")
            raw_matches.append(
                {
                    "path": rel,
                    "line_number": line_number,
                    "line": (data.get("lines", {}) or {}).get("text", "").rstrip("\r\n"),
                    "submatches": [
                        {
                            "match": sm.get("match", {}).get("text", ""),
                            "start": sm.get("start"),
                            "end": sm.get("end"),
                        }
                        for sm in data.get("submatches", [])
                    ],
                }
            )
            if isinstance(line_number, int):
                match_lines.setdefault(rel, set()).add(line_number)
        elif etype == "context":
            rel = relative_path(data.get("path", {}).get("text", ""))
            ctx_line = (data.get("lines", {}) or {}).get("text", "").rstrip("\r\n")
            line_no = data.get("line_number")
            if isinstance(line_no, int):
                contexts.setdefault(rel, {})[line_no] = ctx_line

    for match in raw_matches:
        rel = str(match["path"])
        line_number = match["line_number"]
        before: list[list[object]] = []
        after: list[list[object]] = []
        if isinstance(line_number, int):
            for line_no, ctx_line in sorted(contexts.get(rel, {}).items()):
                if line_no in match_lines.get(rel, set()):
                    continue
                if line_number - context_lines <= line_no < line_number:
                    before.append([line_no, ctx_line])
                elif line_number < line_no <= line_number + context_lines:
                    after.append([line_no, ctx_line])
        match["context_before"] = before
        match["context_after"] = after

    truncated = len(raw_matches) > max_results
    matches = raw_matches[:max_results]

    return ok_result(
        {
            "workspace_id": workspace_id,
            "query": q,
            "path": path or ".",
            "context_lines": context_lines,
            "match_count": len(matches),
            "truncated": truncated,
            "matches": matches,
        },
        workspace_id=workspace_id,
    )


def _search_multi(
    workspace_id: str,
    worktree: Path,
    queries: list[str],
    path: str,
    globs: list[str] | None,
    context_lines: int,
    max_results: int,
) -> dict[str, Any]:
    """Run multiple searches and aggregate results grouped by query."""
    per_query_max = max(1, max_results // max(len(queries), 1))
    aggregated: list[dict[str, Any]] = []
    total_count = 0
    truncated = False
    errors: list[str] = []

    for q in queries:
        single = _search(
            workspace_id=workspace_id,
            query=q,
            path=path,
            globs=globs,
            context_lines=context_lines,
            max_results=per_query_max,
        )
        # Check for envelope-wrapped error.
        if not single.get("ok", True):
            errors.append(f"{q!r}: {single.get('error', {}).get('message', 'unknown error')}")
            continue
        result = single.get("result", single)
        for match in result.get("matches", []):
            match["query_group"] = q
            aggregated.append(match)
        total_count += result.get("match_count", 0)
        if result.get("truncated"):
            truncated = True

    # Limit total results.
    if len(aggregated) > max_results:
        aggregated = aggregated[:max_results]
        truncated = True

    result: dict[str, Any] = {
        "workspace_id": workspace_id,
        "queries": queries,
        "path": path or ".",
        "context_lines": context_lines,
        "match_count": len(aggregated),
        "total_matches": total_count,
        "truncated": truncated,
        "matches": aggregated,
    }
    if errors:
        result["query_errors"] = errors
    return ok_result(result, workspace_id=workspace_id)


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="search_code",
        description=(
            "Search for a literal string or simple regex inside a workspace "
            "using ripgrep. Returns up to max_results matches with a small "
            "context window. Useful for locating call sites and definitions.\n\n"
            "**Single query:** pass ``query`` (string) for a single search.\n"
            "**Multi-query:** pass ``queries`` (list of strings) to run several "
            "searches in one call. Each result includes a ``query_group`` field "
            "indicating which query matched it."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def search_code(
        workspace_id: str,
        query: str = "",
        path: str = "",
        globs: list[str] | None = None,
        context_lines: int = 2,
        max_results: int = _DEFAULT_MAX_RESULTS,
        queries: list[str] | None = None,
    ) -> dict[str, object]:
        log.info(
            "search_code workspace_id=%s query=%r queries=%s path=%s globs=%s",
            workspace_id,
            query,
            queries,
            path,
            globs,
        )
        return _search(
            workspace_id=workspace_id,
            query=query,
            path=path,
            globs=globs,
            context_lines=context_lines,
            max_results=max_results,
            queries=queries,
        )
