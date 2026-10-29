"use client";

import { useEffect } from "react";
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
}: {
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
      <div className="space-y-6">
        <ChartSkeleton />
        <ChartSkeleton height="h-48" />
        <ChartSkeleton height="h-48" />
      </div>
    );
  }

  return (
    <div className="space-y-6" aria-busy={loading}>
      {(errors.prices || errors.forecast) && (
        <RequestError message={[errors.prices, errors.forecast].filter(Boolean).join(" ")} onRetry={retry} retrying={loading} />
      )}
      {(!(errors.prices || errors.forecast) || data.combined.length > 0) && (
        <PriceChart data={data.combined} productionHorizon={data.productionHorizon} />
      )}
      {errors.accuracy && <RequestError message={errors.accuracy} onRetry={retry} retrying={loading} />}
      {(!errors.accuracy || data.accuracy.length > 0) && <AccuracyCard records={data.accuracy} />}
      {errors.benchmarks && <RequestError message={errors.benchmarks} onRetry={retry} retrying={loading} />}
      {data.benchmarks && <BenchmarkTable benchmarks={data.benchmarks} />}
    </div>
  );
}
