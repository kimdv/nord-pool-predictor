import assert from "node:assert/strict";
import test from "node:test";
import { fetchJson, fetchOptionalJson, HttpError } from "../lib/client-api.ts";

test("successful empty responses remain valid data", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json([]));
  assert.deepEqual(await fetchJson("/prices"), []);
});

test("HTTP failures retain their status instead of becoming empty data", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 502 }));
  await assert.rejects(fetchJson("/prices"), (error) => error instanceof HttpError && error.status === 502);
});

test("network failures propagate to the UI", async (t) => {
  const failure = new TypeError("Network unavailable");
  t.mock.method(globalThis, "fetch", async () => { throw failure; });
  await assert.rejects(fetchJson("/prices"), (error) => error === failure);
});

test("invalid JSON is a request failure", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("not json"));
  await assert.rejects(fetchJson("/prices"), SyntaxError);
});

test("optional resources treat only a 404 as absent", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 404 }));
  assert.equal(await fetchOptionalJson("/forecast"), null);
  fetch.mock.mockImplementation(async () => new Response(null, { status: 503 }));
  await assert.rejects(fetchOptionalJson("/forecast"), HttpError);
});

test("cancellation signals reach fetch unchanged", async (t) => {
  const controller = new AbortController();
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    assert.equal(options.signal, controller.signal);
    return Response.json({ value: 1 });
  });
  assert.deepEqual(await fetchJson("/prices", { signal: controller.signal }), { value: 1 });
});
