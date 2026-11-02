import type { JobSummary } from "./api";

export function hasNewlyFinishedJobs(
  previous: Record<string, JobSummary> | null,
  current: Record<string, JobSummary>,
): boolean {
  // The page may have rendered before a short bootstrap finished, even if
  // the first client poll already sees its completion.
  if (!previous) {
    const bootstrap = current.bootstrap;
    return bootstrap?.last_status === "completed" || bootstrap?.last_status === "failed";
  }

  return Object.values(current).some((job) => {
    if (job.last_status !== "completed" && job.last_status !== "failed") {
      return false;
    }

    const before = previous[job.job_type];
    return (
      !before ||
      before.last_status !== job.last_status ||
      before.last_started_at !== job.last_started_at ||
      before.last_finished_at !== job.last_finished_at
    );
  });
}
