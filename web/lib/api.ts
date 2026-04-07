const SERVER_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${SERVER_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`);
  return res.json() as Promise<T>;
}

/* ---------- Types ---------- */

export interface PricePoint {
  ts: string;
  price_dkk_kwh: number;
}

export interface PricesResponse {
  area: string;
  prices: PricePoint[];
}

export interface ForecastValue {
  ts: string;
  predicted_price_dkk_kwh: number;
  lower_dkk_kwh: number;
  upper_dkk_kwh: number;
}

export interface ForecastResponse {
  run_id: string;
  area: string;
  model_version: string;
  issued_at: string;
  status: string;
  notes: string | null;
  values: ForecastValue[];
  production_forecast_horizon: string | null;
}

export interface AccuracyRecord {
  run_id: string;
  issued_at: string;
  mae_24h: number | null;
  rmse_24h: number | null;
  bias_24h: number | null;
  quality_label: string | null;
}

export interface Baseline {
  baseline_name: string;
  mae: number;
  rmse: number;
  bias: number;
}

export interface BenchmarkResponse {
  run_id: string;
  area: string;
  ml_mae: number | null;
  ml_rmse: number | null;
  baselines: Baseline[];
}

export interface AreaInfo {
  code: string;
  label: string;
  weather_points: { id: string; name: string; lat: number; lon: number }[];
}

export interface SourceStatus {
  source: string;
  last_updated: string | null;
  is_stale: boolean;
}

export interface HealthResponse {
  status: string;
  sources: SourceStatus[];
  degraded: boolean;
  last_forecast_at: string | null;
  bootstrapping: boolean;
}

export interface HAForecastEntry {
  start: string;
  price: number;
}

export interface HAResponse {
  state: string;
  attributes: {
    current_price: number | null;
    next_slot_price: number | null;
    today_min: number | null;
    today_max: number | null;
    today_average: number | null;
    forecast: HAForecastEntry[];
    model_version: string;
    forecast_quality: string | null;
    mae_24h: number | null;
  };
}

/* ---------- Fetchers (server-side only) ---------- */

export function fetchPrices(area: string, start: string, end: string) {
  return get<PricesResponse>(
    `/api/prices/${area}?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
  );
}

export function fetchLatestForecast(area: string) {
  return get<ForecastResponse>(`/api/forecasts/${area}/latest`);
}

export function fetchAccuracy(area: string, limit = 5) {
  return get<AccuracyRecord[]>(`/api/forecasts/${area}/accuracy?limit=${limit}`);
}

export function fetchBenchmarks(area: string) {
  return get<BenchmarkResponse>(`/api/forecasts/${area}/benchmarks`);
}

export function fetchAreas() {
  return get<AreaInfo[]>("/api/areas");
}

export function fetchHealth() {
  return get<HealthResponse>("/api/health");
}

export function fetchHA(area: string) {
  return get<HAResponse>(`/api/ha/${area}`);
}
