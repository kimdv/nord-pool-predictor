-- 15-minute resolution migration: renames horizon columns from hours to steps,
-- updates forecast_values check constraint and dependent views, then clears
-- derived/forecast/model data for a clean 15-minute timeline.

-- Dependent view must be dropped before latest_forecast_runs.
DROP VIEW IF EXISTS latest_forecast_quality;
DROP VIEW IF EXISTS latest_forecast_runs;

ALTER TABLE forecast_runs RENAME COLUMN horizon_hours TO horizon_steps;
ALTER TABLE forecast_runs DROP CONSTRAINT IF EXISTS forecast_runs_horizon_hours_check;
ALTER TABLE forecast_runs ADD CONSTRAINT forecast_runs_horizon_steps_check CHECK (horizon_steps > 0);

ALTER TABLE forecast_values RENAME COLUMN horizon_hour TO horizon_step;

ALTER TABLE forecast_values DROP CONSTRAINT IF EXISTS forecast_values_horizon_hour_check;
ALTER TABLE forecast_values ADD CONSTRAINT forecast_values_horizon_step_check CHECK (horizon_step >= 1);

DROP INDEX IF EXISTS idx_forecast_values_run_horizon;
CREATE INDEX IF NOT EXISTS idx_forecast_values_run_step
    ON forecast_values (run_id, horizon_step);

CREATE OR REPLACE VIEW latest_forecast_runs AS
SELECT DISTINCT ON (area)
    run_id, area, model_version, issued_at,
    horizon_steps, status, created_at, updated_at
FROM forecast_runs
WHERE status IN ('completed', 'scored')
ORDER BY area, issued_at DESC;

CREATE OR REPLACE VIEW latest_forecast_quality AS
SELECT
    fr.area,
    frm.run_id,
    fr.model_version,
    fr.issued_at,
    frm.mae_24h, frm.mae_72h, frm.mae_168h,
    frm.rmse_24h, frm.rmse_72h, frm.rmse_168h,
    frm.bias_24h, frm.bias_72h, frm.bias_168h,
    frm.hitrate_0_05, frm.hitrate_0_10, frm.hitrate_0_20,
    frm.quality_label,
    frm.worst_hour_ts, frm.worst_hour_abs_error,
    frm.scored_at
FROM forecast_run_metrics frm
JOIN forecast_runs fr ON fr.run_id = frm.run_id
JOIN latest_forecast_runs lfr ON lfr.run_id = fr.run_id;

-- One-time data reset for a clean 15-minute start
TRUNCATE model_versions CASCADE;
TRUNCATE forecast_runs CASCADE;
TRUNCATE forecast_run_metrics CASCADE;
TRUNCATE baseline_metrics CASCADE;
DELETE FROM price_observations WHERE 1=1;
