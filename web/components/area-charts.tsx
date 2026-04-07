"use client";

import { useEffect } from "react";
import type { AreaCode } from "@/components/area-switcher";
import { useAreaData } from "@/hooks/use-area-data";
import ChartSkeleton from "./chart-skeleton";
import PriceChart from "./price-chart";
import AccuracyCard from "./accuracy-card";
import BenchmarkTable from "./benchmark-table";
import type { ForecastResponse } from "@/lib/api";

export default function AreaCharts({
  area,
  onForecast,
}: {
  area: AreaCode;
  onForecast?: (forecast: ForecastResponse | null) => void;
}) {
  const { data, loading } = useAreaData(area);

  const forecast = data?.forecast ?? null;
  useEffect(() => {
    onForecast?.(forecast);
  }, [forecast, onForecast]);

  if (loading || !data) {
    return (
      <div className="space-y-6">
        <ChartSkeleton />
        <ChartSkeleton height="h-48" />
        <ChartSkeleton height="h-48" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PriceChart data={data.combined} productionHorizon={data.productionHorizon} />
      <AccuracyCard records={data.accuracy} />
      {data.benchmarks && <BenchmarkTable benchmarks={data.benchmarks} />}
    </div>
  );
}
