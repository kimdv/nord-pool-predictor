"use client";

import { useCallback, useState } from "react";
import AreaSwitcher, { useSelectedArea } from "@/components/area-switcher";
import StatCard from "@/components/stat-card";
import JobStatus from "@/components/job-status";
import AreaCharts from "@/components/area-charts";
import PriceBreakdownChart from "@/components/price-breakdown-chart";
import GridCompanySelector, {
  useGridSelection,
} from "@/components/grid-company-selector";
import { translateQuality, qualityColor } from "@/lib/quality-labels";
import type { HAResponse, AreaInfo, SourceStatus, ForecastResponse } from "@/lib/api";

function fmt(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toFixed(2);
}

function priceColor(price: number | null | undefined): string {
  if (price == null) return "slate";
  if (price <= 0.5) return "emerald";
  if (price <= 1.5) return "blue";
  if (price <= 3.0) return "amber";
  return "red";
}

interface AreaData {
  info: AreaInfo;
  ha: HAResponse | null;
}

export default function DashboardContent({
  areaData,
  sources,
}: {
  areaData: AreaData[];
  sources: SourceStatus[];
}) {
  const { selected, select } = useSelectedArea();
  const { selection: gridSelection, update: updateGrid } = useGridSelection();
  const [forecast, setForecast] = useState<ForecastResponse | null>(null);
  const handleForecast = useCallback((f: ForecastResponse | null) => setForecast(f), []);

  const current = areaData.find((d) => d.info.code === selected) ?? areaData[0];

  return (
    <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-8 space-y-6">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-gray-900">Oversigt</h2>
          <p className="text-sm text-gray-500 mt-0.5">
            Elprisprognose for Danmark
          </p>
        </div>
        <div className="flex items-center gap-3">
          <GridCompanySelector
            selection={gridSelection}
            onSelect={updateGrid}
          />
          <AreaSwitcher selected={selected} onSelect={select} />
        </div>
      </div>

      {current && (
        <section className="space-y-4">
          {current.ha ? (
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
              <StatCard
                label="Nuværende pris"
                value={fmt(current.ha.attributes.current_price)}
                unit="DKK/kWh"
                color={priceColor(current.ha.attributes.current_price)}
              />
              <StatCard
                label="Næste kvarter"
                value={fmt(current.ha.attributes.next_slot_price)}
                unit="DKK/kWh"
                color={priceColor(current.ha.attributes.next_slot_price)}
              />
              <StatCard
                label="I dag min"
                value={fmt(current.ha.attributes.today_min)}
                unit="DKK/kWh"
                color="emerald"
              />
              <StatCard
                label="I dag maks"
                value={fmt(current.ha.attributes.today_max)}
                unit="DKK/kWh"
                color="red"
              />
              <StatCard
                label="I dag gns"
                value={fmt(current.ha.attributes.today_average)}
                unit="DKK/kWh"
                color="slate"
              />
              <StatCard
                label="Prognosekvalitet"
                value={translateQuality(current.ha.attributes.forecast_quality)}
                color={qualityColor(current.ha.attributes.forecast_quality)}
              />
            </div>
          ) : (
            <div className="card p-6 text-sm text-gray-400">
              Data ikke tilgængelig for {current.info.code}
            </div>
          )}
        </section>
      )}

      <PriceBreakdownChart
        area={selected}
        gln={gridSelection?.gln ?? null}
        code={gridSelection?.code ?? null}
      />

      <section className="space-y-4">
        <h2 className="text-lg font-semibold text-gray-900">
          7-dags prognose
        </h2>
        <AreaCharts area={selected} onForecast={handleForecast} />
      </section>

      <JobStatus sources={sources} forecast={forecast} />
    </div>
  );
}
