"""Shared freshness policy for health probes and startup recovery."""

from datetime import timedelta

FORECAST_MAX_AGE = timedelta(hours=26)
