-- Section 5: durable append-only workspace/process event stream.
-- init_db creates the same schema for deployments upgrading before the
-- versioned migration runner in Section 9 is introduced.

CREATE TABLE events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     TEXT,
    workspace_id   TEXT,
    process_id     TEXT,
    event_type     TEXT NOT NULL,
    sequence       INTEGER,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
);

CREATE INDEX idx_events_workspace_event
    ON events(workspace_id, event_id);
CREATE INDEX idx_events_process_event
    ON events(process_id, event_id);
CREATE UNIQUE INDEX idx_events_process_sequence
    ON events(process_id, sequence)
    WHERE process_id IS NOT NULL AND sequence IS NOT NULL;

CREATE TABLE event_retention_state (
    workspace_id    TEXT PRIMARY KEY,
    expired_through INTEGER NOT NULL DEFAULT 0
);
