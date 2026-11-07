"use client";

import { useEffect, type ReactNode } from "react";
import type { AreaCode } from "@/components/area-switcher";
import { useAreaData } from "@/hooks/use-area-data";
import ChartSkeleton from "./chart-skeleton";
import PriceChart from "./price-chart";
import AccuracyCard from "./accuracy-card";
import BenchmarkTable from "./benchmark-table";
import RequestError from "./request-error";
import type { ForecastResponse } from "@/lib/api";

export default function AreaCharts({
  area,
  refreshVersion = 0,
  onForecast,
  overview,
  children,
}: {
  overview: ReactNode;
  children: ReactNode;
  area: AreaCode;
  refreshVersion?: number;
  onForecast?: (forecast: ForecastResponse | null) => void;
}) {
  const { data, loading, errors, retry } = useAreaData(area, refreshVersion);
  const forecast = data?.forecast ?? null;

  useEffect(() => {
    onForecast?.(forecast);
  }, [forecast, onForecast]);

  if (!data) {
    return (
      <div className="space-y-8" aria-busy="true" aria-label="Henter prisdata">
        <div className="price-stage">
          {overview}
          <div className="forecast-panel"><ChartSkeleton height="h-96" /></div>
        </div>
        <section className="card p-6" aria-label="Syvdagesprognose"><ChartSkeleton height="h-96" /></section>
        {children}
      </div>
    );
  }

  return (
    <div className="space-y-6" aria-busy={loading}>
      <div className="price-stage">
        {overview}
        <div className="forecast-panel">
          {(errors.prices || errors.forecast) && (
            <RequestError
              message={[errors.prices, errors.forecast].filter(Boolean).join(" ")}
              onRetry={retry}
              retrying={loading}
            />
          )}
          {(!(errors.prices || errors.forecast) || data.today.length > 0) && (
            <PriceChart data={data.today} period="today" historicalRange={null} />
          )}
        </div>
      </div>
      <section className="card p-6" aria-label="Syvdagesprognose">
        {errors.forecast && <RequestError message={errors.forecast} onRetry={retry} retrying={loading} />}
        <PriceChart data={data.combined} productionHorizon={data.productionHorizon} historicalRange={data.historicalRange} />
      </section>
      {children}
      <details id="analyse" className="forecast-analysis">
        <summary>Prognosens præcision</summary>
        <div className="analysis-grid">
          {errors.accuracy && (
            <RequestError message={errors.accuracy} onRetry={retry} retrying={loading} />
          )}
          {(!errors.accuracy || data.accuracy.length > 0) && (
            <AccuracyCard records={data.accuracy} />
          )}
          {errors.benchmarks && (
            <RequestError message={errors.benchmarks} onRetry={retry} retrying={loading} />
          )}
          {data.benchmarks && <BenchmarkTable benchmarks={data.benchmarks} />}
        </div>
      </details>
    </div>
  );
}
