# Section 6 live ChatGPT acceptance

This acceptance proves that ChatGPT can use the atomic Change Set lifecycle
through the real Secure MCP Tunnel. It checks isolated staging, ordered
composition, validation invalidation, conflict detection, idempotent replay,
commit visibility, rollback semantics, Git-index isolation, event redaction,
and cleanup.

A passing run discards its disposable workspace. A failing run retains the
workspace and all non-terminal Change Set evidence for diagnosis.

## Automated preflight

Run this before opening ChatGPT:

```powershell
python scripts/accept-iteration.py --section 6
python -m pytest -q tests/unit/test_change_sets.py
python -m pytest -q tests/smoke/test_mcp_smoke.py `
  -m smoke
```

The live MCP inventory must expose all seven Change Set tools and
`get_capabilities` must report `supports_change_sets=true`.

## Exact live acceptance prompt

Paste the following into a fresh conversation on
[chatgpt.com](https://chatgpt.com/) with the `gpt-local` app enabled:

```text
Run the Section 6 atomic Change Set live acceptance using only the gpt-local
app. Do not use web search, shell/computer tools, direct filesystem tools,
commits, branches, pushes, deployments, or manual cleanup. Every workspace
change must use the named gpt-local tool. Use a fresh random 10-character
lowercase hex suffix wherever <suffix> appears.

If a tool required by a step is unavailable, report FAIL and stop. Do not
substitute apply_patch/replace_text except where a step explicitly requires the
ordinary replace_text tool to create a commit conflict. Do not claim PASS for
an operation whose response you did not inspect.

1. Call get_capabilities. Require:
   - capabilities.supports_change_sets = true
   - supports_change_set_validators = false
   - change_set_file_types = ["utf8_regular_file"]
   - positive max_operations, max_changed_files, max_staging_bytes, and
     ttl_hours in change_set_limits.
   Record all Change Set limits.

2. Call list_projects, then create one detached workspace for project
   "Gpt-Local" with task_name "section6-live-chatgpt-<suffix>". Record
   workspace_id and worktree_path. Call git_status and save the complete initial
   status as initial_status. Require no dirty entries. The test path is
   "section6-live-<suffix>.txt".

3. Begin Change Set A with explanation "section6 live atomic composition".
   Record change_set_id, revision, before_tree_hash, base_head, and expires_at.
   Immediately call get_change_set and require status="open", revision=1, no
   operations, and no changed files.

4. Prove path rejection without contaminating staging. Call stage_replace on
   Change Set A with path="../section6-escape.txt", old_text="x",
   new_text="y", explanation="must be denied", operation_id="deny-<suffix>",
   and idempotency_key="deny-<suffix>". Require error.code="PATH_DENIED".
   Call get_change_set and require revision=1, no operations, and no changed
   files.

5. Stage a new UTF-8 file with stage_patch on Change Set A. Use this exact
   unified diff after replacing <suffix>:

   --- /dev/null
   +++ b/section6-live-<suffix>.txt
   @@ -0,0 +1 @@
   +section6-alpha

   Use explanation="create isolated acceptance file",
   operation_id="create-<suffix>", and idempotency_key="create-<suffix>".
   Require ordinal=1, revision=2, validation_invalidated=false, and an added
   manifest entry for the test path.

6. Prove staging isolation. Call read_files for the test path in the real
   workspace and require FILE_NOT_FOUND. Call git_status and require it exactly
   matches initial_status. Call get_change_set and require status="open", one
   operation, and an added changed-file entry with after_sha256 present.

7. Stage ordered replacement on Change Set A:
   stage_replace(path=<test path>, old_text="section6-alpha",
   new_text="section6-beta", explanation="ordered staged replacement",
   operation_id="replace-<suffix>", idempotency_key="replace-<suffix>").
   Require ordinal=2 and revision=3. Repeat the identical stage_replace call
   with the same operation_id and idempotency_key. Require the same ordinal and
   revision and no duplicate operation.

8. Validate Change Set A with expected_revision=3 and
   validation_profile="default". Record validated_digest_A1 and
   after_tree_hash_A1. Require status="validated"; both structure and
   git_apply_check validators passed; the manifest contains only the test path;
   and the diff preview contains "section6-beta". Call read_files again and
   still require FILE_NOT_FOUND in the real workspace.

9. Prove validation invalidation. Stage another replacement on Change Set A:
   old_text="section6-beta", new_text="section6-gamma",
   explanation="invalidate prior validation", operation_id="gamma-<suffix>",
   idempotency_key="gamma-<suffix>". Require ordinal=3, revision=4, and
   validation_invalidated=true. Attempt commit_change_set using
   validated_digest_A1, expected_workspace_tree=<before_tree_hash>, and
   idempotency_key="stale-commit-<suffix>". Require
   error.code="CHANGE_SET_NOT_VALIDATED" and no real workspace write.

10. Revalidate Change Set A with expected_revision=4. Record
    validated_digest_A2 and after_tree_hash_A2; require they differ from A1.
    Commit it with validated_digest_A2,
    expected_workspace_tree=<before_tree_hash>, and
    idempotency_key="commit-a-<suffix>". Require status="committed" and
    after_tree_hash=after_tree_hash_A2. Repeat the identical commit call and
    require a safe replay with the same final result. Call read_files and
    require the exact content "section6-gamma\n". Call git_status and require
    only the new test file is dirty; no other path may change.

11. Call rollback_change_set on committed Change Set A with
    idempotency_key="rollback-committed-<suffix>". Require
    error.code="CHANGE_SET_ALREADY_COMMITTED" and verify the test file still
    contains "section6-gamma\n".

12. Prove whole-workspace conflict detection. Begin Change Set B. Stage replace
    on the test path from "section6-gamma" to "section6-staged-conflict" and
    validate its returned revision. Then make one explicitly permitted ordinary
    replace_text call on the real workspace, replacing "section6-gamma" with
    "section6-external-change", explanation="create Change Set conflict",
    idempotency_key="external-<suffix>". Attempt commit_change_set B using its
    validated digest and before tree with idempotency_key="commit-b-<suffix>".
    Require error.code="CHANGE_SET_CONFLICT", and require the response does not
    claim any file was applied. Read the real file and require the exact content
    "section6-external-change\n", proving the staged value was not written.

13. Roll back Change Set B with idempotency_key="rollback-b-<suffix>". Require
    status="rolled_back". Repeat the identical rollback call and require a safe
    replay with the same terminal result. The real file must remain
    "section6-external-change\n".

14. Restore cleanliness entirely through Change Sets. Begin Change Set C and
    stage this exact deletion patch after replacing <suffix>:

    --- a/section6-live-<suffix>.txt
    +++ /dev/null
    @@ -1 +0,0 @@
    -section6-external-change

    Validate the returned revision and commit with
    idempotency_key="commit-c-<suffix>". Require the test path no longer exists.
    Call git_status and require it exactly matches initial_status.

15. Call get_events for the workspace with cursor=null, wait_seconds=0, and
    event_types containing all seven change_set.* event names. Require events
    for begun, staged, validated, committed, and rolled_back. Inspect every
    Change Set event payload and require it contains no patch text, source text,
    old_text, new_text, diff, idempotency key, or absolute path. Require no
    duplicate committed event for Change Set A and no committed event for
    Change Set B.

16. If and only if every assertion passed, call discard_workspace with
    idempotency_key="discard-section6-<suffix>" and require removed_path=true and
    database_record_removed=true. If any assertion failed, retain the workspace
    and all Change Set evidence for diagnosis.

17. Return a compact PASS/FAIL table with rows for: tool exposure/capabilities,
    initial state, begin/get, path rejection, stage isolation, ordered
    composition, operation replay, validation, validation invalidation, stale
    commit rejection, successful commit, commit replay, committed rollback
    rejection, workspace conflict, conflict zero-write proof, rollback replay,
    cleanup Change Set, event coverage/redaction, Git isolation, and workspace
    cleanup. Include sanitized IDs, revisions, tree/digest prefixes, operation
    ordinals, error codes, event types/counts, and cleanup evidence. Do not
    include source or patch bodies in the final report.
```

## Pass/fail record

| Requirement | Result | Sanitized evidence |
|---|---|---|
| Tool exposure and capabilities | PENDING | |
| Initial state | PENDING | |
| Begin and `get_change_set` | PENDING | |
| Path rejection | PENDING | |
| Stage isolation | PENDING | |
| Ordered composition | PENDING | |
| Operation replay | PENDING | |
| Validation | PENDING | |
| Validation invalidation | PENDING | |
| Stale commit rejection | PENDING | |
| Successful commit | PENDING | |
| Commit replay | PENDING | |
| Committed rollback rejection | PENDING | |
| Workspace conflict | PENDING | |
| Conflict zero-write proof | PENDING | |
| Rollback replay | PENDING | |
| Cleanup Change Set | PENDING | |
| Event coverage and redaction | PENDING | |
| Git isolation | PENDING | |
| Workspace cleanup | PENDING | |

## Run evidence

- Timestamp (Asia/Shanghai):
- ChatGPT task URL:
- Workspace ID/path:
- Change Set IDs:
- Result:
- Cleanup result:
- Abnormal behavior:
