-- Snapshot-local component state permits merging partial responses while
-- preserving every previously observed aggregate and its availability time.
ALTER TABLE production_forecasts ADD COLUMN components_json JSONB;
