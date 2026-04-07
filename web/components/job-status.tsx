"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { SourceStatus, ForecastResponse } from "@/lib/api";

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "Ingen data";
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return "Ukendt";
  return d.toLocaleString("da-DK", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const SOURCE_LABELS: Record<string, string> = {
  prices: "Elpriser",
  weather: "Vejrdata",
  production: "Vind & sol",
  crossborder: "Udlandsforbindelser",
};

const SOURCE_JOBS: Record<string, string> = {
  prices: "ingest_prices",
  weather: "ingest_weather",
  production: "ingest_production",
  crossborder: "ingest_crossborder",
};

const EXTRA_JOBS: { key: string; label: string; description: string }[] = [
  {
    key: "refresh_forecast",
    label: "Kør prognose",
    description: "Generér en ny prisprognose for de næste 7 dage baseret på den trænede model",
  },
  {
    key: "score_forecasts",
    label: "Evaluer prognoser",
    description: "Sammenlign tidligere prognoser med de faktiske priser og beregn nøjagtighed",
  },
  {
    key: "retrain_model",
    label: "Gentræn model",
    description: "Træn ML-modellen forfra med de nyeste data. Tager typisk 5-15 minutter",
  },
];

function RefreshButton({
  jobKey,
  running,
  feedback,
  onTrigger,
}: {
  jobKey: string;
  running: boolean;
  feedback: string;
  onTrigger: (key: string) => void;
}) {
  return (
    <button
      onClick={() => onTrigger(jobKey)}
      disabled={running}
      className={`ml-auto inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium transition-all ${
        running
          ? "border-blue-200 bg-blue-50 text-blue-400 cursor-wait"
          : feedback === "Startet"
            ? "border-emerald-200 bg-emerald-50 text-emerald-600"
            : feedback
              ? "border-red-200 bg-red-50 text-red-500"
              : "border-gray-200 text-gray-400 hover:border-blue-300 hover:text-blue-600"
      }`}
      title={`Hent ${jobKey}`}
    >
      {running ? (
        <span className="inline-block h-3 w-3 rounded-full border-2 border-blue-300 border-t-transparent animate-spin" />
      ) : (
        <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      )}
      {feedback && <span>{feedback}</span>}
    </button>
  );
}

const FORECAST_STATUS_LABELS: Record<string, string> = {
  completed: "Færdig",
  scored: "Scoret",
  running: "Kører",
  pending: "Afventer",
  failed: "Fejlet",
};

const FORECAST_STATUS_COLORS: Record<string, string> = {
  completed: "bg-emerald-500",
  scored: "bg-emerald-500",
  running: "bg-amber-400 animate-pulse",
  pending: "bg-gray-400",
  failed: "bg-red-500",
};

export default function JobStatus({
  sources: initialSources,
  forecast,
}: {
  sources: SourceStatus[];
  forecast?: ForecastResponse | null;
}) {
  const [sources, setSources] = useState(initialSources);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [feedback, setFeedback] = useState<Record<string, string>>({});
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    setSources(initialSources);
  }, [initialSources]);

  const refreshSources = useCallback(async () => {
    try {
      const res = await fetch("/api/backend/health");
      if (res.ok) {
        const data = await res.json();
        if (data.sources) setSources(data.sources);
      }
    } catch { /* ignore */ }
  }, []);

  const startPolling = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = setInterval(refreshSources, 5000);
  }, [refreshSources]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const trigger = useCallback(async (key: string) => {
    setRunning((prev) => new Set(prev).add(key));
    setFeedback((prev) => ({ ...prev, [key]: "" }));

    try {
      const res = await fetch(`/api/backend/jobs/trigger/${key}`, {
        method: "POST",
      });
      const data = await res.json();
      setFeedback((prev) => ({
        ...prev,
        [key]: res.ok ? "Startet" : (data.detail ?? "Fejl"),
      }));
      if (res.ok) startPolling();
    } catch {
      setFeedback((prev) => ({ ...prev, [key]: "Fejl" }));
    }

    setTimeout(() => {
      setRunning((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
      setFeedback((prev) => ({ ...prev, [key]: "" }));
      refreshSources();
      stopPolling();
    }, 10000);
  }, [startPolling, stopPolling, refreshSources]);

  return (
    <div className="card p-6">
      <h2 className="text-lg font-semibold text-gray-900 mb-3">Datakilder</h2>
      <div className="space-y-1.5">
        {sources.map((s) => {
          const label = SOURCE_LABELS[s.source] ?? s.source.replace(/_/g, " ");
          const jobKey = SOURCE_JOBS[s.source];
          return (
            <div key={s.source} className="flex items-center gap-2.5 text-sm py-1">
              <span
                className={`inline-block h-2 w-2 rounded-full shrink-0 ${
                  s.is_stale ? "bg-red-500 animate-pulse" : "bg-emerald-500"
                }`}
              />
              <span className="font-medium text-gray-700">{label}</span>
              <span className="text-gray-400 text-xs">
                {formatDate(s.last_updated)}
              </span>
              {s.is_stale && (
                <span className="rounded-full bg-red-50 border border-red-200 px-2 py-0.5 text-xs font-medium text-red-600">
                  Forældet
                </span>
              )}
              {jobKey && (
                <RefreshButton
                  jobKey={jobKey}
                  running={running.has(jobKey)}
                  feedback={feedback[jobKey] ?? ""}
                  onTrigger={trigger}
                />
              )}
            </div>
          );
        })}

      </div>

      {forecast && (
        <div className="mt-3 pt-3 border-t border-gray-100 flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
          <div className="flex items-center gap-1.5">
            <span className="text-gray-400">Model</span>
            <span className="font-medium text-gray-700 text-xs">{forecast.model_version}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-gray-400">Udgivet</span>
            <span className="font-medium text-gray-700 text-xs">
              {new Date(forecast.issued_at).toLocaleString("da-DK")}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-gray-400">Status</span>
            <span className="inline-flex items-center gap-1 font-medium text-gray-700 text-xs">
              <span className={`inline-block h-2 w-2 rounded-full ${FORECAST_STATUS_COLORS[forecast.status] ?? "bg-gray-400"}`} />
              {FORECAST_STATUS_LABELS[forecast.status] ?? forecast.status}
            </span>
          </div>
        </div>
      )}

      <div className="mt-4 pt-3 border-t border-gray-100 flex flex-wrap gap-2">
        {EXTRA_JOBS.map((job) => {
          const isRunning = running.has(job.key);
          const msg = feedback[job.key];
          return (
            <button
              key={job.key}
              onClick={() => trigger(job.key)}
              disabled={isRunning}
              title={job.description}
              className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-all ${
                isRunning
                  ? "border-blue-200 bg-blue-50 text-blue-500 cursor-wait"
                  : "border-gray-200 bg-white text-gray-600 hover:border-blue-300 hover:text-blue-600"
              }`}
            >
              {isRunning && (
                <span className="inline-block h-3 w-3 rounded-full border-2 border-blue-300 border-t-transparent animate-spin" />
              )}
              {job.label}
              <svg className="h-3.5 w-3.5 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              {msg && (
                <span className={`text-xs ${msg === "Startet" ? "text-emerald-500" : "text-red-500"}`}>
                  · {msg}
                </span>
              )}
            </button>
          );
        })}
      </div>

      <p className="mt-4 pt-3 border-t border-gray-100 text-xs text-gray-400">
        Spotpriser, vind/sol-prognoser og tariffer fra{" "}
        <a href="https://www.energidataservice.dk" target="_blank" rel="noopener noreferrer" className="underline hover:text-gray-600">
          Energi Data Service
        </a>{" "}
        (Energinet). Vejrdata fra{" "}
        <a href="https://open-meteo.com" target="_blank" rel="noopener noreferrer" className="underline hover:text-gray-600">
          Open-Meteo
        </a>.
      </p>
    </div>
  );
}
