import type { ForecastResponse } from "@/lib/api";

const STATUS_COLORS: Record<string, string> = {
  completed: "bg-emerald-400",
  scored: "bg-emerald-400",
  running: "bg-amber-400 animate-pulse",
  pending: "bg-gray-400",
  failed: "bg-red-500",
};

const STATUS_LABELS: Record<string, string> = {
  completed: "Færdig",
  scored: "Scoret",
  running: "Kører",
  pending: "Afventer",
  failed: "Fejlet",
};

export default function ForecastInfoBar({ forecast }: { forecast: ForecastResponse }) {
  const dotColor = STATUS_COLORS[forecast.status] ?? "bg-gray-400";
  const statusLabel = STATUS_LABELS[forecast.status] ?? forecast.status;

  return (
    <div className="card px-5 py-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
      <div className="flex items-center gap-2">
        <span className="text-gray-400">Model</span>
        <span className="font-medium text-gray-700">{forecast.model_version}</span>
      </div>
      <div className="h-4 w-px bg-gray-200" />
      <div className="flex items-center gap-2">
        <span className="text-gray-400">Udgivet</span>
        <span className="font-medium text-gray-700">
          {new Date(forecast.issued_at).toLocaleString("da-DK")}
        </span>
      </div>
      <div className="h-4 w-px bg-gray-200" />
      <div className="flex items-center gap-2">
        <span className="text-gray-400">Status</span>
        <span className="inline-flex items-center gap-1.5 font-medium text-gray-700">
          <span className={`inline-block h-2 w-2 rounded-full ${dotColor}`} />
          {statusLabel}
        </span>
      </div>
    </div>
  );
}
