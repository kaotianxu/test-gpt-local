-- Section 3: persisted process identity and restart-recovery metadata.
-- Applied by the migration runner introduced in Section 9; init_db keeps a
-- compatibility path for deployments upgrading before that runner exists.

ALTER TABLE processes ADD COLUMN process_creation_identity TEXT;
ALTER TABLE processes ADD COLUMN heartbeat TEXT;
ALTER TABLE processes ADD COLUMN last_output_offset INTEGER NOT NULL DEFAULT 0;
ALTER TABLE processes ADD COLUMN job_object_identity TEXT;
ALTER TABLE processes ADD COLUMN recovery_status TEXT;

CREATE INDEX IF NOT EXISTS idx_process_recovery
    ON processes(status, heartbeat);

-- process_creation_identity prevents PID-reuse mistakes.
-- heartbeat records the latest successful monitor pass.
-- last_output_offset lets a recovered monitor resume output accounting.
