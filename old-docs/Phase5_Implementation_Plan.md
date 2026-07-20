# Phase 5 Implementation Plan: Unattended Background Operation

> Status at entry: Phase 4 implemented and accepted  
> Target platform: Windows 11, PowerShell 7, Python 3.12+  
> Operating model: one trusted user, one local MCP server, one Secure MCP Tunnel  
> Estimated effort: 2–4 working days, including restart and logon acceptance tests

## 1. Outcome

Phase 5 turns the MCP server and `tunnel-client` into one unattended, user-level
background service. After installation, normal use must not require two open terminal
windows.

The completed system must:

1. Start automatically when the configured Windows user logs on.
2. Start the MCP server, wait for `/healthz`, then start the tunnel.
3. Wait for a late-starting local proxy without repeatedly opening windows.
4. Restart either child process after an unexpected failure.
5. Stop only the processes owned by this installation.
6. Persist bounded, redacted logs for both child processes and the supervisor.
7. Expose a simple start, stop, restart, status, doctor, install, and uninstall workflow.
8. Recover its existing database and worktree records after a service restart.
9. Produce enough evidence to diagnose failures without attaching a debugger.

The preferred Phase 5 implementation is a **user-level supervisor launched by one
hidden Task Scheduler task**. It behaves like a service while retaining access to the
user's tunnel profile, proxy, Git configuration, Python environment, and credentials.

## 2. Design decision

### 2.1 Selected topology

```text
Windows user logon
        |
        v
Task Scheduler: gpt-local-code-operator
        |
        v
Python supervisor (one instance)
        |
        +-- MCP child: python -m app.server
        |       `-- http://127.0.0.1:8765/healthz
        |
        `-- Tunnel child: tunnel-client run --profile ...
                `-- OpenAI control plane through configured proxy
```

The supervisor owns both children but monitors and restarts them independently. The
tunnel is a dependent child: it may run only when the proxy and MCP health endpoint are
ready.

### 2.2 Why not a native Windows service in Phase 5

A native service normally runs outside the interactive user's environment. That can
change the available PATH, home directory, tunnel profile, proxy access, credentials,
and Git configuration. This project is explicitly a personal tool whose proxy is tied
to the logged-in user, so a logon task is the lower-risk default.

A WinSW-based native service may be added later for machines that require operation
before user logon. It is not required for Phase 5 acceptance.

### 2.3 One task instead of two

The current two-task design leaves dependency ordering and failure handling split
between Task Scheduler and shell scripts. Phase 5 replaces it with one scheduled task
and one supervisor so that startup order, PID ownership, restart policy, logs, and
shutdown are controlled in one place.

## 3. Scope and non-goals

### 3.1 In scope

- Background lifecycle supervision for MCP and tunnel processes.
- One-instance enforcement.
- Dependency-aware startup and restart.
- Hidden user-logon startup through Task Scheduler.
- Exact process ownership and controlled shutdown.
- Atomic runtime status and heartbeat files.
- Log files, rotation, retention, and secret redaction.
- Installation, removal, status, restart, and diagnostics scripts.
- Conservative startup reconciliation for database workspaces and processes.
- Unit, integration, failure-injection, logon, and ChatGPT tunnel acceptance tests.

### 3.2 Not in scope

- Running as `LocalSystem` or before user logon.
- Installing or controlling the user's proxy application.
- Enterprise monitoring, alerting, or remote administration.
- Multiple users sharing one supervisor.
- Automatic commit, merge, push, or deployment.
- Deleting dirty worktrees merely because they are old.
- Changing the accepted Phase 1–4 MCP tool behavior.

## 4. Runtime contract

### 4.1 Supervisor states

The supervisor publishes one of these top-level states:

```text
starting
waiting_for_proxy
starting_mcp
waiting_for_mcp
starting_tunnel
healthy
degraded
stopping
stopped
failed
```

Each child publishes:

```text
stopped
starting
healthy
backoff
failed
```

Runtime status must include:

- Supervisor PID, process creation identity, start time, state, and heartbeat time.
- MCP PID, health state, last exit code, restart count, and last error.
- Tunnel PID, state, last exit code, restart count, and last error.
- Proxy readiness without exposing proxy credentials.
- Configuration path and non-secret effective endpoints.
- The next retry time when a child is in backoff.

Status is written atomically to `data/service/status.json`. A partially written file
must never be observable.

### 4.2 Single-instance behavior

Only one supervisor may own an installation. It must hold an exclusive Windows file
lock for its full lifetime and record its PID plus process creation time. A second
start must exit cleanly with an `already_running` result and must not start additional
children.

PID alone is not sufficient because Windows may reuse PIDs. Stop and status operations
must validate the recorded process creation identity before acting.

### 4.3 Startup order

1. Load and validate configuration.
2. Acquire the single-instance lock.
3. Initialize logging and write the initial status.
4. Reconcile interrupted process records and registered workspaces.
5. Start MCP and poll `/healthz` until it succeeds or startup times out.
6. Wait for the configured proxy when proxying is enabled.
7. Run `tunnel-client doctor` with a bounded timeout.
8. Start the tunnel and enter the monitoring loop.

The MCP server does not depend on the proxy and should remain locally available while
the proxy is unavailable.

### 4.4 Restart policy

Unexpected child exits use bounded exponential backoff:

```text
initial delay: 2 seconds
multiplier: 2
maximum delay: 60 seconds
stable-period reset: 5 minutes
retry limit: unlimited while supervisor is running
```

The exact values are configurable. Restart loops must never spin continuously.

Rules:

- MCP failure restarts MCP.
- While MCP is unhealthy, the tunnel is stopped or held in `backoff`.
- After MCP recovers, the tunnel is started again.
- Tunnel failure does not restart a healthy MCP server.
- Proxy loss makes the tunnel degraded; MCP stays healthy.
- A requested supervisor shutdown never triggers child restart.

### 4.5 Shutdown policy

`stop-service.ps1` requests supervisor shutdown through a service control request and
waits for a bounded grace period. The supervisor then:

1. Enters `stopping`.
2. Stops the tunnel child and its process tree.
3. Stops the MCP child and its process tree.
4. Flushes logs and writes final state.
5. Releases its lock and exits.

If graceful shutdown exceeds the configured timeout, the control script may terminate
only the validated supervisor-owned process tree. It must not kill every `python` or
`tunnel-client` process on the machine.

## 5. Configuration changes

Extend `config/operator.yaml` with a `service` section. Existing server, proxy, and
logging keys remain authoritative; startup scripts must stop duplicating their values.

```yaml
service:
  task_name: gpt-local-code-operator
  tunnel_enabled: true
  tunnel_profile: local-code-operator
  tunnel_health_url: http://127.0.0.1:8080/readyz
  poll_interval_seconds: 2
  heartbeat_interval_seconds: 5
  startup_timeout_seconds: 120
  shutdown_timeout_seconds: 20

  restart:
    initial_delay_seconds: 2
    multiplier: 2
    max_delay_seconds: 60
    stable_reset_seconds: 300

  cleanup:
    reconcile_on_start: true
    report_expired_workspaces: true
    auto_discard_clean_expired: false

logging:
  level: INFO
  retention_days: 14
  max_file_bytes: 10485760
  backup_count: 5
```

Validation requirements:

- Reject non-loopback MCP binding for this personal service configuration.
- Reject non-positive intervals and timeouts.
- Require a tunnel profile when the tunnel is enabled.
- Resolve and display the exact Python, PowerShell, and tunnel-client executables in
  `doctor`, but do not log credential values.
- Continue supporting manual MCP-only startup when `tunnel_enabled` is false.

## 6. Planned files and responsibilities

### 6.1 Application code

```text
app/services/supervisor.py
    Child lifecycle, health polling, backoff, dependency handling, and shutdown.

app/services/service_state.py
    File lock, atomic status writes, heartbeat, control requests, and PID identity.

app/services/logging_config.py
    Rotating file handlers and secret-redaction filter shared by supervisor/server.

app/service.py
    CLI entry point: run, status, stop, doctor, and reconcile.
```

Where practical, lifecycle code must be platform-isolated so its state transitions can
be unit-tested without launching real processes.

### 6.2 PowerShell entry points

```text
scripts/install-service.ps1
scripts/uninstall-service.ps1
scripts/start-service.ps1
scripts/stop-service.ps1
scripts/restart-service.ps1
scripts/status-service.ps1
scripts/doctor.ps1
```

The word `service` here means the user-level supervised background application. The
installer must clearly state that it creates a Task Scheduler task, not a native
Windows service.

### 6.3 Compatibility scripts

Existing `start-mcp.ps1` and `start-tunnel.ps1` remain useful for foreground debugging.
They must read endpoints and proxy settings from the same configuration and propagate
native executable failures explicitly.

`stop-all.ps1` should become a compatibility wrapper around `stop-service.ps1`. It must
not enumerate and kill unrelated processes by image name.

The old `install-scheduled-tasks.ps1` should either call the new installer or exit with
a migration message. It must not leave the old two tasks alongside the new task.

## 7. Installation behavior

`install-service.ps1` must:

1. Run preflight diagnostics and stop on required failures.
2. Resolve the project root and Python interpreter to absolute paths.
3. Verify that `.env` is excluded from Git and has a usable runtime key when the
   tunnel is enabled.
4. Remove or disable the two legacy scheduled tasks after confirming their names.
5. Register one task for the current user with limited privileges.
6. Configure `AtLogOn`, `StartWhenAvailable`, bounded task restart, and
   `MultipleInstances = IgnoreNew`.
7. Launch PowerShell non-interactively with a hidden window.
8. Set the working directory explicitly rather than relying on the scheduler default.
9. Start the task immediately unless `-NoStart` is supplied.
10. Wait for supervisor health and print a concise installation result.

The installer must be idempotent. Re-running it updates the known task rather than
creating duplicates. Administrator elevation should not be required for a current-user
task; if the host policy requires elevation, the script reports that fact instead of
self-elevating silently.

`uninstall-service.ps1` must stop the owned processes, unregister the known task, and
remove only transient service-control files. It preserves configuration, databases,
logs, and worktrees unless the user supplies an explicit cleanup option.

## 8. Logging and diagnostics

### 8.1 Log files

```text
logs/supervisor.log
logs/mcp.log
logs/tunnel.log
```

Requirements:

- UTF-8 text with timestamps, level, component, PID, and event name.
- Size-based rotation plus age-based retention.
- stdout and stderr from both children retained in their component logs.
- Flush important lifecycle events immediately.
- A new service start is identifiable by a generated run ID.
- Status output references log paths but does not copy unbounded log content.

### 8.2 Secret handling

The runtime API key may be loaded from `.env` for compatibility, but it must never be
written to status, logs, exception messages, process command lines, or diagnostic
output. The logging layer must redact configured secret values and common key-shaped
assignments such as `CONTROL_PLANE_API_KEY=...`.

Phase 5 may document Windows Credential Manager as a later improvement; migrating the
secret store is not required for acceptance.

### 8.3 Doctor checks

`doctor.ps1` or `python -m app.service doctor` reports pass, warning, or failure for:

- Supported Python version and importable dependencies.
- PowerShell 7, Git, ripgrep, and `tunnel-client` resolution.
- Valid operator and project YAML.
- MCP loopback host and port availability.
- Proxy reachability when enabled.
- Runtime API key presence without revealing it.
- Tunnel profile and bounded `tunnel-client doctor` result.
- Writable `data/` and `logs/` directories.
- Scheduled-task presence and action path.
- Current supervisor heartbeat and child status.

Doctor must be read-only except for creating and removing a temporary write probe in
the application-owned data/log directories.

## 9. Recovery and cleanup

### 9.1 Process recovery

On startup, database processes left in `queued` or `running` by an earlier crash are
marked with an explicit interrupted terminal state. They must not remain permanently
running and must not be automatically re-executed.

### 9.2 Workspace reconciliation

For each database workspace, startup reconciliation checks:

- The worktree directory still exists.
- Git still registers the worktree.
- The recorded repository and path remain within configured roots.
- No service-managed process is still attached to it.

Missing or inconsistent workspaces are reported. Repair must be conservative and
auditable.

### 9.3 Expired worktrees

Phase 5 reports workspaces older than their configured TTL. Automatic deletion is off
by default. If `auto_discard_clean_expired` is explicitly enabled, deletion is allowed
only when all of these are true:

- The worktree is clean, including untracked files.
- No process is active for the workspace.
- The base repository and worktree registration match the stored record.
- The workspace exceeds its TTL.
- Removal verification succeeds at the filesystem, Git, and database levels.

Dirty or ambiguous worktrees are never automatically deleted.

## 10. Implementation work packages

### 5.1 Configuration and state foundations

- Add typed service configuration and defaults.
- Implement atomic status/control files and exclusive locking.
- Implement process identity validation.
- Add unit tests for configuration, stale status, PID reuse, and atomic writes.

Exit condition: two supervisors cannot start, and status remains parseable during
continuous updates.

### 5.2 Supervisor lifecycle

- Implement MCP and tunnel child adapters.
- Implement health polling and dependency transitions.
- Implement restart backoff and stable-period reset.
- Implement requested and forced shutdown paths.
- Capture child stdout/stderr without pipe deadlocks.

Exit condition: injected child failures recover without duplicate children or rapid
restart loops.

### 5.3 Installation and control commands

- Add install/uninstall/start/stop/restart/status scripts.
- Replace the legacy dual-task installation path.
- Make install and uninstall idempotent.
- Preserve manual foreground scripts for debugging.

Exit condition: a fresh current-user installation starts in the background with no
persistent terminal window and can be completely removed.

### 5.4 Logging, doctor, and recovery

- Add rotating component logs and redaction.
- Add structured doctor output and actionable failure messages.
- Reconcile interrupted process records and workspaces.
- Report expired workspaces; implement guarded opt-in clean deletion.

Exit condition: common startup and runtime failures can be diagnosed from status,
doctor, and bounded logs.

### 5.5 Documentation and migration

- Update README quick start and project tree.
- Document foreground debug mode versus background service mode.
- Document migration from the two legacy scheduled tasks.
- Document proxy-late, tunnel-auth, occupied-port, and stale-lock recovery.
- Add a Phase 5 acceptance report template.

Exit condition: installation and removal can be followed from a clean checkout without
knowledge of the implementation.

## 11. Test strategy

### 11.1 Unit tests

Tests must cover:

- State transitions and illegal transitions.
- Backoff calculation, cap, and stable reset.
- Single-instance lock acquisition and release.
- Atomic status writes and stale heartbeat detection.
- PID plus creation-time identity validation.
- Proxy/MCP dependency decisions.
- Requested shutdown suppressing restarts.
- Log redaction and rotation configuration.
- Configuration defaults and invalid values.
- Expired-worktree cleanup eligibility rules.

Time and process creation should be injectable so tests do not rely on long sleeps or
kill arbitrary real processes.

### 11.2 Integration tests

Use harmless fixture child processes and temporary ports to verify:

- Correct startup order.
- MCP startup timeout.
- Delayed proxy availability.
- Tunnel exit and restart.
- MCP exit causing tunnel suspension and ordered recovery.
- Repeated child crashes with bounded backoff.
- Clean shutdown and full process-tree termination.
- Duplicate supervisor rejection.
- Recovery from stale status and interrupted database processes.
- Log capture, rotation, and secret absence.
- Installer idempotency and exact scheduled-task configuration.

Integration tests must not depend on the real user's production tunnel profile unless
they are explicitly marked as live acceptance tests.

### 11.3 Regression checks

Before live acceptance, all existing checks must pass:

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy app
git diff --check
```

Phase 1–4 MCP behavior must remain compatible.

## 12. Live acceptance procedure

Acceptance is performed on the real Windows user session and recorded in
`Phase5_Acceptance_Report.md`. Record commands, timestamps, PIDs, exit codes, relevant
status snapshots, and abnormal behavior.

### Scenario A: clean installation

1. Confirm neither legacy task is running.
2. Run `install-service.ps1`.
3. Confirm exactly one scheduled task exists.
4. Confirm the installer returns success and supervisor state becomes `healthy`.
5. Confirm exactly one MCP and one tunnel child belong to the supervisor.
6. Confirm no persistent terminal window remains open.

Pass: service is healthy, ownership is exact, and installation is idempotent on a
second run.

### Scenario B: delayed proxy

1. Stop or isolate the configured proxy.
2. Restart the service.
3. Confirm MCP becomes healthy while status reports `waiting_for_proxy` or `degraded`.
4. Restore the proxy.
5. Confirm the tunnel starts without manual service restart.

Pass: no tight restart loop occurs and the final state is `healthy`.

### Scenario C: tunnel crash recovery

1. Record the tunnel PID.
2. Terminate only that owned tunnel process.
3. Observe non-zero restart count and backoff evidence.
4. Confirm a new tunnel PID appears and MCP PID does not change.

Pass: tunnel recovers and MCP remains available.

### Scenario D: MCP crash recovery

1. Record both child PIDs.
2. Terminate only the owned MCP process.
3. Confirm tunnel is stopped or suspended while MCP is unhealthy.
4. Confirm MCP restarts and passes `/healthz`.
5. Confirm the tunnel reconnects afterward.

Pass: recovery order is MCP then tunnel, with no duplicates.

### Scenario E: duplicate and occupied-port handling

1. Attempt a second supervisor start; expect `already_running`.
2. Stop the service, bind a fixture process to the MCP port, and start the service.
3. Confirm bounded retries and an actionable degraded status.
4. Release the port and confirm automatic recovery.

Pass: no unrelated port owner is killed and the service recovers after release.

### Scenario F: stop, restart, and uninstall

1. Run status, stop, start, and restart commands in sequence.
2. Verify every old child exits before its replacement becomes authoritative.
3. Run uninstall.
4. Confirm the scheduled task is absent and owned processes are gone.
5. Confirm databases, logs, configuration, and worktrees remain.

Pass: control operations are idempotent and do not affect unrelated Python or tunnel
processes.

### Scenario G: logon recovery

1. Install the service and retain at least one known workspace.
2. Log off and log back on, or reboot when practical.
3. Confirm automatic startup with no manual terminal.
4. Confirm the prior workspace is still listed and its Git state is unchanged.
5. Confirm interrupted process records are terminal rather than `running`.

Pass: the service and persisted state recover after a real user-session restart.

### Scenario H: ChatGPT end-to-end check

Using ChatGPT through the built-in browser and the Secure MCP Tunnel:

1. Ask ChatGPT to call `ping`, `list_projects`, and `get_project_status`.
2. Restart the background service locally.
3. After status returns healthy, ask ChatGPT to repeat the calls.
4. Confirm no workspace content changed and no new terminal was required.

Pass: ChatGPT loses connectivity only during the restart window and reconnects without
recreating the app or tunnel configuration.

### Scenario I: logging and secret inspection

1. Cause one controlled MCP failure and one controlled tunnel failure.
2. Confirm supervisor, MCP, and tunnel logs contain useful lifecycle evidence.
3. Force or simulate rotation with a small test limit.
4. Search all logs and status files for the runtime API key and known secret fragments.

Pass: diagnostics are sufficient, files are bounded, and no secret is present.

## 13. Abnormal behavior report

Every acceptance run must include an abnormal-behavior section, even when empty. Each
entry contains:

```text
timestamp
scenario
observed behavior
expected behavior
reproduction steps
relevant status/log excerpt with secrets removed
impact on acceptance
workaround or fix
final disposition
```

Transient network failures, browser retry states, tunnel warnings, stale processes,
unexpected windows, duplicate processes, slow shutdowns, and scheduler inconsistencies
must be recorded rather than silently retried.

## 14. Definition of done

Phase 5 passes only when all of the following are true:

- One hidden current-user scheduled task replaces the two legacy tasks.
- Logon starts the supervisor without an occupied terminal.
- Exactly one supervisor, one MCP child, and at most one tunnel child exist.
- MCP and tunnel startup ordering is deterministic.
- Proxy delay, tunnel failure, MCP failure, occupied port, and duplicate start have
  been exercised successfully.
- Restarts use bounded backoff and never form a tight loop.
- Stop and uninstall affect only processes owned by this installation.
- Status and heartbeat are atomic, current, and actionable.
- Logs rotate, respect retention, and contain no runtime API key.
- Existing workspaces survive service and user-session restart.
- Dirty expired worktrees are never automatically removed.
- Existing unit, lint, type, and integration checks pass.
- ChatGPT reconnects through the tunnel after a service restart.
- The acceptance report records all evidence and abnormal behavior.

## 15. Rollback

If Phase 5 fails during implementation or deployment:

1. Run the new uninstall script to stop owned children and remove its task.
2. Preserve `data/`, `logs/`, `.env`, configuration, and worktrees.
3. Use `start-mcp.ps1` and `start-tunnel.ps1` in foreground debug mode.
4. Reinstall the legacy tasks only if explicitly needed.

Rollback must not require database migration reversal and must not modify any project
worktree.
