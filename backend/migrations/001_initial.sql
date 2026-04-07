-- Nord Pool Forecasting Schema
-- Target: PostgreSQL 16
-- Notes:
-- * Stores actual prices, weather observations/forecasts, model versions,
--   forecast runs, forecast values, evaluation metrics, production data,
--   and cross-border capacity.
-- * Uses UUIDs for forecast runs.
-- * Keeps issued weather forecasts so backtesting can use the data that was
--   actually available at prediction time.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- Price observations
-- =============================================================================

CREATE TABLE IF NOT EXISTS price_observations (
    area            TEXT            NOT NULL,
    ts              TIMESTAMPTZ     NOT NULL,
    price_dkk_kwh   DOUBLE PRECISION NOT NULL,
    source          TEXT            NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (area, ts)
);

CREATE INDEX IF NOT EXISTS idx_price_observations_ts
    ON price_observations (ts);

-- =============================================================================
-- Weather observations (raw per weather point)
-- =============================================================================

CREATE TABLE IF NOT EXISTS weather_observations (
    area              TEXT            NOT NULL,
    ts                TIMESTAMPTZ     NOT NULL,
    temperature_c     DOUBLE PRECISION,
    wind_speed_ms     DOUBLE PRECISION,
    cloud_cover_pct   DOUBLE PRECISION,
    precipitation_mm  DOUBLE PRECISION,
    solar_irradiance_wm2  DOUBLE PRECISION,
    source            TEXT            NOT NULL,
    created_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (area, ts, source)
);

CREATE INDEX IF NOT EXISTS idx_weather_observations_ts
    ON weather_observations (ts);

-- =============================================================================
-- Weather forecasts (raw per weather point, versioned by issued_at)
-- =============================================================================

CREATE TABLE IF NOT EXISTS weather_forecasts (
    area              TEXT            NOT NULL,
    issued_at         TIMESTAMPTZ     NOT NULL,
    ts                TIMESTAMPTZ     NOT NULL,
    temperature_c     DOUBLE PRECISION,
    wind_speed_ms     DOUBLE PRECISION,
    cloud_cover_pct   DOUBLE PRECISION,
    precipitation_mm  DOUBLE PRECISION,
    solar_irradiance_wm2  DOUBLE PRECISION,
    source            TEXT            NOT NULL,
    created_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (area, issued_at, ts, source)
);

CREATE INDEX IF NOT EXISTS idx_weather_forecasts_lookup
    ON weather_forecasts (area, ts, issued_at DESC);

-- =============================================================================
-- Production observations (actual wind/solar MW)
-- =============================================================================

CREATE TABLE IF NOT EXISTS production_observations (
    area            TEXT            NOT NULL,
    ts              TIMESTAMPTZ     NOT NULL,
    wind_mw         DOUBLE PRECISION,
    solar_mw        DOUBLE PRECISION,
    source          TEXT            NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (area, ts, source)
);

CREATE INDEX IF NOT EXISTS idx_production_observations_ts
    ON production_observations (ts);

-- =============================================================================
-- Production forecasts (Energinet official, versioned by issued_at)
-- =============================================================================

CREATE TABLE IF NOT EXISTS production_forecasts (
    area            TEXT            NOT NULL,
    issued_at       TIMESTAMPTZ     NOT NULL,
    ts              TIMESTAMPTZ     NOT NULL,
    wind_mw         DOUBLE PRECISION,
    solar_mw        DOUBLE PRECISION,
    source          TEXT            NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (area, issued_at, ts, source)
);

CREATE INDEX IF NOT EXISTS idx_production_forecasts_lookup
    ON production_forecasts (area, ts, issued_at DESC);

-- =============================================================================
-- Cross-border observations (actual flows)
-- =============================================================================

CREATE TABLE IF NOT EXISTS crossborder_observations (
    connection      TEXT            NOT NULL,
    ts              TIMESTAMPTZ     NOT NULL,
    flow_mw         DOUBLE PRECISION,
    capacity_mw     DOUBLE PRECISION,
    source          TEXT            NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (connection, ts, source)
);

CREATE INDEX IF NOT EXISTS idx_crossborder_observations_ts
    ON crossborder_observations (ts);

-- =============================================================================
-- Cross-border forecasts (versioned by issued_at)
-- =============================================================================

CREATE TABLE IF NOT EXISTS crossborder_forecasts (
    connection      TEXT            NOT NULL,
    issued_at       TIMESTAMPTZ     NOT NULL,
    ts              TIMESTAMPTZ     NOT NULL,
    flow_mw         DOUBLE PRECISION,
    capacity_mw     DOUBLE PRECISION,
    source          TEXT            NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (connection, issued_at, ts, source)
);

CREATE INDEX IF NOT EXISTS idx_crossborder_forecasts_lookup
    ON crossborder_forecasts (connection, ts, issued_at DESC);

-- =============================================================================
-- Model versions
-- =============================================================================

CREATE TABLE IF NOT EXISTS model_versions (
    model_version       TEXT        PRIMARY KEY,
    model_type          TEXT        NOT NULL,
    area                TEXT        NOT NULL,
    horizon_strategy    TEXT        NOT NULL,
    feature_set_version TEXT        NOT NULL,
    trained_at          TIMESTAMPTZ NOT NULL,
    metrics_json        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    artifact_path       TEXT        NOT NULL,
    is_active           BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_model_versions_area_active
    ON model_versions (area, is_active);

-- =============================================================================
-- Forecast runs
-- =============================================================================

CREATE TABLE IF NOT EXISTS forecast_runs (
    run_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    area            TEXT        NOT NULL,
    model_version   TEXT        NOT NULL REFERENCES model_versions(model_version),
    issued_at       TIMESTAMPTZ NOT NULL,
    horizon_hours   INTEGER     NOT NULL CHECK (horizon_hours > 0),
    status          TEXT        NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'scored')),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_forecast_runs_area_issued_at
    ON forecast_runs (area, issued_at DESC);

-- =============================================================================
-- Forecast values
-- =============================================================================

CREATE TABLE IF NOT EXISTS forecast_values (
    run_id                  UUID            NOT NULL REFERENCES forecast_runs(run_id) ON DELETE CASCADE,
    ts                      TIMESTAMPTZ     NOT NULL,
    horizon_hour            INTEGER         NOT NULL CHECK (horizon_hour >= 1),
    predicted_price_dkk_kwh DOUBLE PRECISION NOT NULL,
    lower_dkk_kwh           DOUBLE PRECISION,
    upper_dkk_kwh           DOUBLE PRECISION,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_forecast_values_ts
    ON forecast_values (ts);

CREATE INDEX IF NOT EXISTS idx_forecast_values_run_horizon
    ON forecast_values (run_id, horizon_hour);

-- =============================================================================
-- Forecast errors (per-hour actuals vs predicted)
-- =============================================================================

CREATE TABLE IF NOT EXISTS forecast_errors (
    run_id                  UUID            NOT NULL REFERENCES forecast_runs(run_id) ON DELETE CASCADE,
    ts                      TIMESTAMPTZ     NOT NULL,
    actual_price_dkk_kwh    DOUBLE PRECISION NOT NULL,
    predicted_price_dkk_kwh DOUBLE PRECISION NOT NULL,
    abs_error               DOUBLE PRECISION NOT NULL,
    signed_error            DOUBLE PRECISION NOT NULL,
    sq_error                DOUBLE PRECISION NOT NULL,
    scored_at               TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_forecast_errors_ts
    ON forecast_errors (ts);

-- =============================================================================
-- Forecast run metrics (aggregated per run)
-- =============================================================================

CREATE TABLE IF NOT EXISTS forecast_run_metrics (
    run_id              UUID PRIMARY KEY REFERENCES forecast_runs(run_id) ON DELETE CASCADE,
    mae_24h             DOUBLE PRECISION,
    mae_72h             DOUBLE PRECISION,
    mae_168h            DOUBLE PRECISION,
    rmse_24h            DOUBLE PRECISION,
    rmse_72h            DOUBLE PRECISION,
    rmse_168h           DOUBLE PRECISION,
    bias_24h            DOUBLE PRECISION,
    bias_72h            DOUBLE PRECISION,
    bias_168h           DOUBLE PRECISION,
    hitrate_0_05        DOUBLE PRECISION,
    hitrate_0_10        DOUBLE PRECISION,
    hitrate_0_20        DOUBLE PRECISION,
    quality_label       TEXT CHECK (quality_label IN ('excellent', 'good', 'acceptable', 'poor')),
    worst_hour_ts       TIMESTAMPTZ,
    worst_hour_abs_error DOUBLE PRECISION,
    scored_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_forecast_run_metrics_quality
    ON forecast_run_metrics (quality_label, scored_at DESC);

-- =============================================================================
-- Baseline metrics (per run, ML vs simple baselines)
-- =============================================================================

CREATE TABLE IF NOT EXISTS baseline_metrics (
    run_id                  UUID        NOT NULL REFERENCES forecast_runs(run_id) ON DELETE CASCADE,
    baseline_name           TEXT        NOT NULL,
    mae                     DOUBLE PRECISION,
    rmse                    DOUBLE PRECISION,
    bias                    DOUBLE PRECISION,
    scored_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, baseline_name)
);

-- =============================================================================
-- Job runs (observability)
-- =============================================================================

CREATE TABLE IF NOT EXISTS job_runs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT        NOT NULL CHECK (job_type IN (
        'ingest_prices', 'ingest_weather', 'ingest_production', 'ingest_crossborder',
        'refresh_forecast', 'retrain_model', 'score_forecasts', 'cleanup'
    )),
    status          TEXT        NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    details_json    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_job_runs_type_created_at
    ON job_runs (job_type, created_at DESC);

-- =============================================================================
-- Views
-- =============================================================================

CREATE OR REPLACE VIEW latest_forecast_runs AS
SELECT DISTINCT ON (area)
    run_id, area, model_version, issued_at, horizon_hours,
    status, created_at, updated_at
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
