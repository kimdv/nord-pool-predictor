import assert from "node:assert/strict";
import test from "node:test";
import { hasNewlyFinishedJobs } from "../lib/job-updates.ts";

function job(overrides = {}) {
  return {
    job_type: "refresh_forecast",
    last_status: "completed",
    last_started_at: "2026-10-28T10:00:00Z",
    last_finished_at: "2026-10-28T10:01:00Z",
    batch_size: 2,
    failures_in_batch: 0,
    ...overrides,
  };
}

function snapshot(...jobs) {
  return Object.fromEntries(jobs.map((row) => [row.job_type, row]));
}

test("the initial snapshot does not request a redundant dashboard refresh", () => {
  assert.equal(hasNewlyFinishedJobs(null, snapshot(job())), false);
});

test("an unchanged completed job does not refresh on every poll", () => {
  assert.equal(hasNewlyFinishedJobs(snapshot(job()), snapshot(job())), false);
});

test("a running batch does not refresh when only some areas have finished", () => {
  const previous = job({ last_status: "running", last_finished_at: null });
  const current = job({ last_status: "running" });
  assert.equal(hasNewlyFinishedJobs(snapshot(previous), snapshot(current)), false);
});

for (const status of ["completed", "failed"]) {
  test(`a running job refreshes when it becomes ${status}`, () => {
    const previous = job({ last_status: "running", last_finished_at: null });
    const current = job({ last_status: status });
    assert.equal(hasNewlyFinishedJobs(snapshot(previous), snapshot(current)), true);
  });

  test(`a new ${status} run is detected even if it finished between polls`, () => {
    const previous = job({ last_status: status });
    const current = job({
      last_status: status,
      last_started_at: "2026-10-28T11:00:00Z",
      last_finished_at: "2026-10-28T11:01:00Z",
    });
    assert.equal(hasNewlyFinishedJobs(snapshot(previous), snapshot(current)), true);
  });
}

test("a newly observed completed job type refreshes the dashboard", () => {
  assert.equal(hasNewlyFinishedJobs({}, snapshot(job())), true);
});

test("a newly observed running job waits for completion", () => {
  assert.equal(hasNewlyFinishedJobs({}, snapshot(job({ last_status: "running" }))), false);
});

test("a completion refreshes even while another job is still running", () => {
  const other = job({ job_type: "retrain_model", last_status: "running" });
  const previous = snapshot(job({ last_status: "running" }), other);
  const current = snapshot(job(), other);
  assert.equal(hasNewlyFinishedJobs(previous, current), true);
});

test("a changed finish timestamp detects another completed area in the batch", () => {
  const previous = job();
  const current = job({ last_finished_at: "2026-10-28T10:02:00Z" });
  assert.equal(hasNewlyFinishedJobs(snapshot(previous), snapshot(current)), true);
});
