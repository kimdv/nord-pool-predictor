import assert from "node:assert/strict";
import test from "node:test";
import { endOfTomorrowInCopenhagen, formatCopenhagenDateTime } from "../lib/copenhagen-time.ts";

const boundaries = [
  ["winter", "2026-01-15T12:00:00Z", "2026-01-16T22:59:59.999Z"],
  ["summer", "2026-07-15T12:00:00Z", "2026-07-16T21:59:59.999Z"],
  ["spring clock change", "2026-03-28T12:00:00Z", "2026-03-29T21:59:59.999Z"],
  ["autumn clock change", "2026-10-24T12:00:00Z", "2026-10-25T22:59:59.999Z"],
  ["Copenhagen already past midnight", "2026-01-15T23:30:00Z", "2026-01-17T22:59:59.999Z"],
  ["month rollover", "2026-01-30T12:00:00Z", "2026-01-31T22:59:59.999Z"],
  ["year rollover", "2026-12-30T12:00:00Z", "2026-12-31T22:59:59.999Z"],
];

for (const [label, now, expected] of boundaries) {
  test(`tomorrow ends at Copenhagen midnight: ${label}`, () => {
    const original = process.env.TZ;
    try {
      for (const zone of ["UTC", "Asia/Tokyo", "America/New_York", "Pacific/Honolulu"]) {
        process.env.TZ = zone;
        assert.equal(endOfTomorrowInCopenhagen(new Date(now)), expected, zone);
      }
    } finally {
      if (original === undefined) delete process.env.TZ;
      else process.env.TZ = original;
    }
  });
}

test("chart times use Copenhagen regardless of the viewer's timezone", () => {
  const original = process.env.TZ;
  const options = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  try {
    for (const zone of ["UTC", "Asia/Tokyo", "America/New_York"]) {
      process.env.TZ = zone;
      assert.equal(formatCopenhagenDateTime("2026-01-15T10:15:00Z", options), "11.15");
      assert.equal(formatCopenhagenDateTime("2026-07-15T10:15:00Z", options), "12.15");
    }
  } finally {
    if (original === undefined) delete process.env.TZ;
    else process.env.TZ = original;
  }
});
