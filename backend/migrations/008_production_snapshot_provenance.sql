-- Existing snapshots have no trustworthy component-level issuance metadata.
-- Corrected ingestion populates this column on refresh, without inventing timestamps.
ALTER TABLE production_forecasts ADD COLUMN oldest_component_issued_at TIMESTAMPTZ;
