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
import { formatCopenhagenDateTime } from "@/lib/copenhagen-time";

const AXIS_TIME_FORMAT: Intl.DateTimeFormatOptions = {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
};

export default function PriceChart({
  data,
  productionHorizon,
}: {
  data: CombinedPoint[];
  productionHorizon?: string | null;
}) {
  const [showUncertainty, setShowUncertainty] = useState(false);

  if (!data.length) {
    return (
      <div className="card p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">
          Prisprognose
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
    <div className="card p-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Prisprognose</h2>
        <label className="inline-flex items-center gap-2 cursor-pointer select-none">
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
        </label>
      </div>
      <div className="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={data}
            margin={{ top: 5, right: 20, bottom: 5, left: 10 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis
              dataKey="ts"
              tickFormatter={(ts: string) => formatCopenhagenDateTime(ts, AXIS_TIME_FORMAT)}
              tick={{ fill: "#9ca3af", fontSize: 11 }}
              interval={Math.max(Math.floor(data.length / 12) - 1, 0)}
              axisLine={{ stroke: "#e5e7eb" }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: "#9ca3af", fontSize: 11 }}
              tickFormatter={(v: number) => v.toFixed(2)}
              axisLine={false}
              tickLine={false}
              label={{
                value: "DKK/kWh",
                angle: -90,
                position: "insideLeft",
                fill: "#9ca3af",
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
            {nowTimestamp && nowTimestamp !== cutoffTimestamp && (
              <ReferenceLine
                x={nowTimestamp}
                stroke="#d1d5db"
                strokeWidth={1}
                strokeDasharray="4 3"
                label={{
                  value: "Nu",
                  position: "insideTopRight",
                  fill: "#9ca3af",
                  fontSize: 11,
                }}
              />
            )}
            {cutoffTimestamp && (
              <ReferenceLine
                x={cutoffTimestamp}
                stroke="#d1d5db"
                strokeWidth={1}
                strokeDasharray="4 3"
                label={{
                  value: "Kendte priser ▸",
                  position: "insideTopRight",
                  fill: "#9ca3af",
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
            {showUncertainty && (
              <Area
                type="monotone"
                dataKey="band"
                name="P10–P90 interval"
                stroke="none"
                fill="#059669"
                fillOpacity={0.1}
              />
            )}
            <Line
              type="monotone"
              dataKey="actual"
              name="Spotpris"
              stroke="#2563eb"
              strokeWidth={2}
              dot={false}
            />
            <Line
              type="monotone"
              dataKey="predicted"
              name="Prognose (P50)"
              stroke="#7c3aed"
              strokeWidth={2}
              strokeDasharray="6 3"
              dot={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
