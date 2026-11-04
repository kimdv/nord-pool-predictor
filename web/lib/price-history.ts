export const HISTORY_WINDOW_MS = 168 * 3600_000;
const SLOT_MS = 15 * 60_000;
export const HISTORY_SLOT_COUNT = HISTORY_WINDOW_MS / SLOT_MS;

export interface PriceRange {
  min: number;
  max: number;
  count: number;
}

export function historyWindow(now = new Date()) {
  const end = Math.floor(now.getTime() / SLOT_MS) * SLOT_MS;
  return { start: end - HISTORY_WINDOW_MS, end };
}

export function historicalRange(
  points: { ts: string; value: number | null | undefined }[],
  end: number,
): PriceRange | null {
  const values = new Map<number, number>();
  for (const point of points) {
    const ts = Date.parse(point.ts);
    if (ts >= end - HISTORY_WINDOW_MS && ts < end && ts % SLOT_MS === 0 &&
        typeof point.value === "number" && Number.isFinite(point.value)) {
      values.set(ts, point.value);
    }
  }
  if (!values.size) return null;
  return { min: Math.min(...values.values()), max: Math.max(...values.values()), count: values.size };
}
