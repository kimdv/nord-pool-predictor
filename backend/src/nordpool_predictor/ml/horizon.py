"""Generate a buffer so the dashboard retains seven days between refreshes."""

DISPLAY_HORIZON_DAYS = 7
FORECAST_BUFFER_DAYS = 1
DEFAULT_HORIZON_STEPS = (DISPLAY_HORIZON_DAYS + FORECAST_BUFFER_DAYS) * 96
SHORT_HORIZON_STEPS = 2 * 96
