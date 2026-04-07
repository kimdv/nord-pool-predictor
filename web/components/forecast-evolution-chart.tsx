"use client";

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface Snapshot {
  run_id: string;
  issued_at: string;
  steps: number;
}

export default function ForecastEvolutionChart({
  snapshots,
}: {
  snapshots: Snapshot[];
}) {
  if (!snapshots.length) {
    return (
      <div className="card p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          Prognose-snapshots
        </h2>
        <p className="text-sm text-gray-400">Ingen snapshots tilgængelige endnu.</p>
      </div>
    );
  }

  const data = snapshots.map((s) => ({
    issued: new Date(s.issued_at).toLocaleDateString("da-DK", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }),
    steps: s.steps,
  }));

  return (
    <div className="card p-6">
      <h2 className="text-lg font-semibold text-gray-900 mb-4">
        Prognose-snapshots
      </h2>
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
          <XAxis
            dataKey="issued"
            stroke="#9ca3af"
            fontSize={12}
            axisLine={{ stroke: "#e5e7eb" }}
            tickLine={false}
          />
          <YAxis
            stroke="#9ca3af"
            fontSize={12}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#ffffff",
              border: "1px solid #e5e7eb",
              borderRadius: "0.5rem",
              boxShadow: "0 4px 6px -1px rgb(0 0 0 / 0.05)",
            }}
            labelStyle={{ color: "#374151" }}
          />
          <Legend iconType="circle" iconSize={8} />
          <Line
            type="monotone"
            dataKey="steps"
            name="Horisont (trin)"
            stroke="#7c3aed"
            strokeWidth={2}
            dot={{ r: 3, fill: "#7c3aed" }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
