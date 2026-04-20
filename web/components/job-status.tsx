"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { JobSummary, SourceStatus, ForecastResponse } from "@/lib/api";

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

const STATUS_LABELS: Record<string, string> = {
  completed: "Færdig",
  scored: "Scoret",
  running: "Kører",
  pending: "Afventer",
  failed: "Fejlet",
};

const STATUS_DOT_CLASS: Record<string, string> = {
  completed: "bg-emerald-500",
  scored: "bg-emerald-500",
  running: "bg-amber-400 animate-pulse",
  pending: "bg-gray-400",
  failed: "bg-red-500",
};

const TRIGGERED_POLL_MS = 5000;
const FEEDBACK_CLEAR_MS = 10000;

type FeedbackState = { kind: "idle" | "ok" | "error"; text: string };

const IDLE_FEEDBACK: FeedbackState = { kind: "idle", text: "" };

function RefreshButton({
  jobKey,
  busy,
  feedback,
  onTrigger,
}: {
  jobKey: string;
  busy: boolean;
  feedback: FeedbackState;
  onTrigger: (key: string) => void;
}) {
  const cls =
    busy
      ? "border-blue-200 bg-blue-50 text-blue-400 cursor-wait"
      : feedback.kind === "ok"
        ? "border-emerald-200 bg-emerald-50 text-emerald-600"
        : feedback.kind === "error"
          ? "border-red-200 bg-red-50 text-red-500"
          : "border-gray-200 text-gray-400 hover:border-blue-300 hover:text-blue-600";

  return (
    <button
      onClick={() => onTrigger(jobKey)}
      disabled={busy}
      className={`ml-auto inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium transition-all ${cls}`}
      title={`Hent ${jobKey}`}
    >
      {busy ? (
        <span className="inline-block h-3 w-3 rounded-full border-2 border-blue-300 border-t-transparent animate-spin" />
      ) : (
        <svg
          className="h-3 w-3"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
          />
        </svg>
      )}
      {feedback.text && <span>{feedback.text}</span>}
    </button>
  );
}

export default function JobStatus({
  sources: initialSources,
  forecast,
}: {
  sources: SourceStatus[];
  forecast?: ForecastResponse | null;
}) {
  const [sources, setSources] = useState(initialSources);
  const [triggering, setTriggering] = useState<Set<string>>(new Set());
  const [feedback, setFeedback] = useState<Record<string, FeedbackState>>({});
  const [summaries, setSummaries] = useState<Record<string, JobSummary>>({});

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const feedbackTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const fetchAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setSources(initialSources);
  }, [initialSources]);

  const refreshSources = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetch("/api/backend/health", { signal });
      if (!res.ok) return;
      const data = await res.json();
      if (data.sources) setSources(data.sources);
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        console.warn("Failed to refresh sources", err);
      }
    }
  }, []);

  const refreshSummaries = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetch("/api/backend/jobs/summary", { signal });
      if (!res.ok) return;
      const rows: JobSummary[] = await res.json();
      const map: Record<string, JobSummary> = {};
      for (const row of rows) map[row.job_type] = row;
      setSummaries(map);
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        console.warn("Failed to refresh job summaries", err);
      }
    }
  }, []);

  const refreshAll = useCallback(async () => {
    fetchAbortRef.current?.abort();
    const controller = new AbortController();
    fetchAbortRef.current = controller;
    await Promise.all([
      refreshSources(controller.signal),
      refreshSummaries(controller.signal),
    ]);
  }, [refreshSources, refreshSummaries]);

  useEffect(() => {
    refreshSummaries();
    return () => {
      fetchAbortRef.current?.abort();
      const timers = feedbackTimersRef.current;
      for (const t of Object.values(timers)) clearTimeout(t);
    };
  }, [refreshSummaries]);

  const anyRunning = useMemo(() => {
    if (triggering.size > 0) return true;
    return Object.values(summaries).some((s) => s.last_status === "running");
  }, [triggering, summaries]);

  useEffect(() => {
    if (!anyRunning) {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    if (pollRef.current) return;
    pollRef.current = setInterval(refreshAll, TRIGGERED_POLL_MS);
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [anyRunning, refreshAll]);

  const scheduleFeedbackClear = useCallback((key: string) => {
    const prev = feedbackTimersRef.current[key];
    if (prev) clearTimeout(prev);
    feedbackTimersRef.current[key] = setTimeout(() => {
      setFeedback((state) => ({ ...state, [key]: IDLE_FEEDBACK }));
      delete feedbackTimersRef.current[key];
    }, FEEDBACK_CLEAR_MS);
  }, []);

  const trigger = useCallback(
    async (key: string) => {
      setTriggering((prev) => new Set(prev).add(key));
      setFeedback((prev) => ({ ...prev, [key]: IDLE_FEEDBACK }));

      try {
        const res = await fetch(`/api/backend/jobs/trigger/${key}`, {
          method: "POST",
        });
        const data = await res.json().catch(() => ({}));
        setFeedback((prev) => ({
          ...prev,
          [key]: res.ok
            ? { kind: "ok", text: "Startet" }
            : { kind: "error", text: data.detail ?? "Fejl" },
        }));
      } catch (err) {
        console.warn(`Failed to trigger job ${key}`, err);
        setFeedback((prev) => ({
          ...prev,
          [key]: { kind: "error", text: "Fejl" },
        }));
      } finally {
        setTriggering((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
        await refreshAll();
        scheduleFeedbackClear(key);
      }
    },
    [refreshAll, scheduleFeedbackClear],
  );

  return (
    <div className="card p-6">
      <h2 className="text-lg font-semibold text-gray-900 mb-3">Datakilder</h2>
      <div className="space-y-1.5">
        {sources.map((s) => {
          const label = SOURCE_LABELS[s.source] ?? s.source.replace(/_/g, " ");
          const jobKey = SOURCE_JOBS[s.source];
          const summary = jobKey ? summaries[jobKey] : undefined;
          const busy = jobKey
            ? triggering.has(jobKey) || summary?.last_status === "running"
            : false;
          return (
            <div key={s.source} className="flex items-center gap-2.5 text-sm py-1">
              <span
                className={`inline-block h-2 w-2 rounded-full shrink-0 ${
                  s.is_stale ? "bg-red-500 animate-pulse" : "bg-emerald-500"
                }`}
              />
              <span className="font-medium text-gray-700">{label}</span>
              <span className="text-gray-400 text-xs">{formatDate(s.last_updated)}</span>
              {s.is_stale && (
                <span className="rounded-full bg-red-50 border border-red-200 px-2 py-0.5 text-xs font-medium text-red-600">
                  Forældet
                </span>
              )}
              {jobKey && (
                <RefreshButton
                  jobKey={jobKey}
                  busy={busy}
                  feedback={feedback[jobKey] ?? IDLE_FEEDBACK}
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
            <span className="font-medium text-gray-700 text-xs">
              {forecast.model_version}
            </span>
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
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  STATUS_DOT_CLASS[forecast.status] ?? "bg-gray-400"
                }`}
              />
              {STATUS_LABELS[forecast.status] ?? forecast.status}
            </span>
          </div>
        </div>
      )}

      <div className="mt-3 pt-3 border-t border-gray-100 space-y-1.5">
        {EXTRA_JOBS.map((job) => {
          const summary = summaries[job.key];
          const triggered = triggering.has(job.key);
          const status = summary?.last_status;
          const isRunning = triggered || status === "running";
          const isFailed = !isRunning && status === "failed";
          const isCompleted = !isRunning && status === "completed";
          const lastTs =
            summary?.last_finished_at ?? summary?.last_started_at ?? null;

          const dotClass = isRunning
            ? "bg-amber-400 animate-pulse"
            : isFailed
              ? "bg-red-500"
              : isCompleted
                ? "bg-emerald-500"
                : "bg-gray-300";

          return (
            <div
              key={job.key}
              className="flex items-center gap-2.5 text-sm py-1"
            >
              <span
                className={`inline-block h-2 w-2 rounded-full shrink-0 ${dotClass}`}
              />
              <span
                className="font-medium text-gray-700"
                title={job.description}
              >
                {job.label}
              </span>
              <span className="text-gray-400 text-xs">
                {summary ? formatDate(lastTs) : "Ingen kørsler"}
              </span>
              {isRunning && (
                <span className="rounded-full bg-amber-50 border border-amber-200 px-2 py-0.5 text-xs font-medium text-amber-600">
                  {STATUS_LABELS.running}
                </span>
              )}
              {isFailed && summary && summary.batch_size > 1 ? (
                <span className="rounded-full bg-red-50 border border-red-200 px-2 py-0.5 text-xs font-medium text-red-600">
                  {`${STATUS_LABELS.failed} (${summary.failures_in_batch}/${summary.batch_size})`}
                </span>
              ) : isFailed ? (
                <span className="rounded-full bg-red-50 border border-red-200 px-2 py-0.5 text-xs font-medium text-red-600">
                  {STATUS_LABELS.failed}
                </span>
              ) : null}
              <RefreshButton
                jobKey={job.key}
                busy={isRunning}
                feedback={feedback[job.key] ?? IDLE_FEEDBACK}
                onTrigger={trigger}
              />
            </div>
          );
        })}
      </div>

      <p className="mt-4 pt-3 border-t border-gray-100 text-xs text-gray-400">
        Spotpriser, vind/sol-prognoser og tariffer fra{" "}
        <a
          href="https://www.energidataservice.dk"
          target="_blank"
          rel="noopener noreferrer"
          className="underline hover:text-gray-600"
        >
          Energi Data Service
        </a>{" "}
        (Energinet). Vejrdata fra{" "}
        <a
          href="https://open-meteo.com"
          target="_blank"
          rel="noopener noreferrer"
          className="underline hover:text-gray-600"
        >
          Open-Meteo
        </a>
        .
      </p>
    </div>
  );
}
