import type { ForecastResponse, PricesResponse } from "./api";

export interface CombinedPoint {
  ts: string;
  actual?: number;
  predicted?: number;
  lower?: number;
  upper?: number;
  band?: [number, number];
}

const SLOT_MS = 15 * 60_000;
export const FORECAST_WINDOW_MS = 7 * 24 * 3600_000;

export function priceChartWindow(now = new Date()) {
  const start = Math.floor(now.getTime() / SLOT_MS) * SLOT_MS;
  return { start, end: start + FORECAST_WINDOW_MS };
}

export function buildCombinedData(
  prices: PricesResponse | null,
  forecast: ForecastResponse | null,
  now = new Date(),
): CombinedPoint[] {
  const map = new Map<number, CombinedPoint>();
  const { start, end } = priceChartWindow(now);

  for (const price of prices?.prices ?? []) {
    const time = new Date(price.ts).getTime();
    map.set(time, { ts: price.ts, actual: price.price_dkk_kwh });
  }

  for (const value of forecast?.values ?? []) {
    const time = new Date(value.ts).getTime();
    // Published prices take precedence, including legitimate zero prices.
    if (map.has(time)) continue;
    map.set(time, {
      ts: value.ts,
      predicted: value.predicted_price_dkk_kwh,
      lower: value.lower_dkk_kwh,
      upper: value.upper_dkk_kwh,
      band: [value.lower_dkk_kwh, value.upper_dkk_kwh],
    });
  }

  const points: CombinedPoint[] = [];
  for (let time = start; time <= end; time += SLOT_MS) {
    points.push(map.get(time) ?? { ts: new Date(time).toISOString() });
  }
  return points;
}
