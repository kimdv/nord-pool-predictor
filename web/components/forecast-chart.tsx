"use client";

import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";

interface ForecastPoint {
  ts: string;
  predicted: number;
  lower: number;
  upper: number;
}

export default function ForecastChart({ data }: { data: ForecastPoint[] }) {
  if (!data.length) {
    return (
      <div className="card p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">
          Prognose med konfidensinterval
        </h2>
        <p className="text-sm text-gray-400">
          Ingen prognosedata endnu. Vises efter første modelkørsel.
        </p>
      </div>
    );
  }

  const formatted = data.map((d) => ({
    ...d,
    band: [d.lower, d.upper] as [number, number],
    label: new Date(d.ts).toLocaleString("da-DK", {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    }),
  }));

  return (
    <div className="card p-6">
      <h2 className="text-lg font-semibold text-gray-900 mb-4">
        Prognose med konfidensinterval
      </h2>
      <div className="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={formatted} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
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
              formatter={(value: number | [number, number], name: string) => {
                if (Array.isArray(value))
                  return [`${value[0].toFixed(4)} – ${value[1].toFixed(4)} DKK/kWh`, name];
                return [value.toFixed(4) + " DKK/kWh", name];
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 13, color: "#6b7280" }}
              iconType="circle"
              iconSize={8}
            />
            <Area
              type="monotone"
              dataKey="band"
              name="P10–P90 interval"
              stroke="none"
              fill="#2563eb"
              fillOpacity={0.08}
            />
            <Area
              type="monotone"
              dataKey="predicted"
              name="Prognose (P50)"
              stroke="#059669"
              strokeWidth={2}
              fill="none"
              dot={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
