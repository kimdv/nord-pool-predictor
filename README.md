# Nord Pool Price Predictor

A local Nord Pool day-ahead electricity price forecasting system for the Danish bidding zones DK1 and DK2. Built with a FastAPI backend, LightGBM ML pipeline, and a Next.js dashboard.

> **Disclaimer**
>
> This project is **100% vibe coded** and provided as-is for hobby and experimentation purposes.
> Price forecasts can be wrong.
> Do not rely on this as the only source for financial, trading, or operational decisions.
> **Use at your own risk.**

## Overview

The system ingests hourly spot prices, weather forecasts, wind/solar production data, and cross-border electricity flows. It trains quantile LightGBM models (p10/p50/p90) to predict prices with confidence bands for 8 days, including a 1-day buffer between refreshes. The dashboard shows the next 7 days (168 hours), using actual spot prices wherever available and forecasts for the remaining slots. The system evaluates forecast accuracy against actuals and naive baselines, and exposes everything through a REST API consumed by the dashboard and Home Assistant.

### Architecture

```text
Energi Data Service ──┐
                     ├──▶ Worker (scheduler, ingestion, LightGBM)
Open-Meteo ──────────┘                    │
                                          ▼
                             PostgreSQL (data + durable jobs)
                                          ▲
                                          │
                             FastAPI :8000 (reads + job submission)
                                      ▲       ▲
                                      │       │
                               Next.js :3000  Home Assistant
```

The API fetches tariff data on demand. The worker owns all scheduled and manually
queued ingestion, training, and forecast execution.

### Data Sources

All data sources are free and require no API keys:

- **Prices**: [Energi Data Service](https://api.energidataservice.dk/) — hourly DKK/MWh spot prices for DK1 and DK2
- **Weather**: [Open-Meteo](https://open-meteo.com/) — temperature, wind speed, cloud cover, precipitation, solar irradiation for a configurable list of weather points across Denmark
- **Production**: Energi Data Service — actual and forecast wind/solar power production
- **Cross-border flows**: Energi Data Service — electricity flows between DK and neighbouring zones

## Quick Start

### Prerequisites

- Docker and Docker Compose

### Run

These commands are for a fresh installation. Before updating an existing database,
follow the [storage inventory, backup, and migration steps](docs/nas-recovery.md).
PostgreSQL 18 uses a different volume target; changing the mount is not a migration.

```bash
# Clone the repository
git clone https://github.com/kimdv/nord-pool-predictor.git
cd nord-pool-predictor

# Start all services
docker compose up -d

# View logs
docker compose logs -f app
```

On first startup the worker will:
1. Run database migrations
2. Backfill 365 days of historical data (prices, production, cross-border flows)
3. Train initial models for DK1 and DK2
4. Run the first forecast

This takes **15–30 minutes** depending on your hardware. Progress is logged to the console and the `job_runs` table.

Once ready:
- **Dashboard**: http://localhost:3000
- **API**: http://localhost:8000
- **API docs**: http://localhost:8000/docs

### Environment Variables

Copy `backend/.env.example` to `backend/.env` and adjust as needed:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://nordpool:nordpool@localhost:5432/nordpool` | PostgreSQL connection string |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `MODEL_DIR` | `/data/models` | Path for model artifact storage |
| `TRAINING_WINDOW_DAYS` | `365` | Days of history used for training |
| `RETENTION_WEATHER_OBS_DAYS` | `180` | Retention period for weather observations |
| `RETENTION_FORECAST_DAYS` | `365` | Retention period for forecasts and errors |

## Daily Schedule

All times are in CET/CEST (Europe/Copenhagen):

| Time | Job | Description |
|------|-----|-------------|
| 13:15 | `ingest_prices` | Fetch tomorrow's day-ahead prices |
| 13:20 | `ingest_weather` | Fetch latest weather forecasts for all zones |
| 13:30 | `ingest_crossborder` | Fetch cross-border flow data |
| 17:55 | `ingest_production` | Fetch wind/solar production forecasts |
| 00:05, 06:05, 12:05, 18:05 | `refresh_forecast` | Predict 8 days, including a 1-day buffer to keep the dashboard's next 7 days covered |
| 06:00 | `score_forecasts` | Compare yesterday's predictions to actuals |
| Sun 03:00 | `cleanup` | Trim old data beyond retention windows |
| Sun 04:00 | `retrain_model` | Retrain models with Optuna hyperparameter tuning |

## API Endpoints

### Prices & Forecasts

- `GET /api/prices/{area}?start=...&end=...` — Historical spot prices
- `GET /api/tariffs/breakdown/{area}?gln=...&code=...` — Today’s price breakdown. Add `history_end=<timezone-aware quarter-hour timestamp>` to include `historical_slots` from the preceding 168 hours, using published spot prices and tariffs valid on each date. Unavailable historical days are omitted.
- `GET /api/forecasts/{area}/latest` — Latest forecast with p10/p50/p90 bands
- `GET /api/forecasts/{area}/accuracy` — Recent forecast accuracy metrics
- `GET /api/forecasts/{area}/benchmarks` — ML vs baseline model comparison
- `GET /api/forecasts/{area}/snapshots` — How the forecast evolved over time

### System

- `GET /api/health` — Service health and data freshness indicators
- `GET /api/areas` — Configured bidding zones and weather points
- `GET /api/models` — Model versions with training metrics
- `GET /api/jobs` — Recent job run history

### Home Assistant

- `GET /api/ha/{area}` — HA REST sensor compatible JSON

## Home Assistant Integration

Add this to your Home Assistant `configuration.yaml`:

```yaml
sensor:
  - platform: rest
    name: Nordpool Forecast DK1
    resource: http://<your-host>:8000/api/ha/DK1
    value_template: "{{ value_json.state }}"
    json_attributes_path: "$.attributes"
    json_attributes:
      - forecast
      - today_min
      - today_max
      - today_average
      - forecast_quality
    scan_interval: 900

  - platform: rest
    name: Nordpool Forecast DK2
    resource: http://<your-host>:8000/api/ha/DK2
    value_template: "{{ value_json.state }}"
    json_attributes_path: "$.attributes"
    json_attributes:
      - forecast
      - today_min
      - today_max
      - today_average
      - forecast_quality
    scan_interval: 900
```

Replace `<your-host>` with the IP or hostname of the machine running the Docker stack.

## Deployment

For an existing NAS installation recovering from failed training/forecast jobs,
follow [the NAS recovery guide](docs/nas-recovery.md). Current deployments require
one background worker sharing the API's backend image digest and model storage.

### Standalone (recommended)

Use the provided `docker-compose.yml` which defines four services: `postgres`, `app`, `worker`, and `web` on isolated Docker networks.

### Existing Docker Compose setup

If you already have services running, add the Nord Pool stack on dedicated networks:

The example below retains the PostgreSQL 16 storage layout. For an existing Nord
Pool database, preserve its image and storage until completing the recovery
guide's backup and migration procedure; do not change only the image or mount.

```yaml
services:
  nordpool-db:
    image: postgres:16-alpine
    restart: always
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $NORDPOOL_DB_USER -d nordpool"]
      interval: 10s
      timeout: 5s
      retries: 5
    environment:
      - POSTGRES_DB=nordpool
      - POSTGRES_USER=${NORDPOOL_DB_USER}
      - POSTGRES_PASSWORD=${NORDPOOL_DB_PASSWORD}
    volumes:
      - ./nordpool/postgres:/var/lib/postgresql/data
    networks:
      - nordpool-backend

  nordpool-app:
    image: ghcr.io/kimdv/nord-pool-predictor-app:latest
    restart: always
    depends_on:
      nordpool-db:
        condition: service_healthy
    environment:
      - DATABASE_URL=postgresql+asyncpg://${NORDPOOL_DB_USER}:${NORDPOOL_DB_PASSWORD}@nordpool-db:5432/nordpool
      - CORS_ALLOWED_ORIGINS=http://localhost:3000
    volumes:
      - ./nordpool/models:/data/models
    ports:
      - "8000:8000"
    networks:
      - nordpool-backend
      - nordpool-public

  nordpool-worker:
    image: ghcr.io/kimdv/nord-pool-predictor-app:latest
    command: ["python", "-m", "nordpool_predictor.worker"]
    restart: always
    stop_grace_period: 30m
    depends_on:
      nordpool-db:
        condition: service_healthy
    environment:
      - DATABASE_URL=postgresql+asyncpg://${NORDPOOL_DB_USER}:${NORDPOOL_DB_PASSWORD}@nordpool-db:5432/nordpool
      - MODEL_DIR=/data/models
    volumes:
      - ./nordpool/models:/data/models
    networks:
      - nordpool-backend
      - nordpool-public

  nordpool-web:
    image: ghcr.io/kimdv/nord-pool-predictor-web:latest
    restart: always
    depends_on:
      nordpool-app:
        condition: service_started
    environment:
      - API_BASE_URL=http://nordpool-app:8000
    ports:
      - "3000:3000"
    networks:
      - nordpool-public

networks:
  nordpool-backend:
    internal: true
  nordpool-public:
```

## Adding Weather Points

Edit `backend/areas.yaml` to add new weather points or bidding zones:

```yaml
areas:
  DK1:
    label: Vest
    weather_points:
      - id: DK1_AARHUS
        name: Aarhus
        lat: 56.15
        lon: 10.21
      # Add more points here
```

No code changes are needed — the ingestion, ML, and API layers read from this configuration automatically.

## Development

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

### Frontend

```bash
cd web
npm install
npm run dev
```

## License

[MIT](LICENSE)

### Background worker

The API only serves requests and submits jobs to PostgreSQL. Run exactly one
`python -m nordpool_predictor.worker` process alongside it; Compose includes this
service. The worker owns scheduling, bootstrap, ingestion and ML execution, and
holds a PostgreSQL advisory lock for its lifetime. A second worker exits without
executing jobs. API processes can scale independently.

Manual triggers return HTTP 202 with a persistent `job_id`. Poll
`GET /api/jobs/requests/{job_id}` for pending/running/cancelling/cancelled/completed/failed status.
Cancel queued or running work with the dashboard's **Annuller** button or
`POST /api/jobs/cancel/{job_name}` (including `bootstrap`). Pending requests are
cancelled immediately; running requests enter `cancelling` until the worker has
killed and reaped their job process, normally within a second. Cancellation keeps
already committed data and does not roll back finished areas or published models.
Stale running history can also be cleared with this endpoint. Deploy the API and
worker together so both use the cancellation protocol.
Duplicate queued, running or cancelling jobs return HTTP 409. Pending jobs survive restarts;
interrupted running jobs are marked failed when the worker restarts. Scheduled
runs enqueue the same job types and share those duplicate controls. Shutdown
waits for the current job; configure a suitable grace period for long training.

### Feature availability

Feature set `v3-copenhagen` derives hour, quarter-hour, weekday, month, weekend,
and Danish-holiday features in `Europe/Copenhagen`, including daylight saving
changes. Storage and feature-matrix indexes remain UTC. It uses price rolling
statistics anchored strictly before issuance and selects weather, production,
and cross-border forecast snapshots
available no later than that issuance. Production feature readers share the
`production_feature_snapshots` compatibility view: legacy aggregate snapshots use
their recorded `created_at` as conservative availability; component-backed snapshots
use their observation-based `issued_at`. Stored values and timestamps remain unchanged.
Historical observations are not substituted for future covariates.
Training, forecast selection, and worker readiness accept only `v3-copenhagen`.
Earlier models, including `v2-asof` models with UTC calendar features, are excluded;
worker bootstrap retrains them before generating new forecasts. Historical models
and forecasts remain stored.

Training metrics report non-null wind/solar sample counts, coverage fractions, and
snapshot availability provenance. Sparse history and a missing wind or solar input
are accepted. Only when both inputs are entirely absent does the durable retraining
job attempt a historical forecast backfill, then rebuild its training matrix once.
Bootstrap and retraining share that one retrieval attempt across configured areas,
including empty or failed retrievals; a later job can retry.
Failed retrieval or still-missing inputs preserve a usable active model. With no
usable model, first-install training can retain its baseline behavior and explicitly
reports `baseline_without_production` in model metrics.

The fallback requests at most 365 days in monthly chunks, using only
`Forecasts_Hour.ForecastDayAhead`, stored idempotently under the separate
`energidataservice_day_ahead_history_v1` source. Availability follows Energinet's
documented 18:00 Europe/Copenhagen publication on the preceding local day, converted
to UTC ([dataset metadata](https://api.energidataservice.dk/meta/dataset/Forecasts_Hour)).
`ForecastCurrent` and its `TimestampUTC` do not reconstruct historical availability.
No production audit, deletion, mandatory recollection, or unconditional upgrade
backfill is required.
Coverage reported for an existing price run excludes reconstructed history imported
after that run's issuance, even when its scheduled publication was earlier.

On each start the worker checks every configured area independently. Existing
models are reused only when their feature set and all quantile artifacts are
usable; missing or expired forecasts are regenerated. If training is required,
idempotent price, production and cross-border backfills restore the full configured
history rather than trusting another area's newest timestamp. Training uses
issuance-safe weather forecasts, so archive-weather observations are not fetched
or required. Per-area failures are recorded as a failed bootstrap request, and a
restart resumes the remaining areas.
Replacement forecasts require a complete eight-day quarter-hour price history
for their area. Bootstrap checks persisted coverage even when ingestion returns
successfully, and missing history does not block recovery for other areas.
Each replacement uses one issuance cutoff, captured after live covariate ingestion,
for both price validation and prediction. Readiness is checked again after recovery
so forecasts that expired while another area was recovering are regenerated.
Each area gets one recovery attempt per bootstrap; remaining gaps fail the job.
Replacement forecasts pin the model version validated by bootstrap. If that
version is no longer active, recovery fails instead of selecting another model.
Bootstrap and unpinned refreshes share artifact-aware selection: unusable active
versions are skipped in favor of the newest compatible version that can load.
Pinned forecasts never fall back. If no candidate is usable, the refresh fails
without creating a forecast.

### Accuracy evaluation

Model registration reports a chronological 20% holdout that is excluded from
hyperparameter tuning and evaluation-model fitting; production models are then
fit on the full training window. Development labels at or after the earliest
simulated holdout issuance are purged before tuning and evaluation. Live forecasts
are rescored as actual prices arrive. Horizon metrics stay null until that horizon is fully covered, and the
accuracy API reports `coverage_24h`, `coverage_72h`, and `coverage_168h`. A run
becomes `scored` only after all its values have actual prices. Benchmarks use the
latest run with a complete 24-hour evaluation and compare baselines over that
same period using prices available by issuance.

### History pagination

Forecast history returns 14 runs by default (maximum 100), with values loaded in
one batch. Model history defaults to 20 versions (maximum 100). Both retain their
array response shape and return `X-Next-Cursor` when another page exists; pass it
as `cursor` on the next request with the same filters. Ordering includes a unique
identifier so equal timestamps do not skip records. The web proxy and CORS
configuration expose this header.

Prices default to a seven-day window ending two days from now, with 1,000 rows
per page (maximum 5,000). Use timezone-aware `start` and `end` (at most 366 days
apart), and pass the response's `next_cursor` as `after` for the next page while
keeping the same date bounds.

Model registrations now point to an immutable JSON artifact manifest relative to
`MODEL_DIR`, listing all quantile files and persisted feature importance. Artifact
and manifest writes are atomic; registration happens only after the complete
bundle is written. Existing directory registrations remain readable through the
same shared loader. Model objects use a bounded per-process cache, and model
loading/prediction run outside the API event loop.

### Health probes

Production forecast snapshots retain issuance timestamps per area, target hour, and
source snapshot. Legacy `energidataservice_forecast` rows used response-wide stamps
and are excluded from live readiness; startup refetches corrected
snapshots under `energidataservice_forecast_v2`. Missing source timestamps are not
replaced with fetch time. Production observations preserve missing values and genuine
zeroes; reingestion corrects previously stored observations.
Updates carry forward the components already known for that hour. A snapshot becomes
available when its component updates are observed; readiness uses its oldest source timestamp
so fresh wind data cannot mask stale solar data (or vice versa).
Snapshot-local component state carries missing response fields forward and lets late
arrivals or same-timestamp revisions append a new snapshot without changing historical
inputs. Unchanged, stale, and future-dated updates do not advance availability.
Live component merging and readiness require component state; historical training
and prediction features can use older aggregates through the compatibility view.

`GET /api/health/live` checks process responsiveness without database queries.
`GET /api/health/ready` returns HTTP 503 when the database fails, bootstrap is
pending/running, source data is stale, or any configured area lacks a recent
successful forecast with future values. Docker uses readiness for app health;
the dashboard starts once the app starts so it remains accessible during bootstrap.
The existing `/api/health` remains a detailed dashboard status endpoint.
Failed or running forecasts do not advance successful forecast freshness, and
`last_forecast_at` is the oldest latest-success timestamp across configured areas.
It is null when any configured area has no successful forecast.
Startup recovery regenerates successful forecasts older than the same 26-hour limit
and refreshes production observations and cross-border flows when historical backfills
were skipped or failed.
Weather, production forecasts, and recent prices are refreshed even when every
area already has a fresh price forecast, without regenerating those forecasts.
Source freshness uses observation times for each configured area's prices and production,
and each supported cross-border connection touching a configured area, including shared
connections when only one endpoint is configured. Weather is checked for every configured
weather point. Weather and production forecasts must have an issuance within 26 hours,
available now, covering the hourly bucket of the next quarter-hour prediction target.
The latest snapshot must contain a usable measurement; an all-null snapshot cannot
fall back to an older populated snapshot during readiness checks.
Each source reports its oldest required input timestamp, or no timestamp if an input
is missing. Existing owner/timestamp indexes support these checks without new index builds.

### Price availability

Zero is a valid price. Actual zero/negative prices take precedence over forecasts.
Home Assistant reports `state: "unavailable"` when the current slot has neither
an actual nor a forecast; `available`, `current_price_source`, and `today_coverage`
explain the data available. Daily aggregates require a complete Copenhagen local
day, including its 92/100 slots at daylight-saving transitions.

Tariff slots carry `spot_source` (`actual`, `forecast`, or `missing`). Missing spot
prices and their totals are null, and the dashboard shows chart gaps and labels
forecast estimates. Missing official tariff records return an upstream error
instead of silently substituting zero or guessed tariff constants.
Tariff lookups and cache entries use the same Copenhagen calendar date as the
spot-price slots. Records must be valid on that date (`ValidFrom` inclusive,
`ValidTo` exclusive), so crossing midnight during a request cannot mix tariff days.
