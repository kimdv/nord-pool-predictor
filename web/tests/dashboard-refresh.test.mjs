import assert from "node:assert/strict";
import test from "node:test";
import { createDashboardRefresh } from "../lib/dashboard-refresh.ts";

function setup(t, now = "2026-11-04T10:14:59.999Z") {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: Date.parse(now) });
  const document = new EventTarget();
  document.visibilityState = "visible";
  globalThis.document = document;
  const refresh = t.mock.fn();
  const controller = createDashboardRefresh(refresh);
  t.after(() => {
    controller.dispose();
    delete globalThis.document;
  });
  return { document, refresh, controller };
}

test("refreshes at the next quarter-hour and reschedules from the wall clock", (t) => {
  const { refresh, controller } = setup(t);
  assert.equal(refresh.mock.callCount(), 0);
  t.mock.timers.tick(1);
  assert.equal(refresh.mock.callCount(), 1);
  controller.complete();
  t.mock.timers.tick(15 * 60_000);
  assert.equal(refresh.mock.callCount(), 2);
});

for (const [label, before] of [
  ["winter midnight", "2026-01-15T22:59:59.999Z"],
  ["summer midnight", "2026-07-15T21:59:59.999Z"],
  ["midnight after the spring clock change", "2026-03-29T21:59:59.999Z"],
  ["midnight after the autumn clock change", "2026-10-25T22:59:59.999Z"],
  ["spring skipped hour", "2026-03-29T00:59:59.999Z"],
  ["autumn repeated hour", "2026-10-25T00:59:59.999Z"],
]) {
  test(`refreshes at Copenhagen ${label}`, (t) => {
    const { refresh } = setup(t, before);
    t.mock.timers.tick(1);
    assert.equal(refresh.mock.callCount(), 1);
  });
}

test("a suspended background tab refreshes immediately on becoming visible", (t) => {
  const { document, refresh } = setup(t);
  document.visibilityState = "hidden";
  document.dispatchEvent(new Event("visibilitychange"));
  t.mock.timers.tick(1);
  assert.equal(refresh.mock.callCount(), 0);
  t.mock.timers.setTime(Date.parse("2026-11-05T08:27:00Z"));
  document.visibilityState = "visible";
  document.dispatchEvent(new Event("visibilitychange"));
  t.mock.timers.tick(0);
  assert.equal(refresh.mock.callCount(), 1);
});

test("coalesces boundary, visibility and job signals and waits for the active refresh", (t) => {
  const { document, refresh, controller } = setup(t);
  controller.request();
  document.dispatchEvent(new Event("visibilitychange"));
  t.mock.timers.tick(1);
  assert.equal(refresh.mock.callCount(), 1);
  controller.request();
  controller.request();
  t.mock.timers.tick(0);
  assert.equal(refresh.mock.callCount(), 1);
  controller.complete();
  t.mock.timers.tick(0);
  assert.equal(refresh.mock.callCount(), 2);
  controller.complete();
  t.mock.timers.tick(0);
  assert.equal(refresh.mock.callCount(), 2);
});

test("a completed failed refresh does not block a retry", (t) => {
  const { refresh, controller } = setup(t);
  controller.request();
  t.mock.timers.tick(0);
  controller.complete();
  controller.request();
  t.mock.timers.tick(0);
  assert.equal(refresh.mock.callCount(), 2);
});

test("unmount cancels queued refreshes, boundary timers and visibility listeners", (t) => {
  const { document, refresh, controller } = setup(t);
  controller.request();
  controller.dispose();
  t.mock.timers.tick(24 * 3600_000);
  document.dispatchEvent(new Event("visibilitychange"));
  controller.request();
  controller.complete();
  t.mock.timers.tick(0);
  assert.equal(refresh.mock.callCount(), 0);
});
