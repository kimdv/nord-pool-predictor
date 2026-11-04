"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import AreaSwitcher, { useSelectedArea } from "@/components/area-switcher";
import StatCard from "@/components/stat-card";
import JobStatus from "@/components/job-status";
import AreaCharts from "@/components/area-charts";
import PriceBreakdownChart from "@/components/price-breakdown-chart";
import GridCompanySelector, {
  useGridSelection,
} from "@/components/grid-company-selector";
import { translateQuality } from "@/lib/quality-labels";
import type { HAResponse, AreaInfo, SourceStatus, ForecastResponse } from "@/lib/api";

function fmt(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toFixed(2);
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
  const router = useRouter();
  const { selected, select } = useSelectedArea();
  const { selection: gridSelection, update: updateGrid } = useGridSelection();
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [forecast, setForecast] = useState<ForecastResponse | null>(null);
  const handleForecast = useCallback((f: ForecastResponse | null) => setForecast(f), []);
  const refreshDashboard = useCallback(() => {
    setRefreshVersion((version) => version + 1);
    router.refresh();
  }, [router]);

  const current = areaData.find((d) => d.info.code === selected) ?? areaData[0];

  const attributes = current?.ha?.attributes;
  const overview = (
    <aside className="today-summary" aria-label="Dagens spotpriser">
      <div className="current-price">
        <div className="summary-heading"><span className="live-dot" aria-hidden="true" />{attributes?.current_price_source === "forecast" ? "Estimeret spotpris nu" : "Spotpris lige nu"}</div>
        <p className="current-price-value">{fmt(attributes?.current_price)}<span>DKK/kWh</span></p>
        <p className="price-context">{selected === "DK1" ? "Vestdanmark" : "Østdanmark"} / {selected}</p>
      </div>
      <dl className="today-stats">
        <StatCard label="Næste kvarter" value={fmt(attributes?.next_slot_price)} unit="DKK/kWh" />
        <div className="daily-range">
          <StatCard label="Laveste i dag" value={fmt(attributes?.today_min)} color="emerald" />
          <StatCard label="Højeste i dag" value={fmt(attributes?.today_max)} color="amber" />
        </div>
        <StatCard label="Gennemsnit i dag" value={fmt(attributes?.today_average)} unit="DKK/kWh" />
      </dl>
      <div className="summary-footnote">Spotpriser uden tariffer, afgifter og moms. Dagens minimum og maksimum er i DKK/kWh.</div>
      {attributes && <div className="quality-note">Prognosekvalitet <span>{translateQuality(attributes.forecast_quality)}</span></div>}
      {!attributes && <p className="summary-footnote" role="status">Prisdata er endnu ikke tilgængelige for {selected}.</p>}
    </aside>
  );

  return (
    <div className="dashboard-shell">
      <section className="dashboard-intro" aria-labelledby="dashboard-title">
        <div>
          <h1 id="dashboard-title">Elprisen, syv dage frem.</h1>
          <p>Følg prisprognosen, undersøg usikkerheden og mål modellens præcision.</p>
        </div>
        <div className="dashboard-settings">
          <div className="setting-field"><span>Prisområde</span><AreaSwitcher selected={selected} onSelect={select} /></div>
          <div className="setting-field"><span>Dit netselskab</span><GridCompanySelector selection={gridSelection} onSelect={updateGrid} /></div>
        </div>
      </section>

      <section id="prisoverblik" aria-label="Prisoverblik">
        <AreaCharts area={selected} refreshVersion={refreshVersion} onForecast={handleForecast} overview={overview}>
          <PriceBreakdownChart area={selected} gln={gridSelection?.gln ?? null} code={gridSelection?.code ?? null} refreshVersion={refreshVersion} />
        </AreaCharts>
      </section>

      <section id="datakilder" className="data-section" aria-label="Datakilder og opdateringer">
        <JobStatus sources={sources} forecast={forecast} onJobsFinished={refreshDashboard} />
      </section>
    </div>
  );
}
