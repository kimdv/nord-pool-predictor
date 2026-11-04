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
import RequestError from "./request-error";
import PriceHistorySummary from "./price-history-summary";
import { historicalRange, historyWindow } from "@/lib/price-history";
import { fetchJson } from "@/lib/client-api";
import { formatCopenhagenDateTime } from "@/lib/copenhagen-time";

import { buildPriceBreakdownSeries, type SlotBreakdown } from "@/lib/price-breakdown";

interface Props {
  area: AreaCode;
  gln: string | null;
  code: string | null;
  refreshVersion?: number;
}

const CATEGORIES = [
  { key: "spot_price", label: "Spotpris", color: "#174d70" },
  { key: "grid_tariff", label: "Nettarif", color: "#277a70" },
  { key: "transport", label: "Transport", color: "#98601d" },
  { key: "electricity_tax", label: "Elafgift", color: "#506f94" },
  { key: "vat", label: "Moms (25%)", color: "#5d7482" },
] as const;

type CategoryKey = (typeof CATEGORIES)[number]["key"];

const SLOT_FORMAT: Intl.DateTimeFormatOptions = {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
};
const SLOT_MS = 15 * 60 * 1000;

function formatSlot(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return formatCopenhagenDateTime(d, SLOT_FORMAT);
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

export default function PriceBreakdownChart({
  area,
  gln,
  code,
  refreshVersion = 0,
}: Props) {
  const [data, setData] = useState<SlotBreakdown[] | null>(null);
  const [history, setHistory] = useState<{ slots: SlotBreakdown[]; end: number } | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [retryVersion, setRetryVersion] = useState(0);
  const [disabled, setDisabled] = useState<Set<CategoryKey>>(new Set());
  const [prevKey, setPrevKey] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const historyEnd = historyWindow(new Date(nowMs)).end;
  const today = formatCopenhagenDateTime(new Date(), { year: "numeric", month: "2-digit", day: "2-digit" });
  const currentKey = gln && code ? `${area}:${gln}:${code}:${today}` : null;
  if (currentKey !== prevKey) {
    setPrevKey(currentKey);
    setData(null);
    setHistory(null);
    setHistoryLoading(currentKey !== null);
    setError(false);
    setLoading(currentKey !== null);
  }

  useEffect(() => {
    if (!gln || !code) return;

    const controller = new AbortController();
    setLoading(true);

    fetchJson<{ slots: SlotBreakdown[] }>(
      `/api/backend/tariffs/breakdown/${area}?gln=${encodeURIComponent(gln)}&code=${encodeURIComponent(code)}`,
      { signal: controller.signal },
    )
      .then((json) => {
        if (controller.signal.aborted) return;
        setData(json.slots);
        setError(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => {
      controller.abort();
    };
  }, [area, gln, code, refreshVersion, retryVersion, historyEnd, today]);

  useEffect(() => {
    if (!gln || !code) return;
    const controller = new AbortController();
    setHistory(null);
    setHistoryLoading(true);
    fetchJson<{ historical_slots: SlotBreakdown[] }>(
      `/api/backend/tariffs/breakdown/${area}?gln=${encodeURIComponent(gln)}&code=${encodeURIComponent(code)}&history_end=${encodeURIComponent(new Date(historyEnd).toISOString())}`,
      { signal: controller.signal },
    ).then((json) => {
      if (!controller.signal.aborted) setHistory({ slots: json.historical_slots, end: historyEnd });
    }).catch(() => {
      if (!controller.signal.aborted) setHistory(null);
    }).finally(() => {
      if (!controller.signal.aborted) setHistoryLoading(false);
    });
    return () => controller.abort();
  }, [area, gln, code, refreshVersion, retryVersion, historyEnd]);

  const retry = useCallback(() => setRetryVersion((version) => version + 1), []);

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

  const chartData = useMemo(
    () => buildPriceBreakdownSeries(data ?? [], disabled, activeSlotIndex),
    [activeSlotIndex, data, disabled],
  );
  const range = useMemo(() => history ? historicalRange(
    buildPriceBreakdownSeries(history.slots.filter((slot) => slot.spot_source === "actual"), disabled, -1)
      .map((point) => ({ ts: point.ts, value: point.total })), history.end,
  ) : null, [history, disabled]);
  const activeTimestamp = chartData[activeSlotIndex]?.ts ?? null;

  if (!gln || !code) {
    return (
      <div className="card tariff-empty p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">
          Elpriser i dag
        </h2>
        <p className="text-sm text-gray-400">
          Hvad koster strømmen på din regning? Vælg dit netselskab øverst for at se spotpris, nettarif, afgifter og moms.
        </p>
      </div>
    );
  }

  if (loading && !data) {
    return <ChartSkeleton height="h-72" />;
  }

  if (error && (!data || data.length === 0)) {
    return <RequestError message="Kunne ikke hente prissammensætningen." onRetry={retry} retrying={loading} />;
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
                aria-pressed={!isOff}
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

      <PriceHistorySummary range={range} loading={historyLoading} />
      {error && <RequestError message="Kunne ikke opdatere prissammensætningen. De senest hentede priser vises." onRetry={retry} retrying={loading} />}
      {data.some((slot) => slot.spot_source !== "actual") && (
        <p className="mb-3 text-xs text-amber-700">
          Manglende priser vises som huller. Prognosepriser er estimater.
        </p>
      )}
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chartData}
            margin={{ top: 5, right: 10, bottom: 5, left: 10 }}
          >
            <CartesianGrid vertical={false} stroke="#e4ecf0" />
            <XAxis
              dataKey="ts"
              tickFormatter={formatSlot}
              tick={{ fill: "#657c8a", fontSize: 11 }}
              axisLine={{ stroke: "#e5e7eb" }}
              tickLine={false}
              interval={3}
            />
            <YAxis
              tick={{ fill: "#657c8a", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              tickFormatter={(v: number) => v.toFixed(2)}
              label={{
                value: "DKK/kWh",
                angle: -90,
                position: "insideLeft",
                fill: "#657c8a",
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
                    <p className="font-medium text-gray-800 mb-1">
                      {formatCopenhagenDateTime(String(label), { ...SLOT_FORMAT, timeZoneName: "shortOffset" })}
                    </p>
                    {payload[0]?.payload?.spot_source === "forecast" && (
                      <p className="text-amber-700">Estimeret spotpris</p>
                    )}
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
            {range && (
              <ReferenceLine
                y={range.min}
                ifOverflow="extendDomain"
                stroke="#64748b"
                strokeDasharray="3 4"
                label={{
                  value: range.min === range.max ? "Laveste/højeste · 7 dage" : "Laveste · 7 dage",
                  position: "insideBottomRight",
                  fill: "#475569",
                  fontSize: 11,
                }}
              />
            )}
            {range && range.max !== range.min && (
              <ReferenceLine
                y={range.max}
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
            {activeTimestamp && (
              <ReferenceLine
                x={activeTimestamp}
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
                isAnimationActive={false}
                key={cat.key}
                dataKey={cat.key}
                name={cat.key}
                stackId="price"
                fill={cat.color}
                radius={
                  cat.key === "vat" ? [2, 2, 0, 0] : undefined
                }
              >
                {chartData.map((entry) => (
                  <Cell
                    key={`${cat.key}-${entry.ts}`}
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
