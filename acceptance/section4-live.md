# Section 4 live ChatGPT acceptance

This check proves that ChatGPT can use all seven symbol-level code-intelligence
tools through the real Secure MCP Tunnel. It creates no source changes. A passing
workspace is discarded; a failing workspace is retained for diagnosis.

Latest result: **FAIL** — the direct MCP preflight passed, but ChatGPT retained a
workspace because its installed `gpt-local` app exposed the cached pre-Section-4
tool schemas. The app management refresh request also failed.

## Automated live preflight

```powershell
.\scripts\accept-section4-live.ps1
```

## Exact ChatGPT prompt

Paste the following into a fresh chat with the `gpt-local` app enabled:

```text
Run the Section 4 live code-intelligence acceptance using only the gpt-local app.
Do not use web search, shell commands, file writes, patches, commits, merges, pushes,
or deployments.

1. Call get_capabilities and confirm supports_code_intelligence is true.
2. Call list_projects, then create one detached workspace for project Gpt-Local with
   task_name "section4-live-chatgpt". Record its workspace_id and worktree_path.
3. In that workspace call every tool below directly and verify the stated evidence:
   - list_symbols(path="app/services/process_recovery.py"): recover_processes exists.
   - find_definition(symbol="recover_processes",
     path="app/services/process_recovery.py"): the definition path and line are returned.
   - find_references(symbol="recover_processes", path="app"): at least two references.
   - find_implementations(symbol="Enum",
     path="app/services/process_scheduler.py"): AccessMode is returned.
   - get_call_hierarchy(symbol="recover_processes",
     path="app/services/process_recovery.py"): the definition plus outgoing _terminal
     and _now_iso are returned.
   - get_diagnostics(path="app/services/process_recovery.py"): exactly one file is
     checked and there are zero diagnostics.
   - get_changed_symbols(base="HEAD~1", head="HEAD"): recover_processes is returned
     and app/services/process_recovery.py is listed as changed.
4. Call git_status and verify there are no dirty working-tree entries.
5. If every assertion passed, call discard_workspace and confirm removed_path=true.
   If anything failed, do not discard the workspace.
6. Return a compact PASS/FAIL table with one row per required tool, concrete evidence
   from its response, the workspace ID/path, and the cleanup result. Do not claim PASS
   for a tool you did not call.
```

## Pass/fail matrix

| Requirement | Pass condition | Result | Evidence |
|---|---|---|---|
| Capability | `supports_code_intelligence=true` | PASS | ChatGPT observed `true`; server 0.2.0 advertised all seven names |
| `list_symbols` | `recover_processes` returned | FAIL | Advertised by the server but absent from ChatGPT's callable schema |
| `find_definition` | Correct path and line returned | FAIL | Not exposed to ChatGPT for invocation |
| `find_references` | Count is at least 2 | FAIL | Not exposed to ChatGPT for invocation |
| `find_implementations` | `AccessMode` returned | FAIL | Not exposed to ChatGPT for invocation |
| `get_call_hierarchy` | `_terminal` and `_now_iso` outgoing | FAIL | Not exposed to ChatGPT for invocation |
| `get_diagnostics` | One file, zero diagnostics | FAIL | Not exposed to ChatGPT for invocation |
| `get_changed_symbols` | Recovery file and symbol returned | FAIL | Not exposed to ChatGPT for invocation |
| Git isolation | Workspace is clean | PASS | `## HEAD (no branch)` with no dirty entries |
| Cleanup | Passing workspace removed; failing workspace retained | PASS | Failed workspace `ws-c63c4b58` remains active and clean |

## Run evidence

- Timestamp (Asia/Shanghai): 2026-07-22 17:17:13 +08:00
- ChatGPT task URL: https://chatgpt.com/c/6a6087d9-e7f4-83ee-83a1-c64ddfdf9c71
- Workspace ID: `ws-c63c4b58`
- Worktree path: `E:\GPTWorktrees\gpt-local\ws-c63c4b58-section4-live-chatgpt`
- Automated live preflight: PASS; `1 passed`; workspace `ws-b71bf974` discarded
- ChatGPT result: FAIL; capability, project discovery, workspace creation, and Git
  isolation passed, but none of the seven tools was callable from the cached app schema
- Cleanup result: PASS for failure policy; workspace retained and independently clean
- Final MCP state: `healthy`
- Final tunnel state: `healthy`

## Abnormal behavior

Record every abnormal event, including transient network failures and browser retry
states. Do not include credentials or secret values.

### Recorded events

1. At 16:52 +08:00, the first direct run could not create a workspace because eight
   active `Gpt-Local` records reached the configured limit. Six records referenced
   missing, unregistered worktrees; no existing record was deleted. The limit was
   temporarily raised from 8 to 9 and restored to 8 after acceptance.
2. Requests to ChatGPT's telemetry endpoint repeatedly timed out. Page loading and
   the submitted ChatGPT task still completed, but browser interactions were slow and
   several control sessions timed out.
3. ChatGPT created `ws-c63c4b58` and confirmed a clean detached worktree, then reported
   that its callable connector schema omitted all seven newly advertised operations.
   It correctly returned FAIL and retained the workspace.
4. The installed development app's management page showed the cached older schemas.
   Two attempts to use its explicit Refresh action failed with a network request error;
   the schema remained unchanged. No uninstall, reinstallation, or credential change
   was attempted.

```text
timestamp:
scenario:
observed behavior:
expected behavior:
reproduction steps:
sanitized evidence:
acceptance impact:
workaround or fix:
final disposition:
```
