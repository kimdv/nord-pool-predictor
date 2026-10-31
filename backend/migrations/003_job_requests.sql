CREATE TABLE job_requests (
    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name TEXT NOT NULL CHECK (job_name IN (
        'bootstrap', 'ingest_prices', 'ingest_weather', 'ingest_production',
        'ingest_crossborder', 'refresh_forecast', 'score_forecasts', 'retrain_model', 'cleanup'
    )),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error TEXT
);
CREATE UNIQUE INDEX idx_job_requests_active ON job_requests (job_name)
    WHERE status IN ('pending', 'running');
CREATE INDEX idx_job_requests_pending ON job_requests (created_at, job_id)
    WHERE status = 'pending';
