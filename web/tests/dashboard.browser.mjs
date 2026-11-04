import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import { after, afterEach, before, beforeEach, test } from "node:test";
import { chromium } from "playwright";

// Run next dev on 3106 with API_BASE_URL=http://127.0.0.1:8000, then npm run test:browser.
// Install its browser once with npx playwright install chromium. All API data is disposable.
const baseURL = process.env.BROWSER_TEST_URL ?? "http://127.0.0.1:3106";
const codes = ["C", "B", "A"].map((code) => ({ code, note: `Nettarif ${code}`, description: "" }));
let price, failHA, failTariff, haDelay, haRequests, tariffRequests;
let browser, context, page, runtimeErrors;
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://test");
  let body = [];
  let status = 200;
  if (url.pathname === "/api/areas") body = [{ code: "DK1", label: "Vestdanmark", weather_points: [] }];
  else if (url.pathname === "/api/health") body = { status: "ok", sources: [], degraded: false };
  else if (url.pathname === "/api/ha/DK1") {
    haRequests++;
    await delay(haDelay);
    status = failHA ? 503 : 200;
    body = { state: price, attributes: { current_price: price, next_slot_price: price + 0.1,
      today_min: price, today_max: price, today_average: price, current_price_source: "actual" } };
  } else if (url.pathname === "/api/tariffs/grid-companies") {
    body = [{ gln: "multi", name: "Flere tariffer" }, { gln: "sole", name: "En tarif" }];
  } else if (url.pathname.endsWith("/multi/codes")) body = codes;
  else if (url.pathname.endsWith("/sole/codes")) body = codes.slice(0, 1);
  else if (url.pathname.startsWith("/api/tariffs/breakdown/")) {
    tariffRequests++;
    status = failTariff ? 503 : 200;
    const ts = new Date(Math.floor(Date.now() / 900_000) * 900_000).toISOString();
    body = { slots: [{ ts, hour: 12, minute: 0, spot_price: price, spot_source: "actual",
      grid_tariff: 0.1, system_tariff: 0.1, transmission_tariff: 0.1, grid_loss_tariff: 0.1,
      electricity_tax: 0.1, total_ex_vat: price + 0.5, vat: (price + 0.5) * 0.25,
      total_incl_vat: (price + 0.5) * 1.25 }], historical_slots: [] };
  } else if (url.pathname.startsWith("/api/prices/")) body = { area: "DK1", prices: [] };
  else if (url.pathname.endsWith("/latest") || url.pathname.endsWith("/benchmarks")) status = 404;
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
});

before(async () => {
  server.listen(8000, "127.0.0.1");
  await once(server, "listening");
  browser = await chromium.launch();
});

after(async () => {
  await context?.close();
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
});

beforeEach(async () => {
  await context?.close();
  price = 0.5;
  failHA = failTariff = false;
  haDelay = haRequests = tariffRequests = 0;
  context = await browser.newContext();
  page = await context.newPage();
  runtimeErrors = [];
  page.on("pageerror", (error) => runtimeErrors.push(error.message));
  await page.goto(baseURL);
  await page.getByRole("button", { name: "Vælg netselskab" }).waitFor();
});

afterEach(() => assert.deepEqual(runtimeErrors, []));

async function openCodes() {
  await page.locator(".dashboard-settings").getByRole("button").last().click();
  await page.getByRole("button", { name: "Flere tariffer", exact: true }).click();
  await page.getByText("Vælg tarif for Flere tariffer", { exact: true }).waitFor();
  for (const code of codes) await page.getByRole("button", { name: code.note, exact: true }).waitFor();
}

async function savedSelection() {
  return page.evaluate(() => JSON.parse(localStorage.getItem("gridCompanySelection")));
}

async function resumeTab(times = 1) {
  await page.evaluate((times) => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    for (let i = 0; i < times; i++) document.dispatchEvent(new Event("visibilitychange"));
  }, times);
}

test("every multi-tariff option stays available and can be saved", async () => {
  for (const code of codes) {
    await openCodes();
    await page.getByRole("button", { name: code.note, exact: true }).click();
    await page.getByRole("button", { name: `Flere tariffer · ${code.note}`, exact: true }).waitFor();
    assert.equal((await savedSelection()).code, code.code);
  }
  await page.reload();
  await page.getByRole("button", { name: "Flere tariffer · Nettarif A", exact: true }).waitFor();
  await openCodes();
  await page.locator("h1").click();
  assert.equal((await savedSelection()).code, "A");
});

test("a sole option is selected automatically", async () => {
  await page.getByRole("button", { name: "Vælg netselskab" }).click();
  await page.getByRole("button", { name: "En tarif", exact: true }).click();
  await page.getByRole("button", { name: "En tarif · Nettarif C", exact: true }).waitFor();
  assert.equal((await savedSelection()).code, "C");
});

test("tab recovery refreshes the server summary and coalesces simultaneous signals", async () => {
  const before = haRequests;
  price = 0.75;
  haDelay = 200;
  await resumeTab(3);
  await page.locator(".current-price-value").filter({ hasText: "0.75" }).waitFor();
  assert.equal(haRequests - before, 1);
});

test("quarter-hour rollover refreshes the server-provided current price", async () => {
  const boundary = Math.floor(Date.now() / 900_000) * 900_000;
  await page.clock.install({ time: new Date(boundary - 60_000) });
  price = 0.6;
  await resumeTab();
  await page.locator(".current-price-value").filter({ hasText: "0.60" }).waitFor();
  price = 0.7;
  await page.clock.runFor(60_000);
  await page.locator(".current-price-value").filter({ hasText: "0.70" }).waitFor();
});

test("a failed summary refresh removes the old price and permits retry", async () => {
  failHA = true;
  await resumeTab();
  const error = page.getByRole("alert").filter({ hasText: "Kunne ikke hente aktuelle spotpriser." });
  await error.waitFor();
  assert.match(await page.locator(".current-price-value").innerText(), /—/);
  failHA = false;
  price = 0.9;
  await error.getByRole("button", { name: "Prøv igen" }).click();
  await page.locator(".current-price-value").filter({ hasText: "0.90" }).waitFor();
});

test("a browser clock ahead of the server accepts new summaries and retries", async () => {
  const boundary = Math.floor(Date.now() / 900_000) * 900_000;
  await page.clock.install({ time: new Date(boundary + 900_000 + 60_000) });
  price = 0.95;
  await resumeTab();
  await page.locator(".current-price-value").filter({ hasText: "0.95" }).waitFor({ timeout: 5000 });

  failHA = true;
  await resumeTab();
  const error = page.getByRole("alert").filter({ hasText: "Kunne ikke hente aktuelle spotpriser." });
  await error.waitFor();
  assert.match(await page.locator(".current-price-value").innerText(), /—/);
  failHA = false;
  price = 1.05;
  await error.getByRole("button", { name: "Prøv igen" }).click();
  await page.locator(".current-price-value").filter({ hasText: "1.05" }).waitFor({ timeout: 5000 });
  assert.equal(await error.count(), 0);
});

test("signals during a refresh produce only one trailing refresh", async () => {
  const before = haRequests;
  haDelay = 200;
  price = 0.8;
  await resumeTab();
  await page.waitForFunction(() => document.querySelector(".today-summary")?.getAttribute("aria-busy") === "true");
  const trailingResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/" && haRequests - before === 2);
  await resumeTab(3);
  await trailingResponse;
  await page.locator(".current-price-value").filter({ hasText: "0.80" }).waitFor();
  await page.waitForFunction(() => document.querySelector(".today-summary")?.getAttribute("aria-busy") === "false");
  assert.equal(haRequests - before, 2);
});

test("Copenhagen midnight refreshes tariffs and a failure does not show yesterday's chart", async () => {
  await page.getByRole("button", { name: "Vælg netselskab" }).click();
  await page.getByRole("button", { name: "En tarif", exact: true }).click();
  await page.getByRole("button", { name: "En tarif · Nettarif C", exact: true }).waitFor();
  await page.getByRole("button", { name: "Nettarif", exact: true }).waitFor();
  const before = tariffRequests;
  failTariff = true;
  await page.clock.install({ time: new Date("2026-11-04T22:59:59.999Z") });
  await resumeTab();
  await page.clock.runFor(1);
  const error = page.getByRole("alert").filter({ hasText: "Kunne ikke hente prissammensætningen." });
  await error.waitFor();
  assert.ok(tariffRequests > before);
  assert.equal(await page.getByRole("button", { name: "Nettarif", exact: true }).count(), 0);
});
