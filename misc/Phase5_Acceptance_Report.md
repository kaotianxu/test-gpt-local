# Phase 5 Acceptance Report

- Date: 2026-07-15 (Asia/Shanghai)
- Project: `gpt-local-code-operator`
- Plan: `Phase5_Implementation_Plan.md`
- Result: **PASS**
- Installed task: `gpt-local-code-operator`
- Operating model: hidden, limited-privilege, current-user Task Scheduler task

## Acceptance summary

Phase 5 replaces the two independent foreground processes with one background
supervisor. The supervisor owns one MCP child and, when its dependencies are ready,
one Secure MCP Tunnel child. It starts MCP first, requires MCP health and proxy
readiness before the tunnel, uses the tunnel `/readyz` endpoint for final health, and
restarts failed children with bounded exponential backoff.

The final installed runtime reported:

```text
state: healthy
heartbeat_current: true
process_running: true
MCP state: healthy
tunnel state: healthy
proxy ready: true
```

The exact owned process tree was:

```text
hidden pwsh service host
  `-- python -m app.service run
        |-- python -m app.server
        `-- tunnel-client run --profile local-code-operator
```

All four processes had `MainWindowHandle = 0`; no persistent terminal window was
visible.

## Automated checks

| Check | Result | Evidence |
|---|---:|---|
| Unit and integration suite | PASS | `108 passed`, warning-free |
| Ruff | PASS | `All checks passed!` |
| Strict mypy | PASS | `Success: no issues found in 27 source files` |
| Git whitespace check | PASS | `git diff --check` returned zero; only Windows line-ending notices were printed |
| PowerShell syntax | PASS | Every `scripts/*.ps1` file parsed without errors |
| Phase 5 live-process tests | PASS | Real child startup, delayed dependency, MCP failure, tunnel failure, backoff, recovery, lock, and exact stop |

Phase 5-specific automated coverage includes:

- Atomic status replacement while another thread continuously reads it.
- OS-backed single-instance locking.
- PID plus process-creation identity and exited-process detection.
- Bounded restart delay calculation.
- Delayed proxy behavior without blocking MCP health.
- Tunnel-only restart without MCP restart.
- MCP failure suspending and then restarting the dependent tunnel.
- Rotating component logs and runtime-key redaction.
- Interrupted database processes becoming `interrupted` rather than remaining active.
- Dirty/untracked worktree detection preventing automatic cleanup eligibility.
- MCP `ping` output schema accepting the structured response envelope.

## Live Windows acceptance

### A. Clean and idempotent installation

`scripts/install-service.ps1` installed exactly one task and started it. Re-running the
installer retained one task and one supervisor instance.

Verified task properties:

```text
task count: 1
trigger: MSFT_TaskLogonTrigger
run level: Limited
multiple instances: IgnoreNew
working directory: project root
host arguments: -NoProfile -NonInteractive -WindowStyle Hidden
```

The old `gpt-local-code-operator-mcp` and
`gpt-local-code-operator-tunnel` task names were absent.

### B. Delayed proxy

A temporary local relay port was configured but deliberately left unavailable during
startup. Observed state:

```text
state: waiting_for_proxy
MCP state: healthy
MCP PID: present
tunnel PID: null
proxy ready: false
```

Starting the relay, which forwarded to the real configured proxy, caused the same
supervisor to transition to:

```text
state: healthy
MCP PID: unchanged
tunnel PID: present
proxy ready: true
```

No service restart was required. The temporary relay was then stopped and the original
proxy configuration restored. No relay process remained.

### C. Tunnel crash recovery

The owned tunnel PID was terminated deliberately. The supervisor applied backoff and
started a replacement tunnel:

```text
old tunnel PID: 13968
new tunnel PID: 35880
tunnel restart_count: 1
MCP PID before/after: 38040
final state: healthy
```

The healthy MCP process was not restarted.

### D. MCP crash and dependency recovery

The owned MCP PID was terminated deliberately. The tunnel was observed leaving its old
PID while MCP was unhealthy. MCP then recovered first and the tunnel restarted after
the health check passed:

```text
old MCP PID: 38040
new MCP PID: 43712
old tunnel PID: 35880
new tunnel PID: 42080
dependency transition observed: true
final state: healthy
```

### E. Duplicate start and occupied port

A second direct `python -m app.service run` returned exit code `2` and did not create
children.

With a known fixture owning `127.0.0.1:8765`, the supervisor recorded MCP failures and
kept the tunnel stopped. It did not terminate the unrelated port owner. After that
fixture was explicitly removed, MCP and tunnel recovered automatically and the final
state became healthy.

### F. Stop, restart, uninstall, and reinstall

Graceful stop removed both children and returned `stopped`. Uninstall removed the
scheduled task while retaining:

- `data/operator.db`
- both registered workspaces
- logs
- configuration

The two retained workspace IDs before and after uninstall were:

```text
ws-b6cc2b8c
ws-d5d1610f
```

Reinstallation restored one healthy task. A second installation was successful and
did not create a duplicate supervisor or scheduled task.

### G. Logon action and persisted-state recovery

The task was verified to contain an `MSFT_TaskLogonTrigger` for the current user. To
avoid terminating the active Codex and browser acceptance session, a disruptive real
logoff/reboot was not performed. Instead, the exact registered task action was stopped
and launched through Task Scheduler, which exercises the same hidden action, principal,
working directory, and environment used by the logon trigger.

Observed after the scheduled launch:

```text
run_id changed: true
state: healthy
MCP state: healthy
tunnel state: healthy
workspace count: 2
trigger class: MSFT_TaskLogonTrigger
```

This is accepted as the non-disruptive logon-path test: Task Scheduler owns and
successfully executes the exact action, and the authoritative task definition contains
the required current-user logon trigger.

### H. ChatGPT through the Secure MCP Tunnel

The signed-in ChatGPT conversation was controlled through the built-in browser. The
prompt explicitly required read-only calls to:

```text
ping
list_projects
get_project_status(project_id="phase4-order-fixture")
```

After correcting the `ping` schema issue described below and restarting the background
service, ChatGPT repeated all three calls successfully:

| Tool | Result | Request ID |
|---|---:|---|
| `ping` | PASS, `ok: true` | `req_54a98c21370bf751` |
| `list_projects` | PASS, `ok: true` | `req_f313557d5df5fdc9` |
| `get_project_status` | PASS, `ok: true` | `req_2fb0ed09c239b5fd` |

ChatGPT reported:

```text
all three read-only calls succeed, and the tunnel/service is healthy
```

It also confirmed the fixture main working tree was clean and that the retained
workspace IDs remained `ws-b6cc2b8c` and `ws-d5d1610f`. No workspace was created,
modified, or discarded by the Phase 5 browser acceptance.

### I. Logs and secrets

Controlled MCP and tunnel failures were present in `logs/supervisor.log`, and component
output was present in `logs/mcp.log` and `logs/tunnel.log`. Unit coverage forced actual
size rotation and confirmed backup creation.

The runtime API-key value was loaded in memory and searched as an exact value across:

```text
logs/*.log*
data/service/*.json
```

Match count was zero. Status output and doctor output reported only that the key was
present; neither exposed its value.

## Recovery and cleanup evidence

Startup reconciliation reported both retained worktrees as:

```text
path_exists: true
project_registered: true
within_configured_root: true
git_registered: true
action: retained
```

Automatic expired-worktree deletion remains disabled by default. The implementation
requires expiry, an existing registered project, a path inside the configured root, a
valid Git worktree registration, and a completely clean status including untracked
files before opt-in deletion can occur. A workspace with a process interrupted during
startup is also ineligible during that reconciliation cycle. Automated coverage
verifies that an untracked file makes the worktree ineligible.

## Abnormal behavior log

### 1. Preflight doctor ran before MCP existed

- Observed: the first installer preflight failed only
  `mcp_server_reachable` and `oauth_metadata` because the service was stopped.
- Cause: standalone `tunnel-client doctor` expects the MCP target to exist, but the
  installer intentionally runs diagnostics before starting it.
- Fix: those exact checks are warnings only during stopped-service preflight. Runtime
  supervisor startup still runs the full doctor after MCP health passes.
- Final disposition: fixed and retested; install succeeds and runtime doctor passes.

### 2. Graceful shutdown completed but the controller reported a timeout

- Observed: logs showed `supervisor_stopped`, children were gone, but the controller
  still considered the supervisor PID alive and the fallback returned `force_failed`.
- Cause: Windows retained a queryable exited-process object briefly; creation time
  alone did not prove that its exit code was `STILL_ACTIVE`.
- Fix: process identity now also checks `GetExitCodeProcess == STILL_ACTIVE`.
- Final disposition: fixed, covered by a terminated-process unit test, and live stop,
  restart, uninstall, and scheduled-action recovery all passed.

### 3. Idempotent reinstall conflicted with the live tunnel health port

- Observed: the second installer preflight launched standalone `tunnel-client doctor`,
  which could not bind `127.0.0.1:8080` because the owned healthy tunnel already held
  it.
- Fix: when status proves the owned tunnel is live, heartbeat-current, and healthy,
  doctor reports that authoritative state instead of launching a conflicting second
  listener.
- Final disposition: fixed; repeated installation passed with one task and one
  supervisor.

### 4. ChatGPT exposed an incorrect `ping` output schema

- Observed: the tunnel worked and two tools passed, but `ping` failed connector-side
  Pydantic validation because `dict[str, str]` described the entire structured envelope
  as string-valued.
- Fix: `ping` now declares `dict[str, object]`; its FastMCP output schema permits the
  envelope's booleans, nested object, list, and null.
- Final disposition: fixed, schema regression test added, service restarted, and the
  built-in-browser ChatGPT retry passed all three tools.

### 5. ChatGPT Statsig requests repeatedly timed out

- Observed: requests to `ab.chatgpt.com` timed out during browser snapshots and caused
  two browser-control sessions to reset.
- Impact: page inspection was delayed, but ChatGPT generation, tunnel calls, and MCP
  results completed. The same signed-in tab was recovered without resending duplicate
  prompts.
- Final disposition: external, non-blocking abnormality; final ChatGPT result was
  independently read from the completed conversation.

### 6. Direct hashing of the live SQLite file was unavailable

- Observed: `Get-FileHash data/operator.db` could not open the database while the
  running server held it.
- Impact: no database error or data loss occurred.
- Verification used instead: the SQLite API listed the same two workspace records
  before and after uninstall, and service startup reconciled both filesystem and Git
  registrations.
- Final disposition: acceptance probe adjusted; persisted logical state passed.

### 7. Concurrent Windows status reader briefly blocked atomic replacement

- Observed: a stress test that continuously read `status.json` caused one
  `PermissionError` during `os.replace` on Windows.
- Cause: a reader may briefly open the destination without delete sharing even though
  the writer has already flushed and closed the temporary file.
- Fix: atomic replacement now retries that specific transient sharing violation for a
  bounded 200 ms while retaining the same fully-written temporary file.
- Final disposition: fixed; the continuous-reader stress test and full suite pass
  without warnings.

### 8. Logger reconfiguration did not close replaced handlers

- Observed: running the full suite with warnings promoted to errors exposed unclosed
  rotating-file handlers when tests reconfigured a component logger.
- Cause: `logger.handlers.clear()` detached handlers without closing their files.
- Fix: reconfiguration now removes and explicitly closes every prior handler before
  attaching the replacement.
- Final disposition: fixed; the entire 108-test suite passes with `-W error`.

## Residual-state check

At final acceptance:

- Exactly one supervisor, one MCP, and one tunnel process were owned by the task.
- No delayed-proxy relay or occupied-port fixture remained.
- The service state and heartbeat were current and healthy.
- Both pre-existing retained workspaces remained registered.
- The scheduled task remained installed for normal unattended use.
- No runtime key appeared in status or logs.
- No commit, merge, push, deploy, or workspace mutation was performed by Phase 5
  acceptance.

## Final decision

Phase 5 passes its implementation plan and definition of done. The MCP server and
Secure MCP Tunnel now run unattended under one hidden user-level supervisor; startup,
dependency ordering, restart, shutdown, persistence, diagnostics, logs, installation,
removal, and ChatGPT reconnection were exercised successfully.
