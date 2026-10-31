-- Old rolling/covariate semantics cannot be used with the point-in-time feature builder.
-- Preserve history while allowing worker bootstrap to train compatible models.
UPDATE model_versions SET is_active = FALSE WHERE feature_set_version <> 'v2-asof';
