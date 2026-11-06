-- Preserve scheduler provenance across the existing worker subprocess boundary.
ALTER TABLE job_requests ADD COLUMN scheduled_at TIMESTAMPTZ;
