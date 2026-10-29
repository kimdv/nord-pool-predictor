CREATE INDEX idx_forecast_runs_area_cursor ON forecast_runs (area, issued_at DESC, run_id DESC);
CREATE INDEX idx_model_versions_cursor ON model_versions (trained_at DESC, model_version DESC);
