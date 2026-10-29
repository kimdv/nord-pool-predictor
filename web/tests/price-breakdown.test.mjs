import test from "node:test";
import assert from "node:assert/strict";
import { buildPriceBreakdownSeries } from "../lib/price-breakdown.ts";

const slot = {
  ts: "2026-01-01T00:00:00Z", hour: 1, minute: 0,
  spot_price: 0, spot_source: "actual", grid_tariff: 0.5,
  system_tariff: 0.1, transmission_tariff: 0.1,
  grid_loss_tariff: 0.1, electricity_tax: 0.1,
  total_ex_vat: 0.9, vat: 0.225, total_incl_vat: 1.125,
};

test("a legitimate zero spot price retains its complete tariff total", () => {
  const [point] = buildPriceBreakdownSeries([slot], new Set(), 0);
  assert.equal(point.spot_price, 0);
  assert.equal(point.total, 1.125);
  assert.equal(point.isCurrent, true);
});

test("missing spot prices stay chart gaps even when spot display is disabled", () => {
  const [point] = buildPriceBreakdownSeries(
    [{ ...slot, spot_price: null, spot_source: "missing" }], new Set(["spot_price"]), -1,
  );
  assert.equal(point.total, null);
  assert.equal(point.grid_tariff, null);
  assert.equal(point.vat, null);
});

test("estimated prices retain their source through chart transformations", () => {
  const [point] = buildPriceBreakdownSeries(
    [{ ...slot, spot_source: "forecast" }], new Set(), -1,
  );
  assert.equal(point.spot_source, "forecast");
});
