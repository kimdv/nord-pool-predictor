"use client";

import { useState } from "react";
import {
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
} from "recharts";
import type { CombinedPoint } from "@/hooks/use-area-data";
import PriceHistorySummary from "./price-history-summary";
import type { PriceRange } from "@/lib/price-history";
import { formatCopenhagenDateTime } from "@/lib/copenhagen-time";

const AXIS_TIME_FORMAT: Intl.DateTimeFormatOptions = {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
};

export default function PriceChart({
  data,
  productionHorizon,
  historicalRange,
  period = "forecast",
}: {
  period?: "today" | "forecast";
  data: CombinedPoint[];
  historicalRange: PriceRange | null;
  productionHorizon?: string | null;
}) {
  const [showUncertainty, setShowUncertainty] = useState(true);
  const hasPredictions = data.some((point) => point.predicted != null);
  const title = period === "today" ? "Elprisen i dag" : "De næste 7 dage";
  const axisTimeFormat = period === "today" ? { hour: "2-digit", minute: "2-digit" } as const : AXIS_TIME_FORMAT;

  if (!data.some((point) => point.actual != null || point.predicted != null)) {
    return (
      <div className="forecast-chart">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">
          {title}
        </h2>
        <p className="text-sm text-gray-400">
          Ingen prisdata tilgængelig endnu. Data vises når indsamlingen er
          fuldført.
        </p>
      </div>
    );
  }

  const lastActualIdx = data.findLastIndex((d) => d.actual != null);
  const cutoffTimestamp = lastActualIdx >= 0 ? data[lastActualIdx].ts : null;
  const hasMissingPrices = data.some((point) => point.actual == null && point.predicted == null);
  const nowMs = Date.now();
  let nowTimestamp: string | null = null;
  let bestNowDiff = Infinity;
  for (const point of data) {
    const diff = Math.abs(new Date(point.ts).getTime() - nowMs);
    if (diff < bestNowDiff) {
      bestNowDiff = diff;
      nowTimestamp = point.ts;
    }
  }

  let prodHorizonTimestamp: string | null = null;
  if (productionHorizon) {
    const horizonMs = new Date(productionHorizon).getTime();
    let bestIdx = -1;
    let bestDiff = Infinity;
    for (let i = 0; i < data.length; i++) {
      const diff = Math.abs(new Date(data[i].ts).getTime() - horizonMs);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestIdx = i;
      }
    }
    if (bestIdx >= 0) prodHorizonTimestamp = data[bestIdx].ts;
  }

  return (
    <div className="forecast-chart">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-4">
        <div><h2 className="text-lg font-semibold text-gray-900">{title}</h2><p className="chart-description">{period === "today" ? "Dagens spotpriser, fra midnat til midnat" : "Kendte spotpriser og forventet prisudvikling"}</p></div>
        {(period === "forecast" || hasPredictions) && <label className="inline-flex items-center gap-2 cursor-pointer select-none">
          <span
            aria-hidden
            className={`h-2.5 w-2.5 rounded-full transition-colors ${
              showUncertainty ? "bg-emerald-600" : "bg-gray-300"
            }`}
          />
          <span className="text-xs font-medium text-gray-700">Usikkerhed</span>
          <button
            type="button"
            role="switch"
            aria-checked={showUncertainty}
            aria-label="Vis usikkerhedsinterval"
            onClick={() => setShowUncertainty((v) => !v)}
            className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-offset-2 ${
              showUncertainty ? "bg-emerald-600" : "bg-gray-300"
            }`}
          >
            <span
              aria-hidden
              className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                showUncertainty ? "translate-x-[18px]" : "translate-x-0.5"
              }`}
            />
          </button>
        </label>}
      </div>
      {period === "forecast" && <PriceHistorySummary range={historicalRange} />}
      {hasMissingPrices && (
        <p role="status" className="text-sm text-amber-700 mb-4">
          {period === "today" ? "Der mangler priser for dele af i dag." : "Prognosen dækker endnu ikke alle de næste 7 dage."} Manglende priser vises som huller.
        </p>
      )}
      <div className="forecast-plot">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={data}
            margin={{ top: 5, right: 20, bottom: 5, left: 10 }}
          >
            <CartesianGrid vertical={false} stroke="#dfe8ee" />
            <XAxis
              dataKey="ts"
              tickFormatter={(ts: string) => formatCopenhagenDateTime(ts, axisTimeFormat)}
              tick={{ fill: "#657c8a", fontSize: 11 }}
              interval="preserveStartEnd"
              minTickGap={40}
              axisLine={{ stroke: "#e5e7eb" }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: "#657c8a", fontSize: 11 }}
              tickFormatter={(v: number) => v.toFixed(2)}
              axisLine={false}
              tickLine={false}
              label={{
                value: "DKK/kWh",
                angle: -90,
                position: "insideLeft",
                fill: "#657c8a",
                fontSize: 12,
              }}
            />
            <Tooltip
              labelFormatter={(ts) => formatCopenhagenDateTime(String(ts), { ...AXIS_TIME_FORMAT, timeZoneName: "shortOffset" })}
              contentStyle={{
                backgroundColor: "#ffffff",
                border: "1px solid #e5e7eb",
                borderRadius: "0.5rem",
                boxShadow: "0 4px 6px -1px rgb(0 0 0 / 0.05)",
                color: "#374151",
                fontSize: 13,
              }}
              formatter={(value, name) => {
                if (Array.isArray(value))
                  return [
                    `${Number(value[0]).toFixed(4)} – ${Number(value[1]).toFixed(4)} DKK/kWh`,
                    name,
                  ];
                if (value == null) return ["—", name];
                return [Number(value).toFixed(4) + " DKK/kWh", name];
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 13, color: "#6b7280" }}
              iconType="circle"
              iconSize={8}
            />
            {historicalRange && (
              <ReferenceLine
                y={historicalRange.min}
                ifOverflow="extendDomain"
                stroke="#64748b"
                strokeDasharray="3 4"
                label={{
                  value: historicalRange.min === historicalRange.max ? "Laveste/højeste · 7 dage" : "Laveste · 7 dage",
                  position: "insideBottomRight",
                  fill: "#475569",
                  fontSize: 11,
                }}
              />
            )}
            {historicalRange && historicalRange.max !== historicalRange.min && (
              <ReferenceLine
                y={historicalRange.max}
                ifOverflow="extendDomain"
                stroke="#98601d"
                strokeDasharray="7 4"
                label={{
                  value: "Højeste · 7 dage",
                  position: "insideTopRight",
                  fill: "#98601d",
                  fontSize: 11,
                }}
              />
            )}
            {nowTimestamp && (period === "today" || nowTimestamp !== cutoffTimestamp) && (
              <ReferenceLine
                x={nowTimestamp}
                stroke="#d1d5db"
                strokeWidth={1}
                strokeDasharray="4 3"
                label={{
                  value: "Nu",
                  position: "insideTopRight",
                  fill: "#657c8a",
                  fontSize: 11,
                }}
              />
            )}
            {period === "forecast" && cutoffTimestamp && (
              <ReferenceLine
                x={cutoffTimestamp}
                stroke="#d1d5db"
                strokeWidth={1}
                strokeDasharray="4 3"
                label={{
                  value: "Kendte priser ▸",
                  position: "insideTopRight",
                  fill: "#657c8a",
                  fontSize: 11,
                }}
              />
            )}
            {prodHorizonTimestamp && prodHorizonTimestamp !== cutoffTimestamp && (
              <ReferenceLine
                x={prodHorizonTimestamp}
                stroke="#fcd34d"
                strokeWidth={1}
                strokeDasharray="4 3"
                label={{
                  value: "Energinet vind/sol ▸",
                  position: "insideTopRight",
                  fill: "#d97706",
                  fontSize: 11,
                }}
              />
            )}
            {showUncertainty && (period === "forecast" || hasPredictions) && (
              <Area
                isAnimationActive={false}
                type="monotone"
                dataKey="band"
                name="P10–P90 interval"
                stroke="none"
                fill="#277a70"
                fillOpacity={0.1}
              />
            )}
            <Line
              isAnimationActive={false}
              type="monotone"
              dataKey="actual"
              name="Spotpris"
              stroke="#174d70"
              strokeWidth={2.5}
              dot={false}
            />
            {(period === "forecast" || hasPredictions) && <Line
              isAnimationActive={false}
              type="monotone"
              dataKey="predicted"
              name="Prognose (P50)"
              stroke="#277a70"
              strokeWidth={2.5}
              strokeDasharray="6 3"
              dot={false}
            />}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
