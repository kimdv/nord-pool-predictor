import assert from "node:assert/strict";
import test from "node:test";
import { readPreference, writePreference } from "../lib/preference-storage.ts";

function browser(t, value) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", { configurable: true, value });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "window", previous);
    else delete globalThis.window;
  });
}

test("preferences are optional during server rendering", (t) => {
  browser(t, undefined);
  assert.equal(readPreference("area"), null);
  assert.doesNotThrow(() => writePreference("area", "DK2"));
});

test("available storage still persists and restores values", (t) => {
  const values = new Map();
  browser(t, { localStorage: {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  } });
  assert.equal(readPreference("area"), null);
  writePreference("area", "DK2");
  assert.equal(readPreference("area"), "DK2");
});

test("a blocked localStorage property cannot break the UI", (t) => {
  const window = {};
  Object.defineProperty(window, "localStorage", { get() { throw new DOMException("Denied", "SecurityError"); } });
  browser(t, window);
  assert.equal(readPreference("area"), null);
  assert.doesNotThrow(() => writePreference("area", "DK2"));
});

test("a failed read falls back to an absent preference", (t) => {
  browser(t, { localStorage: { getItem() { throw new Error("Read denied"); } } });
  assert.equal(readPreference("area"), null);
});

test("a full storage quota does not escape from writes", (t) => {
  browser(t, { localStorage: { setItem() { throw new DOMException("Full", "QuotaExceededError"); } } });
  assert.doesNotThrow(() => writePreference("area", "DK2"));
});
