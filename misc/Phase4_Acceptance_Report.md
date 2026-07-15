# Phase 4 Acceptance Report

- Date: 2026-07-15 (Asia/Shanghai)
- Project under test: `gpt-local-code-operator`
- Acceptance fixture: `phase4-order-fixture`
- Fixture base commit: `43eba86ee13e187af9bf1892c555ce32123604ad`
- Result: **PASS**

## Scope and evidence

The Phase 4 criteria in `plan-personal-pwsh-proxy.md` were exercised against the
running MCP server at `127.0.0.1:8765`, the Secure MCP Tunnel, and ChatGPT in the
Codex built-in browser. The fixture contains two related source files, a stable
duplicate-order defect, an existing failing regression test, and the configured
`unit_tests`, `lint`, `typecheck`, and `git_tests` checks.

The ChatGPT acceptance prompts required MCP-only operation, detached worktrees,
an intentional first incomplete fix, diagnosis from real failing output, a second
fix, all configured checks, independent final `git_status` and `git_diff` calls,
and no commit/merge/push/deploy.

## Automated and live checks

| Check | Result | Evidence |
|---|---:|---|
| Main unit suite | PASS | `60 passed` |
| Ruff | PASS | `All checks passed!` |
| Strict mypy | PASS | `Success: no issues found in 19 source files` |
| Basic Phase 4 live integration | PASS | `tests/integration/test_phase4.py` completed the create/read/patch/check/Git/discard loop |
| Process execution acceptance | PASS | Sync, async polling, exit code 42, cancellation, Git status after modification |
| Resilience acceptance | PASS | `tests/integration/test_phase4_resilience.py` covered async `run_check`, failure streams/exit code, timeout, terminal-state stability, repeated cancellation, output truncation/tail retention, workspace recovery, and three-workspace isolation |
| ChatGPT task A | PASS | Failure → diagnosis → second patch → all checks passed; final status/diff obtained |
| ChatGPT task B | PASS | Intentional failure recorded with exit code 1; status/diff obtained; workspace discarded |
| ChatGPT task C | PASS | Fresh workspace proved free of B's marker; independent fix and all checks passed |
| Base repository isolation | PASS | Fixture base remained at `43eba86e`; `git status` clean |
| Prohibited operations | PASS | No commit, merge, push, deploy, model API, Codex CLI/SDK, or external coding agent was used by the MCP workflow |
| Residual process check | PASS | No process command line referenced the acceptance worktrees after completion |

## ChatGPT end-to-end tasks

### Task A — failure diagnosis and iterative repair

- User task: fix duplicate order submission, add a different-SKU retry regression
  test, intentionally apply an incomplete first fix, diagnose the failure, then
  apply the complete fix and run every configured check.
- Workspace: `ws-d5d1610f`
- Base commit: `43eba86ee13e187af9bf1892c555ce32123604ad`
- Tool sequence observed: `list_projects`, `create_workspace`, `get_repo_map`,
  `search_code`, `read_files`, `apply_patch`, `list_checks`, `run_check`,
  `run_pwsh`, `git_status`, `git_diff`.
- First `unit_tests`: `failed`, exit code `1`, `2 failed`.
- Failure evidence: the returned order was reused, but persistence still contained
  a second `Order`; pytest reported `Left contains one more item: Order(...)`.
- Second fix: return the cached first order before `save`; cache only after the
  initial save succeeds.
- Final checks:
  - `unit_tests`: passed, exit `0`, `2 passed`.
  - `lint`: passed, exit `0`.
  - `typecheck`: passed, exit `0`.
  - `git_tests`: passed, exit `0`.
- Final status: modified `order_service.py` and `test_order_service.py` only.
- Regression validity: both tests fail on the base implementation and pass on the
  repaired implementation; the new assertion covers the different-SKU retry path.

### Task B — failed task and discard

- Workspace: `ws-3148368d`
- Base commit: `43eba86ee13e187af9bf1892c555ce32123604ad`
- Added a deliberate failing test through `apply_patch`.
- `unit_tests`: failed, exit `1`, with both the original defect and the deliberate
  failure visible in real pytest output.
- `git_status`: exit `0`; `git_diff`: exit `0`.
- `discard_workspace`: `removed_path=true`; the worktree path no longer exists.

### Task C — fresh successful task and pollution check

- Workspace: `ws-b6cc2b8c`
- Base commit: `43eba86ee13e187af9bf1892c555ce32123604ad`
- Before modification, `search_code` found zero matches for Task B's unique marker,
  `read_files` showed only the base test, and `git_status` was clean.
- Applied an independent implementation and regression-test patch.
- Final checks:
  - `unit_tests`: passed, exit `0`, `2 passed`.
  - `lint`: passed, exit `0`.
  - `typecheck`: passed, exit `0`.
  - `git_tests`: passed, exit `0`.
- Final status: modified `order_service.py` and `test_order_service.py` only.
- Task B does not appear in the worktree list; Tasks A and C are retained and
  detached at the same base commit.

## Exceptional scenarios

| Scenario | Observed result |
|---|---|
| Invalid `check_id` | Structured error listed available checks; no `process_id` was created |
| Non-zero command | `failed`, real exit `7`, stdout and stderr retained |
| Async `run_check` | Returned queryable `process_id`; final `failed`/exit `1` was stable across repeated reads |
| Timeout | `timed_out`; repeated queries stayed `timed_out`; a later workspace command passed |
| Active cancellation | `cancelled`; repeated cancellation stayed `cancelled`; process tree termination reported |
| Oversized output | Response was bounded, `truncated=true`, and the diagnostic tail retained `OUTPUT-END` |
| Check-generated files | `__pycache__` was visible in Git status, identified as a generated artifact, removed, and final status rechecked |
| Workspace isolation | Three different IDs and paths; B's marker never appeared in A or C; B was discarded without affecting A/C |

## Implementation corrections made during acceptance

1. Corrected the configured typecheck target from nonexistent `src` to `app`.
2. Added Ruff exclusions for nested `.claude` worktrees and generated output/state.
3. Fixed Ruff violations in the Phase 1–4 implementation and tests.
4. Fixed strict typing errors across configuration, database, server routes,
   workspace validation, process execution, and PowerShell configuration.
5. Fixed post-process Git status collection, which incorrectly attempted to read a
   nonexistent `Popen.cwd`; the process record now retains the real working directory.
6. Fixed process `started_at`, which was previously `null` because the database only
   set it when a PID was supplied during the `running` transition.
7. Added `tests/integration/test_phase4_resilience.py` as repeatable coverage for
   failure, async state, timeout, cancellation, truncation, recovery, and isolation.

## Abnormal behavior log

1. A pre-existing MCP server already owned `127.0.0.1:8765`; a second launch failed
   with Windows socket error 10048. The existing process was identified, stopped,
   and replaced with the corrected server.
2. After the server restart, the ChatGPT `gpt-local` app displayed a retry state.
   Clicking retry stalled. Restarting `tunnel-client` through `start-tunnel.ps1`
   and fully refreshing ChatGPT restored the app.
3. The in-app ChatGPT page repeatedly timed out requests to `ab.chatgpt.com`
   (Statsig initialization/registration). These timeouts slowed browser actions but
   did not stop prompts, ChatGPT generation, or MCP tool calls.
4. Tunnel startup logged warnings that loopback OAuth discovery URLs use HTTP while
   optional Harpoon auto-registration requires HTTPS. MCP initialization and tunnel
   polling still completed successfully.
5. Two transient MCP HTTP 400 responses occurred while ChatGPT opened/replaced
   sessions. The next session initialized normally and Task B/C continued without
   state loss.
6. ChatGPT produced several malformed unified-diff hunk counts during Task A.
   `apply_patch` rejected every malformed patch without modifying the workspace;
   ChatGPT read the error, retried, and ultimately applied a valid patch.
7. Python checks generated `__pycache__` directories in the fixture. ChatGPT and the
   independent verifier both detected and removed them before final Git checks.

No abnormal event invalidated a final result. All retained terminal states, Git
results, and worktree paths were independently rechecked.

## Final acceptance decision

Phase 4 passes the plan's final conditions:

- three independent end-to-end workspaces were completed;
- at least one real modification → failure → diagnosis → second modification → pass
  loop was observed;
- the regression test fails on the old implementation and passes on the fix;
- all successful tasks have real status, diff, output, and exit-code evidence;
- failure, timeout, cancellation, asynchronous polling, output truncation, and
  repeated terminal-state queries were tested;
- the main fixture repository stayed clean and unchanged;
- no commit, merge, push, deploy, external model API, or external coding agent was
  used by the Local Code Operator workflow;
- no unexplained files or acceptance-worktree processes remained.
