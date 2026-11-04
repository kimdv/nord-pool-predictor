export const PRICE_SLOT_MS = 15 * 60_000;

export function createDashboardRefresh(refresh: () => void) {
  let stopped = false;
  let running = false;
  let repeat = false;
  let queued: ReturnType<typeof setTimeout> | undefined;
  let boundary: ReturnType<typeof setTimeout>;

  function request() {
    if (stopped) return;
    if (running) {
      repeat = true;
      return;
    }
    if (queued !== undefined) return;
    queued = setTimeout(() => {
      queued = undefined;
      running = true;
      refresh();
    }, 0);
  }

  function scheduleBoundary() {
    clearTimeout(boundary);
    // Copenhagen midnight and clock changes also fall on this quarter-hour grid.
    boundary = setTimeout(() => {
      if (document.visibilityState === "visible") request();
      scheduleBoundary();
    }, PRICE_SLOT_MS - Date.now() % PRICE_SLOT_MS);
  }

  function onVisibilityChange() {
    if (document.visibilityState !== "visible") return;
    request();
    scheduleBoundary();
  }

  document.addEventListener("visibilitychange", onVisibilityChange);
  scheduleBoundary();

  return {
    request,
    complete() {
      if (!running || stopped) return;
      running = false;
      if (repeat) {
        repeat = false;
        request();
      }
    },
    dispose() {
      stopped = true;
      clearTimeout(boundary);
      clearTimeout(queued);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    },
  };
}
