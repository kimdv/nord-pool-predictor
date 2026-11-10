CREATE TABLE demand_forecasts (
    area TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    source_issued_at TIMESTAMPTZ NOT NULL,
    load_mw DOUBLE PRECISION NOT NULL CHECK (load_mw >= 0 AND load_mw < 'Infinity'),
    source TEXT NOT NULL,
    PRIMARY KEY (area, ts, issued_at, source)
);

ALTER TABLE job_runs DROP CONSTRAINT job_runs_job_type_check;
ALTER TABLE job_runs ADD CONSTRAINT job_runs_job_type_check CHECK (job_type IN (
    'ingest_prices', 'ingest_weather', 'ingest_production', 'ingest_crossborder',
    'refresh_forecast', 'retrain_model', 'score_forecasts', 'cleanup', 'ingest_demand'
));
