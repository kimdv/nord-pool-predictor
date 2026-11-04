import test from "node:test";
import assert from "node:assert/strict";
import { historicalRange, historyWindow, HISTORY_SLOT_COUNT } from "../lib/price-history.ts";
import { buildPriceBreakdownSeries } from "../lib/price-breakdown.ts";

const { start, end } = historyWindow(new Date("2026-10-25T12:08:37Z"));
const point = (time, value) => ({ ts: new Date(time).toISOString(), value });

test("history covers exactly 168 hours across DST, ending at the current slot", () => {
  assert.equal(end, Date.parse("2026-10-25T12:00:00Z"));
  assert.equal(end - start, 168 * 3600_000);
  assert.equal(HISTORY_SLOT_COUNT, 672);
});

test("only complete historical quarter-hours count, including zero and negative prices", () => {
  assert.deepEqual(historicalRange([
    point(start - 900_000, -99), point(start, -1), point(start + 900_000, 0),
    point(end - 900_000, 2), point(end, 99), point(end + 900_000, 100),
    point(start + 1, 50), point(start + 1800_000, null), point(start + 2700_000, NaN),
    point(start, -1),
  ], end), { min: -1, max: 2, count: 3 });
});

test("empty history is unavailable; equal values retain their count", () => {
  assert.equal(historicalRange([], end), null);
  assert.deepEqual(historicalRange([point(start, 0), point(start + 900_000, 0)], end),
    { min: 0, max: 0, count: 2 });
});

test("complete history counts all 672 slots", () => {
  const points = Array.from({ length: 672 }, (_, i) => point(start + i * 900_000, i));
  assert.deepEqual(historicalRange(points, end), { min: 0, max: 671, count: 672 });
});

test("historical totals follow categories and VAT using the existing chart transformation", () => {
  const slots = [{
    ts: new Date(start).toISOString(), spot_price: -0.2, spot_source: "actual",
    grid_tariff: 0.5, system_tariff: 0.1, transmission_tariff: 0.1,
    grid_loss_tariff: 0.1, electricity_tax: 0.1,
  }];
  const range = (disabled) => historicalRange(buildPriceBreakdownSeries(slots, new Set(disabled), -1)
    .map((p) => ({ ts: p.ts, value: p.total })), end);
  assert.ok(Math.abs(range([]).min - 0.875) < 1e-10);
  assert.ok(Math.abs(range(["grid_tariff"]).min - 0.25) < 1e-10);
  assert.ok(Math.abs(range(["vat"]).min - 0.7) < 1e-10);
});
