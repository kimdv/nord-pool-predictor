ALTER TABLE forecast_run_metrics ADD COLUMN coverage_24h DOUBLE PRECISION;
ALTER TABLE forecast_run_metrics ADD COLUMN coverage_72h DOUBLE PRECISION;
ALTER TABLE forecast_run_metrics ADD COLUMN coverage_168h DOUBLE PRECISION;

-- All legacy scores used different coverage/baseline semantics. Recompute both
-- partial and fully covered runs, and hide old metrics until that has completed.
UPDATE forecast_runs fr SET status = 'completed'
WHERE status = 'scored';

DELETE FROM baseline_metrics;
DELETE FROM forecast_run_metrics;
