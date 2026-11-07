"use client";

import { useState, useEffect, useCallback } from "react";
import { todayInCopenhagen } from "@/lib/copenhagen-time";
import { fetchJson, fetchOptionalJson } from "@/lib/client-api";
import { buildCombinedData, priceChartWindow, type CombinedPoint } from "@/lib/combined-price-data";
import { historicalRange, historyWindow, type PriceRange } from "@/lib/price-history";
import type { AreaCode } from "@/components/area-switcher";
import type {
  ForecastResponse,
  AccuracyRecord,
  BenchmarkResponse,
  PricesResponse,
} from "@/lib/api";

export type { CombinedPoint } from "@/lib/combined-price-data";

export interface AreaDetailData {
  combined: CombinedPoint[];
  today: CombinedPoint[];
  historicalRange: PriceRange | null;
  forecast: ForecastResponse | null;
  productionHorizon: string | null;
  accuracy: AccuracyRecord[];
  benchmarks: BenchmarkResponse | null;
}

interface LoadedAreaData extends AreaDetailData {
  prices: PricesResponse | null;
}

type AreaDataErrors = Partial<Record<"prices" | "forecast" | "accuracy" | "benchmarks", string>>;

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

    async function load() {
      const now = new Date();
      const window = priceChartWindow(now);
      const today = todayInCopenhagen(now);
      const history = historyWindow(now);
      const start = new Date(history.start).toISOString();
      const end = new Date(window.end).toISOString();
      setLoading(true);
      const [prices, forecast, accuracy, benchmarks] = await Promise.allSettled([
        fetchJson<PricesResponse>(
          `/api/backend/prices/${area}?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&limit=5000`,
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
          combined: buildCombinedData(nextPrices, nextForecast, now),
          today: buildCombinedData(nextPrices, nextForecast, now, { start: today.start, end: today.end - 1 }),
          historicalRange: historicalRange((nextPrices?.prices ?? []).map((point) => ({
            ts: point.ts, value: point.price_dkk_kwh,
          })), history.end),
          forecast: nextForecast,
          productionHorizon: nextForecast?.production_forecast_horizon ?? null,
          accuracy: accuracy.status === "fulfilled" ? accuracy.value : previous?.accuracy ?? [],
          benchmarks: benchmarks.status === "fulfilled" ? benchmarks.value : previous?.benchmarks ?? null,
        };
      });
      setLoading(false);
    }

    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      await load();
      if (!controller.signal.aborted) timer = setTimeout(refresh, 60_000);
    }
    refresh();
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [area, refreshVersion, retryVersion]);

  return { data, loading, errors, retry };
}
