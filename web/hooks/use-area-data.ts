"use client";

import { useState, useEffect, useCallback } from "react";
import { fetchJson, fetchOptionalJson } from "@/lib/client-api";
import { endOfTomorrowInCopenhagen } from "@/lib/copenhagen-time";
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

interface LoadedAreaData extends AreaDetailData {
  prices: PricesResponse | null;
}

type AreaDataErrors = Partial<Record<"prices" | "forecast" | "accuracy" | "benchmarks", string>>;

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
      if (existing.actual == null) {
        existing.predicted = v.predicted_price_dkk_kwh;
        existing.lower = v.lower_dkk_kwh;
        existing.upper = v.upper_dkk_kwh;
        existing.band = [v.lower_dkk_kwh, v.upper_dkk_kwh];
      }
      map.set(v.ts, existing);
    }
  }

  return Array.from(map.values()).sort(
    (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
  );
}

export function useAreaData(area: AreaCode, refreshVersion = 0) {
  const [data, setData] = useState<LoadedAreaData | null>(null);
  const [loading, setLoading] = useState(true);
  const [errors, setErrors] = useState<AreaDataErrors>({});
  const [retryVersion, setRetryVersion] = useState(0);
  const [prevArea, setPrevArea] = useState(area);
  const retry = useCallback(() => setRetryVersion((version) => version + 1), []);

  if (area !== prevArea) {
    setPrevArea(area);
    setLoading(true);
    setData(null);
    setErrors({});
  }

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };

    const now = new Date();
    const start = new Date(now.getTime() - 48 * 3600_000).toISOString();
    const end = endOfTomorrowInCopenhagen(now);

    async function load() {
      setLoading(true);
      const [prices, forecast, accuracy, benchmarks] = await Promise.allSettled([
        fetchJson<PricesResponse>(
          `/api/backend/prices/${area}?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
          options,
        ),
        fetchOptionalJson<ForecastResponse>(`/api/backend/forecasts/${area}/latest`, options),
        fetchJson<AccuracyRecord[]>(
          `/api/backend/forecasts/${area}/accuracy?limit=5`,
          options,
        ),
        fetchOptionalJson<BenchmarkResponse>(
          `/api/backend/forecasts/${area}/benchmarks`,
          options,
        ),
      ]);

      if (controller.signal.aborted) return;

      const nextErrors: AreaDataErrors = {};
      if (prices.status === "rejected") nextErrors.prices = "Kunne ikke hente spotpriser.";
      if (forecast.status === "rejected") nextErrors.forecast = "Kunne ikke hente prognosen.";
      if (accuracy.status === "rejected") nextErrors.accuracy = "Kunne ikke hente prognosenøjagtighed.";
      if (benchmarks.status === "rejected") nextErrors.benchmarks = "Kunne ikke hente model-sammenligningen.";
      setErrors(nextErrors);
      setData((previous) => {
        const nextPrices = prices.status === "fulfilled" ? prices.value : previous?.prices ?? null;
        const nextForecast = forecast.status === "fulfilled" ? forecast.value : previous?.forecast ?? null;
        return {
          prices: nextPrices,
          combined: buildCombinedData(nextPrices, nextForecast),
          forecast: nextForecast,
          productionHorizon: nextForecast?.production_forecast_horizon ?? null,
          accuracy: accuracy.status === "fulfilled" ? accuracy.value : previous?.accuracy ?? [],
          benchmarks: benchmarks.status === "fulfilled" ? benchmarks.value : previous?.benchmarks ?? null,
        };
      });
      setLoading(false);
    }

    load();
    return () => {
      controller.abort();
    };
  }, [area, refreshVersion, retryVersion]);

  return { data, loading, errors, retry };
}
