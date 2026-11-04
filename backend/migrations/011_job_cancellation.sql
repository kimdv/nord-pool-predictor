ALTER TABLE job_requests DROP CONSTRAINT job_requests_status_check;
ALTER TABLE job_requests ADD CHECK (status IN (
    'pending', 'running', 'cancelling', 'cancelled', 'completed', 'failed'
));
DROP INDEX idx_job_requests_active;
CREATE UNIQUE INDEX idx_job_requests_active ON job_requests (job_name)
    WHERE status IN ('pending', 'running', 'cancelling');

ALTER TABLE job_runs DROP CONSTRAINT job_runs_status_check;
ALTER TABLE job_runs ADD CHECK (status IN ('pending', 'running', 'cancelled', 'completed', 'failed'));
