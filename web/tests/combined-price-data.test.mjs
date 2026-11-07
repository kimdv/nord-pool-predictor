import assert from "node:assert/strict";
import test from "node:test";
import { buildCombinedData, priceChartWindow } from "../lib/combined-price-data.ts";

const now = new Date("2026-11-03T18:08:37Z");
const prices = (entries) => ({
  area: "DK1",
  prices: entries.map(([ts, price_dkk_kwh]) => ({ ts, price_dkk_kwh })),
});
const forecast = (timestamps) => ({
  values: timestamps.map((ts) => ({
    ts, predicted_price_dkk_kwh: 0.5, lower_dkk_kwh: 0.3, upper_dkk_kwh: 0.7,
  })),
});
const pricedPoints = (data) => data.filter((p) => p.actual != null || p.predicted != null);

test("the window covers the current quarter-hour through seven days ahead", () => {
  const data = buildCombinedData(null, null, now);
  assert.equal(data.length, 673);
  assert.equal(data[0].ts, "2026-11-03T18:00:00.000Z");
  assert.equal(data.at(-1).ts, "2026-11-10T18:00:00.000Z");
  assert.deepEqual(pricedPoints(data), []);
});

test("stale forecasts and prices outside the seven-day window are excluded", () => {
  const data = buildCombinedData(
    prices([["2026-11-03T17:45:00Z", 1.5]]),
    forecast(["2026-10-24T18:00:00Z", "2026-11-10T18:15:00Z"]), now,
  );
  assert.deepEqual(pricedPoints(data), []);
});

test("actual spot prices override overlapping forecasts regardless of timezone notation", () => {
  const data = buildCombinedData(
    prices([["2026-11-03T18:00:00Z", 0]]),
    forecast(["2026-11-03T19:00:00+01:00", "2026-11-03T18:15:00Z"]), now,
  );
  assert.equal(data[0].actual, 0);
  assert.equal(data[0].predicted, undefined);
  assert.equal(data[0].band, undefined);
  assert.equal(data[1].predicted, 0.5);
  assert.deepEqual(data[1].band, [0.3, 0.7]);
});

test("forecasts fill unpublished slots even between actual spot prices", () => {
  const data = buildCombinedData(
    prices([["2026-11-03T18:30:00Z", 1], ["2026-11-03T18:00:00Z", 0.8]]),
    forecast(["2026-11-03T18:45:00Z", "2026-11-03T18:15:00Z"]), now,
  );
  assert.deepEqual(data.slice(0, 4).map((p) => p.actual ?? p.predicted), [0.8, 0.5, 1, 0.5]);
});

test("a buffered forecast covers all seven days between refreshes", () => {
  const issued = new Date("2026-11-03T18:05:00Z");
  const targets = Array.from({ length: 768 }, (_, i) =>
    new Date(Date.parse("2026-11-03T18:15:00Z") + i * 15 * 60_000).toISOString());
  const later = new Date(issued.getTime() + 7 * 3600_000);
  const data = buildCombinedData(null, forecast(targets), later);
  assert.equal(pricedPoints(data).length, 673);
  assert.equal(new Date(data.at(-1).ts).getTime(), priceChartWindow(later).end);
});

test("partial or unavailable forecasts retain the full window with honest gaps", () => {
  const data = buildCombinedData(null, forecast(["2026-11-04T00:00:00Z"]), now);
  assert.equal(data.length, 673);
  assert.equal(pricedPoints(data).length, 1);
  assert.deepEqual(data.at(-1), { ts: "2026-11-10T18:00:00.000Z" });
  const spotOnly = buildCombinedData(prices([["2026-11-03T18:00:00Z", 1.5]]), null, now);
  assert.equal(spotOnly[0].actual, 1.5);
  assert.equal(pricedPoints(spotOnly).length, 1);
});

test("the window advances with time and keeps 168 hours across a clock change", () => {
  const before = priceChartWindow(new Date("2026-10-24T18:08:00Z"));
  const after = priceChartWindow(new Date("2026-10-25T18:08:00Z"));
  assert.equal(before.end - before.start, 168 * 3600_000);
  assert.equal(after.start - before.start, 24 * 3600_000);
  assert.equal(after.end - before.end, 24 * 3600_000);
});
