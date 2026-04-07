# Nord Pool Price Predictor

A local Nord Pool day-ahead electricity price forecasting system for the Danish bidding zones DK1 and DK2. Built with a FastAPI backend, LightGBM ML pipeline, and a Next.js dashboard.

> **Disclaimer**
>
> This project is **100% vibe coded** and provided as-is for hobby and experimentation purposes.
> Price forecasts can be wrong.
> Do not rely on this as the only source for financial, trading, or operational decisions.
> **Use at your own risk.**

## Overview

The system ingests hourly spot prices, weather forecasts, wind/solar production data, and cross-border electricity flows. It trains quantile LightGBM models (p10/p50/p90) to predict the next 7 days (168 hours) of prices with confidence bands, evaluates forecast accuracy against actuals and naive baselines, and exposes everything through a REST API consumed by a web dashboard and Home Assistant.

### Architecture

```
┌─────────────────────┐     ┌─────────────────────────────────────┐
│  Energi Data Service │────▶│           Docker Host               │
│  (prices, production)│     │  ┌────────────────────────────────┐ │
└─────────────────────┘     │  │ nordpool-app (FastAPI)          │ │
                            │  │  • APScheduler (daily jobs)     │ │
┌─────────────────────┐     │  │  • LightGBM ML pipeline        │ │
│     Open-Meteo       │────▶│  │  • REST API on :8000           │ │
│  (weather forecasts) │     │  └──────────┬─────────────────────┘ │
└─────────────────────┘     │             │                        │
                            │  ┌──────────▼───────┐                │
                            │  │ PostgreSQL 16     │                │
                            │  └──────────────────┘                │
                            └─────────────────────────────────────┘
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
                 ┌─────────────┐             ┌──────────────┐
                 │  Next.js    │             │   Home       │
                 │  Dashboard  │             │   Assistant  │
                 │  :3000      │             │              │
                 └─────────────┘             └──────────────┘
```

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

```bash
# Clone the repository
git clone https://github.com/kimdv/nord-pool-predictor.git
cd nord-pool-predictor

# Start all services
docker compose up -d

# View logs
docker compose logs -f app
```

On first startup the app will:
1. Run database migrations
2. Backfill 365 days of historical data (prices, weather, production, cross-border flows)
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
| 13:25 | `ingest_production` | Fetch wind/solar production forecasts |
| 13:25 | `ingest_crossborder` | Fetch cross-border flow data |
| 13:30 | `refresh_forecast` | Run ML prediction for next 48 hours |
| 06:00 | `score_forecasts` | Compare yesterday's predictions to actuals |
| Sun 03:00 | `cleanup` | Trim old data beyond retention windows |
| Sun 04:00 | `retrain_model` | Retrain models with Optuna hyperparameter tuning |

## API Endpoints

### Prices & Forecasts

- `GET /api/prices/{area}?start=...&end=...` — Historical spot prices
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

### Standalone (recommended)

Use the provided `docker-compose.yml` which defines three services: `postgres`, `app`, and `web` on isolated Docker networks.

### Existing Docker Compose setup

If you already have services running, add the Nord Pool stack on dedicated networks:

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

  nordpool-web:
    image: ghcr.io/kimdv/nord-pool-predictor-web:latest
    restart: always
    depends_on:
      nordpool-app:
        condition: service_healthy
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
