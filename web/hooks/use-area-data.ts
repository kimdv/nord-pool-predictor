"use client";

import { useState, useEffect } from "react";
import type { AreaCode } from "@/components/area-switcher";
import type {
  ForecastResponse,
  AccuracyRecord,
  BenchmarkResponse,
  PricesResponse,
} from "@/lib/api";

export interface CombinedPoint {
  ts: string;
  actual?: number;
  predicted?: number;
  lower?: number;
  upper?: number;
  band?: [number, number];
}

export interface AreaDetailData {
  combined: CombinedPoint[];
  forecast: ForecastResponse | null;
  productionHorizon: string | null;
  accuracy: AccuracyRecord[];
  benchmarks: BenchmarkResponse | null;
}

async function fetchJson<T>(url: string): Promise<T | null> {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

function buildCombinedData(
  prices: PricesResponse | null,
  forecast: ForecastResponse | null,
): CombinedPoint[] {
  const map = new Map<string, CombinedPoint>();

  if (prices?.prices) {
    for (const p of prices.prices) {
      map.set(p.ts, { ts: p.ts, actual: p.price_dkk_kwh });
    }
  }
  if (forecast?.values) {
    for (const v of forecast.values) {
      const existing = map.get(v.ts) ?? { ts: v.ts };
      existing.predicted = v.predicted_price_dkk_kwh;
      existing.lower = v.lower_dkk_kwh;
      existing.upper = v.upper_dkk_kwh;
      existing.band = [v.lower_dkk_kwh, v.upper_dkk_kwh];
      map.set(v.ts, existing);
    }
  }

  return Array.from(map.values()).sort(
    (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
  );
}

export function useAreaData(area: AreaCode) {
  const [data, setData] = useState<AreaDetailData | null>(null);
  const [loading, setLoading] = useState(true);
  const [prevArea, setPrevArea] = useState(area);

  if (area !== prevArea) {
    setPrevArea(area);
    setLoading(true);
    setData(null);
  }

  useEffect(() => {
    let cancelled = false;

    const now = new Date();
    const start = new Date(now.getTime() - 48 * 3600_000).toISOString();
    const end = now.toISOString();

    async function load() {
      const [prices, forecast, accuracy, benchmarks] = await Promise.all([
        fetchJson<PricesResponse>(
          `/api/backend/prices/${area}?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
        ),
        fetchJson<ForecastResponse>(`/api/backend/forecasts/${area}/latest`),
        fetchJson<AccuracyRecord[]>(
          `/api/backend/forecasts/${area}/accuracy?limit=5`,
        ),
        fetchJson<BenchmarkResponse>(
          `/api/backend/forecasts/${area}/benchmarks`,
        ),
      ]);

      if (cancelled) return;

      setData({
        combined: buildCombinedData(prices, forecast),
        forecast,
        productionHorizon: forecast?.production_forecast_horizon ?? null,
        accuracy: accuracy ?? [],
        benchmarks,
      });
      setLoading(false);
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [area]);

  return { data, loading };
}
