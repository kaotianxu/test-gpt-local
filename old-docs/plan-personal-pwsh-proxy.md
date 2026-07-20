# Modification Plan: P0 & P1 Improvements

Based on `modification_comment.md` analysis of the current codebase (Phase 4 complete).

## Context

The project is a Local Code MCP Operator that lets ChatGPT drive code operations via OpenAI Secure MCP Tunnel. The current implementation covers Phases 0-4 (tunnel connectivity, workspace/read/search, patch application, PowerShell execution, check shortcuts). The modification_comment identifies improvements for production readiness: structured error handling, optimistic concurrency, reduced round-trips, artifact storage, idempotency, project discovery, and capabilities negotiation.

## Implementation Order

Each item builds on the previous one. Tests are updated incrementally.

---

### 1. Response Envelope Helper (`app/services/envelope.py`) — P0.1

Create a unified response envelope that all tools use.

**Success envelope:**
```python
{
  "ok": True,
  "request_id": "req_abc123",
  "workspace_id": "ws_ab12",
  "revision": 7,
  "result": { ... },
  "warnings": [],
  "truncated": False,
  "next_cursor": None,
}
```

**Error envelope:**
```python
{
  "ok": False,
  "error": {
    "code": "PATCH_CONFLICT",
    "message": "Target file changed since it was read.",
    "retryable": True,
    "suggested_next_tool": "read_files",
  }
}
```

**Stable error codes:**
`WORKSPACE_NOT_FOUND`, `STALE_WORKSPACE`, `FILE_CHANGED`, `PATCH_CONFLICT`, `PATH_DENIED`, `PROCESS_TIMEOUT`, `PROCESS_CANCELLED`, `OUTPUT_TRUNCATED`, `CHECK_FAILED`, `TOOL_RETRYABLE`, `PROJECT_NOT_FOUND`, `CHECK_NOT_FOUND`, `INVALID_INPUT`, `INTERNAL_ERROR`, `RATE_LIMITED`, `NOT_IMPLEMENTED`

**Helper functions:**
- `ok_result(result, workspace_id=None, request_id=None, revision=None, warnings=None, truncated=False, next_cursor=None)` → dict
- `error_result(code, message, retryable=False, suggested_next_tool=None, workspace_id=None, request_id=None)` → dict
- `generate_request_id()` → str

**File:** `app/services/envelope.py` (new)

---

### 2. Idempotency Store (`app/storage/idempotency.py`) — P0.2 / P1.7

Add idempotency key deduplication for mutation tools.

**Database table:**
```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key  TEXT PRIMARY KEY,
    tool_name        TEXT NOT NULL,
    input_hash       TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
```

**Helper functions:**
- `get_idempotent_result(key, tool_name, input_hash)` → dict | None
- `store_idempotent_result(key, tool_name, input_hash, result_json)` → None
- `expire_old_keys(hours=24)` → None

**File:** `app/storage/idempotency.py` (new)

---

### 3. Capabilities Tool (`app/tools/capabilities.py`) — P1.10

Add a `get_capabilities` tool that returns server metadata.

**Fields:**
```python
{
  "schema_version": "1.0.0",
  "server_version": "0.2.0",
  "server_name": "gpt-local-code-operator",
  "capabilities": {
    "supports_async_process": True,
    "supports_expected_hash": True,
    "supports_idempotency": True,
    "supports_artifacts": True,
    "supports_multi_query_search": True,
    "supports_diff_context_lines": True,
    "supports_diff_stat_only": True,
    "supports_project_manifest": True,
    "supports_read_process_output": True,
  },
  "max_read_chars": 100000,
  "max_output_chars": 200000,
  "max_timeout_seconds": 3600,
  "max_active_workspaces_per_project": 8,
}
```

**File:** `app/tools/capabilities.py` (new)
**Register in:** `app/server.py`

---

### 4. Git Diff Enhancements (`app/tools/git_tools.py`) — P0.3

Enhance `git_diff` with additional parameters:
- `context_lines: int = 3` — number of context lines in diff output (passed as `-U<lines>`)
- `stat_only: bool = False` — when True, only return `--stat` output
- `cached: bool = False` — already exists, keep it
- `paths: list[str] | None = None` — already exists, keep it

This reduces round-trips: GPT no longer needs separate calls for stat vs full diff.

---

### 5. Search Code Multi-Query (`app/tools/search.py`) — P0.3

Add a `multi_query` parameter to `search_code`:
- `queries: list[str]` — when provided, run multiple searches and aggregate results grouped by file
- Still supports single `query` for backward compatibility
- Results are returned with a `query_group` field so GPT can tell which result came from which query

This reduces round-trips: GPT can search for `submit_order`, `duplicate`, and `order_id` in one call instead of three.

---

### 6. Project Manifest Discovery (`app/services/workspace_manager.py`) — P1.9

Add `project_manifest` to `create_workspace` response. The manifest is discovered by scanning the repository for well-known files:

```python
{
  "project_manifest": {
    "languages": ["python"],
    "instructions": ["AGENTS.md", "CONTRIBUTING.md"],
    "test_commands": ["unit_tests", "lint"],
    "package_manager": "uv",  # or pip, npm, cargo, etc.
    "entrypoints": ["src/app.py"],
    "git_head": "abc123...",
    "project_config_files": ["pyproject.toml", "package.json"],
  }
}
```

**Discovery logic:**
- Check for `AGENTS.md`, `CONTRIBUTING.md`, `README.md` → `instructions`
- Check for `pyproject.toml` → package_manager = "uv" or "pip", language = "python"
- Check for `package.json` → package_manager = "npm", language = "javascript/typescript"
- Check for `Cargo.toml` → package_manager = "cargo", language = "rust"
- Check for `Makefile` → extra build system
- Check for `*.sln`, `*.csproj` → language = "csharp"
- Detect main entrypoints by scanning for `if __name__ == "__main__"` or `def main()` patterns

---

### 7. Read Process Output Tool (`app/tools/powershell.py`) — P0.4

Add a `read_process_output` tool that lets GPT read specific segments of a process's output files:

```python
read_process_output(
    process_id: str,
    stream: str = "stdout",  # "stdout" or "stderr"
    offset: int = 0,
    max_chars: int = 50000,
) -> {
    "process_id": str,
    "stream": str,
    "offset": int,
    "content": str,
    "total_chars": int,
    "truncated": bool,
}
```

This allows the model to read specific parts of a long build log without loading the entire output into context.

---

### 8. Structured Output Summary (`app/services/process_manager.py`) — P0.4

Add structured test result parsing to `run_pwsh` and `run_check` results. When a check completes, try to parse the output for test results:

```python
{
  "exit_code": 1,
  "duration_ms": 18342,
  "stdout_tail": "...",
  "stderr_tail": "...",
  "output_artifact_id": "proc_abc123_full_log",
  "summary": {
    "tests_passed": 84,
    "tests_failed": 2,
    "failed_tests": [
      "tests/test_order.py::test_duplicate_submit"
    ],
    "parse_method": "pytest",  # or "generic"
  },
  "truncated": True,
}
```

**Parsing logic:**
- Detect pytest output: look for `FAILED ` lines and `=== N passed, M failed ===` pattern
- Detect generic: fallback to counting "PASS" / "FAIL" / "ERROR" pattern
- Return `parse_method: "generic"` when pytest-specific parsing fails

---

### 9. Output Artifact ID (`app/services/process_manager.py`) — P0.4

Each process already writes stdout/stderr to files. Add an `output_artifact_id` field that is the same as `process_id` — this lets GPT reference it in `read_process_output`. The `_build_result` method already stores `stdout_path` and `stderr_path` in the DB; we just need to expose the artifact ID in the response.

---

### 10. Wrap All Tools with Envelope (`app/tools/*.py`) — P0.1

Update every tool in `app/tools/` to wrap its return value with the envelope. Target files:

- `projects.py` — ping, list_projects
- `workspaces.py` — create_workspace, get_workspace, list_workspaces, discard_workspace
- `repo_map.py` — get_repo_map
- `search.py` — search_code
- `reader.py` — read_files
- `patcher.py` — apply_patch, replace_text
- `powershell.py` — run_pwsh, get_process_result, cancel_process, read_process_output
- `checks.py` — list_checks, run_check
- `git_tools.py` — git_status, git_diff
- `reports.py` — get_project_status, get_workspace_report
- `capabilities.py` — get_capabilities

**Pattern:** Each tool's Python function returns a dict. At the end of the function, pass it through `envelope.ok_result()` or `envelope.error_result()`.

---

### 11. Add Idempotency to Mutation Tools — P0.2 / P1.7

Add `idempotency_key` parameter to these tools:
- `create_workspace` — in `workspaces.py`
- `apply_patch` — in `patcher.py`
- `replace_text` — in `patcher.py`
- `discard_workspace` — in `workspaces.py`
- `run_pwsh` — in `powershell.py`
- `run_check` — in `checks.py`
- `cancel_process` — in `powershell.py`

**Behavior:**
1. Check if `idempotency_key` is provided
2. Look up in idempotency store
3. If found and input_hash matches, return stored result
4. If found and input_hash differs, return error with `IDEMPOTENCY_KEY_MISMATCH`
5. If not found, execute normally and store result

---

### 12. Add `expected_head` to `apply_patch` — P0.2

Add `expected_head: str | None` parameter to `apply_patch`. When provided, run `git rev-parse HEAD` in the worktree and reject if the HEAD doesn't match. This prevents applying patches based on stale workspace state.

Already partially implemented: `expected_sha256` per-file hash check exists. Adding `expected_head` is the next level.

---

### 13. Update `app/storage/database.py` — P1.6

Add workspace revision counter and enhanced tracking fields:

```sql
ALTER TABLE workspaces ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE workspaces ADD COLUMN current_head TEXT;
ALTER TABLE workspaces ADD COLUMN last_patch_at TEXT;
ALTER TABLE workspaces ADD COLUMN last_check TEXT;
ALTER TABLE workspaces ADD COLUMN changed_files TEXT;  -- JSON array
```

Helper functions:
- `increment_workspace_revision(workspace_id)` → int
- `update_workspace_head(workspace_id, head)` → None
- `update_workspace_metadata(workspace_id, **kwargs)` → None

---

### 14. Update `app/server.py` — Registration

Register new tools:
- `capabilities.register_tools(mcp)` — after projects
- Ensure `read_process_output` is registered in powershell module

---

### 15. Update Tests

- **Unit tests:**
  - `tests/unit/test_envelope.py` — new: test envelope helpers
  - `tests/unit/test_idempotency.py` — new: test idempotency store
  - `tests/unit/test_capabilities.py` — new: test capabilities
  - Update existing tests to handle envelope wrapping

- **Integration tests:**
  - `tests/integration/test_phase4.py` — update to use envelope-aware response parsing
  - `tests/integration/test_phase4_resilience.py` — update similarly
  - Create `tests/integration/test_modifications.py` — new: test idempotency, read_process_output, project_manifest, capabilities

---

## Verification

1. Run unit tests: `python -m pytest tests/unit/ -q`
2. Start MCP server: `python -m app.server` (in background)
3. Run integration tests: `python -m pytest tests/integration/ -q`
4. Manual verification path:
   - Call `get_capabilities` → verify all fields present
   - Call `create_workspace` → verify `project_manifest` in response
   - Apply patch with `idempotency_key` → verify
   - Apply same patch with same key → verify same result returned
   - Apply patch with `expected_head` → verify HEAD check works
   - Call `run_pwsh` with long output → verify `output_artifact_id` present
   - Call `read_process_output` → verify stream reading
   - Call `git_diff` with `stat_only=True` → verify only stat returned
   - Call `search_code` with `queries` → verify multi-query aggregation
5. Run `get_workspace_report` → verify `revision`, `current_head`, `last_check` fields