-- Aggregates collected before component tracking remain useful training data.
-- Their receipt time, rather than a response-wide source stamp, bounds availability.
CREATE VIEW production_feature_snapshots AS
SELECT f.*,
    CASE WHEN source = 'energidataservice_forecast'
           OR (source = 'energidataservice_forecast_v2' AND components_json IS NULL)
         THEN created_at ELSE issued_at END AS available_at,
    CASE WHEN source = 'energidataservice_day_ahead_history_v1' THEN 'day_ahead_schedule'
         WHEN source = 'energidataservice_forecast'
           OR (source = 'energidataservice_forecast_v2' AND components_json IS NULL)
         THEN 'legacy_receipt'
         WHEN source = 'energidataservice_forecast_v2' THEN 'observed'
         ELSE 'source_issuance' END AS availability_basis
FROM production_forecasts f;
