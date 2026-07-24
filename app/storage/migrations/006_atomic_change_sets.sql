CREATE TABLE IF NOT EXISTS change_sets (
    change_set_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    explanation TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    base_head TEXT NOT NULL,
    before_tree_hash TEXT NOT NULL,
    staged_digest TEXT,
    validated_digest TEXT,
    after_tree_hash TEXT,
    commit_phase TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    validated_at TEXT,
    committed_at TEXT,
    closed_at TEXT,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_change_sets_one_active_workspace
    ON change_sets(workspace_id)
    WHERE status IN ('open', 'validated', 'committing', 'recovery_required');

CREATE TABLE IF NOT EXISTS change_set_operations (
    change_set_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    operation_type TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result_json TEXT,
    PRIMARY KEY (change_set_id, operation_id),
    UNIQUE (change_set_id, ordinal),
    FOREIGN KEY (change_set_id) REFERENCES change_sets(change_set_id)
);

CREATE TABLE IF NOT EXISTS change_set_files (
    change_set_id TEXT NOT NULL,
    path TEXT NOT NULL,
    change_type TEXT NOT NULL,
    before_exists INTEGER NOT NULL,
    before_sha256 TEXT,
    before_mode INTEGER,
    after_exists INTEGER NOT NULL,
    after_sha256 TEXT,
    after_mode INTEGER,
    PRIMARY KEY (change_set_id, path),
    FOREIGN KEY (change_set_id) REFERENCES change_sets(change_set_id)
);
