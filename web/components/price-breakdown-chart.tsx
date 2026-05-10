"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BarChart,
  Bar,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
} from "recharts";
import type { AreaCode } from "@/components/area-switcher";
import ChartSkeleton from "./chart-skeleton";

interface SlotBreakdown {
  hour: number;
  minute: number;
  ts: string;
  spot_price: number;
  grid_tariff: number;
  system_tariff: number;
  transmission_tariff: number;
  grid_loss_tariff: number;
  electricity_tax: number;
  total_ex_vat: number;
  vat: number;
  total_incl_vat: number;
}

interface Props {
  area: AreaCode;
  gln: string | null;
  code: string | null;
}

const CATEGORIES = [
  { key: "spot_price", label: "Spotpris", color: "#2563eb" },
  { key: "grid_tariff", label: "Nettarif", color: "#059669" },
  { key: "transport", label: "Transport", color: "#f59e0b" },
  { key: "electricity_tax", label: "Elafgift", color: "#7c3aed" },
  { key: "vat", label: "Moms (25%)", color: "#9ca3af" },
] as const;

type CategoryKey = (typeof CATEGORIES)[number]["key"];

const SLOT_FORMATTER = new Intl.DateTimeFormat("da-DK", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: "Europe/Copenhagen",
});
const SLOT_MS = 15 * 60 * 1000;

function formatSlot(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return SLOT_FORMATTER.format(d);
}

function getActiveSlotIndex(slots: SlotBreakdown[], nowMs: number): number {
  for (let i = 0; i < slots.length; i++) {
    const startMs = new Date(slots[i].ts).getTime();
    if (Number.isNaN(startMs)) continue;

    const nextMs =
      i + 1 < slots.length ? new Date(slots[i + 1].ts).getTime() : NaN;
    const endMs =
      !Number.isNaN(nextMs) && nextMs > startMs ? nextMs : startMs + SLOT_MS;

    if (nowMs >= startMs && nowMs < endMs) return i;
  }

  return -1;
}

export default function PriceBreakdownChart({ area, gln, code }: Props) {
  const [data, setData] = useState<SlotBreakdown[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [disabled, setDisabled] = useState<Set<CategoryKey>>(new Set());
  const [prevKey, setPrevKey] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const currentKey = gln && code ? `${area}:${gln}:${code}` : null;
  if (currentKey !== prevKey) {
    setPrevKey(currentKey);
    setData(null);
    setLoading(currentKey !== null);
  }

  useEffect(() => {
    if (!gln || !code) return;

    let cancelled = false;

    fetch(
      `/api/backend/tariffs/breakdown/${area}?gln=${encodeURIComponent(gln)}&code=${encodeURIComponent(code)}`,
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((json) => {
        if (cancelled) return;
        setData(json?.slots ?? null);
        setLoading(false);
      })
      .catch(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [area, gln, code]);

  useEffect(() => {
    const intervalId = window.setInterval(() => setNowMs(Date.now()), 30_000);
    return () => window.clearInterval(intervalId);
  }, []);

  const toggleCategory = useCallback((key: CategoryKey) => {
    setDisabled((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const activeSlotIndex = useMemo(() => {
    if (!data) return -1;
    return getActiveSlotIndex(data, nowMs);
  }, [data, nowMs]);

  const chartData = useMemo(() => {
    if (!data) return [];
    return data.map((s, index) => {
      const spot = disabled.has("spot_price") ? 0 : s.spot_price;
      const grid = disabled.has("grid_tariff") ? 0 : s.grid_tariff;
      const transport = disabled.has("transport")
        ? 0
        : s.system_tariff + s.transmission_tariff + s.grid_loss_tariff;
      const tax = disabled.has("electricity_tax") ? 0 : s.electricity_tax;
      const subtotal = spot + grid + transport + tax;
      const vat = disabled.has("vat") ? 0 : subtotal * 0.25;

      return {
        slot: formatSlot(s.ts),
        isCurrent: index === activeSlotIndex,
        spot_price: spot,
        grid_tariff: grid,
        transport,
        electricity_tax: tax,
        vat,
        total: subtotal + vat,
      };
    });
  }, [activeSlotIndex, data, disabled]);
  const activeSlot = chartData[activeSlotIndex]?.slot ?? null;

  if (!gln || !code) {
    return (
      <div className="card p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">
          Elpriser i dag
        </h2>
        <p className="text-sm text-gray-400">
          Vælg dit netselskab for at se den fulde prissammensætning.
        </p>
      </div>
    );
  }

  if (loading) {
    return <ChartSkeleton height="h-72" />;
  }

  if (!data || data.length === 0) {
    return (
      <div className="card p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">
          Elpriser i dag
        </h2>
        <p className="text-sm text-gray-400">Ingen prisdata tilgængelig.</p>
      </div>
    );
  }

  return (
    <div className="card p-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Elpriser i dag</h2>
        <div className="flex flex-wrap gap-2">
          {CATEGORIES.map((cat) => {
            const isOff = disabled.has(cat.key);
            return (
              <button
                key={cat.key}
                onClick={() => toggleCategory(cat.key)}
                className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-all ${
                  isOff
                    ? "border-gray-200 text-gray-400 bg-white"
                    : "border-transparent text-white"
                }`}
                style={isOff ? undefined : { backgroundColor: cat.color }}
              >
                {cat.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chartData}
            margin={{ top: 5, right: 10, bottom: 5, left: 10 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis
              dataKey="slot"
              tick={{ fill: "#9ca3af", fontSize: 11 }}
              axisLine={{ stroke: "#e5e7eb" }}
              tickLine={false}
              interval={3}
            />
            <YAxis
              tick={{ fill: "#9ca3af", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              tickFormatter={(v: number) => v.toFixed(2)}
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
                fontSize: 13,
              }}
              content={({ active, payload, label }) => {
                if (!active || !payload?.length) return null;
                const total = payload.reduce(
                  (sum, p) => sum + ((p.value as number) ?? 0),
                  0,
                );
                return (
                  <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-md text-[13px]">
                    <p className="font-medium text-gray-800 mb-1">{label}</p>
                    {payload.map((p) => {
                      const key = String(p.dataKey ?? "");
                      return (
                        <p key={key} style={{ color: p.color }}>
                          {CATEGORIES.find((c) => c.key === key)?.label ?? key}
                          {" : "}
                          {(Number(p.value) || 0).toFixed(4)} DKK/kWh
                        </p>
                      );
                    })}
                    <p className="mt-1 pt-1 border-t border-gray-100 font-semibold text-gray-900">
                      I alt : {total.toFixed(4)} DKK/kWh
                    </p>
                  </div>
                );
              }}
            />
            <Legend content={() => null} />
            {activeSlot && (
              <ReferenceLine
                x={activeSlot}
                stroke="#111827"
                strokeWidth={1.5}
                label={{
                  value: "Nu",
                  position: "insideTop",
                  fill: "#111827",
                  fontSize: 11,
                  fontWeight: 600,
                }}
              />
            )}
            {CATEGORIES.map((cat) => (
              <Bar
                key={cat.key}
                dataKey={cat.key}
                name={cat.key}
                stackId="price"
                fill={cat.color}
                radius={
                  cat.key === "vat" ? [2, 2, 0, 0] : undefined
                }
              >
                {chartData.map((entry, index) => (
                  <Cell
                    key={`${cat.key}-${entry.slot}-${index}`}
                    fill={cat.color}
                    fillOpacity={entry.isCurrent ? 1 : 0.82}
                    stroke={entry.isCurrent ? "#111827" : "transparent"}
                    strokeWidth={entry.isCurrent ? 1.5 : 0}
                  />
                ))}
              </Bar>
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
