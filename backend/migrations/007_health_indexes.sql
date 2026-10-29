-- Support frequent freshness probes without scanning complete observation tables.
CREATE INDEX idx_price_observations_created ON price_observations (created_at DESC);
CREATE INDEX idx_weather_forecasts_created ON weather_forecasts (created_at DESC);
CREATE INDEX idx_production_observations_created ON production_observations (created_at DESC);
CREATE INDEX idx_crossborder_observations_created ON crossborder_observations (created_at DESC);
