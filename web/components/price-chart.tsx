"use client";

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

export default function PriceChart({
  data,
  productionHorizon,
}: {
  data: CombinedPoint[];
  productionHorizon?: string | null;
}) {
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

  const formatted = data.map((d) => ({
    ...d,
    label: new Date(d.ts).toLocaleString("da-DK", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }),
  }));

  const lastActualIdx = formatted.findLastIndex((d) => d.actual != null);
  const cutoffLabel = lastActualIdx >= 0 ? formatted[lastActualIdx].label : null;

  let prodHorizonLabel: string | null = null;
  if (productionHorizon) {
    const horizonMs = new Date(productionHorizon).getTime();
    let bestIdx = -1;
    let bestDiff = Infinity;
    for (let i = 0; i < formatted.length; i++) {
      const diff = Math.abs(new Date(formatted[i].ts).getTime() - horizonMs);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestIdx = i;
      }
    }
    if (bestIdx >= 0) prodHorizonLabel = formatted[bestIdx].label;
  }

  return (
    <div className="card p-6">
      <h2 className="text-lg font-semibold text-gray-900 mb-4">
        Prisprognose
      </h2>
      <div className="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={formatted}
            margin={{ top: 5, right: 20, bottom: 5, left: 10 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis
              dataKey="label"
              tick={{ fill: "#9ca3af", fontSize: 11 }}
              interval={Math.max(Math.floor(formatted.length / 12) - 1, 0)}
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
              contentStyle={{
                backgroundColor: "#ffffff",
                border: "1px solid #e5e7eb",
                borderRadius: "0.5rem",
                boxShadow: "0 4px 6px -1px rgb(0 0 0 / 0.05)",
                color: "#374151",
                fontSize: 13,
              }}
              formatter={(
                value: number | [number, number],
                name: string,
              ) => {
                if (Array.isArray(value))
                  return [
                    `${value[0].toFixed(4)} – ${value[1].toFixed(4)} DKK/kWh`,
                    name,
                  ];
                return [value.toFixed(4) + " DKK/kWh", name];
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 13, color: "#6b7280" }}
              iconType="circle"
              iconSize={8}
            />
            {cutoffLabel && (
              <ReferenceLine
                x={cutoffLabel}
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
            {prodHorizonLabel && prodHorizonLabel !== cutoffLabel && (
              <ReferenceLine
                x={prodHorizonLabel}
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
            <Area
              type="monotone"
              dataKey="band"
              name="P10–P90 interval"
              stroke="none"
              fill="#059669"
              fillOpacity={0.1}
            />
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
