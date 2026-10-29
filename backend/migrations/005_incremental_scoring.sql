ALTER TABLE forecast_run_metrics ADD COLUMN coverage_24h DOUBLE PRECISION;
ALTER TABLE forecast_run_metrics ADD COLUMN coverage_72h DOUBLE PRECISION;
ALTER TABLE forecast_run_metrics ADD COLUMN coverage_168h DOUBLE PRECISION;

-- Revisit legacy partial scores when later actuals arrive.
UPDATE forecast_runs fr SET status = 'completed'
WHERE status = 'scored' AND (
    SELECT COUNT(*) FROM forecast_errors fe WHERE fe.run_id = fr.run_id
) < fr.horizon_steps;
