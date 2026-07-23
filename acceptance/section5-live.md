# Section 5 live ChatGPT acceptance

This acceptance proves that ChatGPT can consume the durable event stream through
the real MCP connection without repeatedly polling `get_process_result`. It also
checks lifecycle ordering, long polling, UTF-8 byte cursors, check completion,
artifact notification, workspace isolation, and cleanup.

## Automated acceptance

```powershell
python scripts/accept-iteration.py --section 5
python -m pytest -q tests/unit/test_event_store.py tests/unit/test_events_tool.py
python -m pytest -q tests/unit/test_process_output_cursor.py
python -m pytest -q tests/integration/test_event_stream.py
python -m ruff check app tests
python -m mypy app
python -m pytest -q
```

The live test should only be attempted after these commands pass and the MCP
service/app schema has been refreshed to expose `get_events` and
`subscribe_process`.

## Exact live acceptance prompt

Paste the following into a fresh ChatGPT conversation with the `gpt-local` app
enabled:

```text
Run the Section 5 durable-event-stream live acceptance using only the gpt-local
app. Do not use web search, local shell/computer tools, manual sleeps, file
patches, commits, pushes, deployments, or repeated get_process_result polling.
All waiting for asynchronous work must use get_events or subscribe_process with
the returned opaque cursor.

1. Call get_capabilities. Require all of the following:
   - supports_event_stream = true
   - supports_event_long_poll = true
   - supports_async_process = true
   - supports_read_process_output = true
   Record the event retention, page-size, wait-time, and waiter limits.

2. Call list_projects, then create one detached workspace for project
   "Gpt-Local" with task_name "section5-live-chatgpt". Record workspace_id and
   worktree_path.

3. Establish the event starting point before starting any command:
   call get_events(workspace_id=<workspace>, cursor=null, wait_seconds=0).
   Save result.cursor as workspace_cursor_0. The call must succeed even when
   events is empty.

4. Start this command asynchronously:
   run_command(
     workspace_id=<workspace>,
     shell="python",
     command="import time; print('section5-alpha', flush=True); time.sleep(2); print('中文-section5-omega', flush=True)",
     tty=false,
     wait=false,
     timeout_seconds=30
   )
   Record process_id. Do not call get_process_result to wait for it.

5. Consume this process only through
   subscribe_process(process_id=<process>, cursor=workspace_cursor_0,
   wait_seconds=10, limit=100). If has_more is true or process.exited has not
   appeared, repeat subscribe_process using exactly the cursor returned by the
   previous response. Stop after process.exited or fail after 6 calls.

6. Verify concrete event-stream evidence:
   - this process has exactly one tool.queued event;
   - exactly one tool.started event;
   - at least one process.output event for stdout;
   - exactly one process.exited event with payload.status="passed" and
     payload.exit_code=0;
   - event_id values strictly increase;
   - non-null process sequence values are exactly 1,2,3,... with no duplicate
     or gap;
   - every event has the requested workspace_id and process_id;
   - no payload contains the command text, environment-variable values,
     authorization/cookie/API-key data, or an absolute workspace path.

7. Read stdout without using a character offset:
   - call read_process_output(process_id=<process>, stream="stdout", offset=0,
     max_chars=16);
   - while next_cursor is non-null, call it again using cursor=<next_cursor>;
   - concatenate all content and require both "section5-alpha" and
     "中文-section5-omega";
   - require offset_unit="bytes" on returned non-empty pages;
   - require that no page starts or ends with a replacement character caused by
     a split UTF-8 code point.

8. Verify bounded long-poll timeout semantics. First call
   get_events(workspace_id=<workspace>, cursor=null, wait_seconds=0) and save
   its cursor as quiet_cursor. Then call
   subscribe_process(process_id=<process>, cursor=quiet_cursor,
   event_types=["tool.started"], wait_seconds=1). Require a successful response
   with events=[] and timed_out=true; timeout is not an error.

9. Verify check events:
   - capture a new workspace tail cursor with get_events(cursor=null);
   - call run_check(workspace_id=<workspace>,
     check_id="section5_acceptance", wait=false);
   - use subscribe_process with that check process_id and captured cursor until
     terminal, never get_process_result;
   - require tool.queued, tool.started, process.exited(status="passed"), and
     check.completed(check_id="section5_acceptance", status="passed");
   - require exactly one process.exited event.

10. Continue consuming events for the command/check processes long enough to
    observe any artifact.created events. For every artifact event, require a
    non-empty artifact_id, kind, relative_path, and non-negative size_bytes;
    relative_path must not be absolute. If no artifact event is returned,
    report this as FAIL rather than silently skipping the assertion.

11. Call git_status and require that the disposable workspace has no dirty
    working-tree entries. If every assertion above passed, call
    discard_workspace and require removed_path=true. If anything failed, retain
    the workspace for diagnosis.

12. Return a compact PASS/FAIL table with one row for:
    capabilities, initial cursor, lifecycle ordering, sequence continuity,
    long-poll behavior, terminal uniqueness, UTF-8 output pagination,
    timeout semantics, check.completed, artifact.created, payload redaction,
    Git isolation, and cleanup. Include the workspace ID/path, both process IDs,
    the first and final event cursors, event IDs/types/sequences, output cursors,
    and sanitized failure evidence. Do not claim PASS for an operation you did
    not call or evidence you did not inspect.
```

## Pass/fail record

Fill this after the live run:

| Requirement | Result | Sanitized evidence |
|---|---|---|
| Capabilities | PENDING | |
| Initial cursor | PENDING | |
| Lifecycle ordering | PENDING | |
| Sequence continuity | PENDING | |
| Long polling | PENDING | |
| Terminal uniqueness | PENDING | |
| UTF-8 output pagination | PENDING | |
| Timeout semantics | PENDING | |
| `check.completed` | PENDING | |
| `artifact.created` | PENDING | |
| Payload redaction | PENDING | |
| Git isolation | PENDING | |
| Cleanup | PENDING | |

