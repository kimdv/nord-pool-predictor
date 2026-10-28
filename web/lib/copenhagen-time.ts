export const COPENHAGEN_TIME_ZONE = "Europe/Copenhagen";

const calendarFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: COPENHAGEN_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function calendarParts(date: Date): Record<string, number> {
  return Object.fromEntries(calendarFormatter.formatToParts(date).map(({ type, value }) => [type, Number(value)]));
}

export function endOfTomorrowInCopenhagen(now = new Date()): string {
  const today = calendarParts(now);
  const midnight = Date.UTC(today.year, today.month - 1, today.day + 2);
  let instant = midnight;

  // Convert the calendar midnight to UTC using the offset on that date.
  for (let i = 0; i < 3; i++) {
    const local = calendarParts(new Date(instant));
    const represented = Date.UTC(local.year, local.month - 1, local.day, local.hour, local.minute, local.second);
    const next = midnight - (represented - instant);
    if (next === instant) break;
    instant = next;
  }

  return new Date(instant - 1).toISOString();
}

const defaultOptions: Intl.DateTimeFormatOptions = {
  year: "numeric", month: "numeric", day: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
};
const formatters = new Map<string, Intl.DateTimeFormat>();

export function formatCopenhagenDateTime(
  value: string | Date,
  options: Intl.DateTimeFormatOptions = defaultOptions,
): string {
  const key = JSON.stringify(options);
  let formatter = formatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("da-DK", { ...options, timeZone: COPENHAGEN_TIME_ZONE });
    formatters.set(key, formatter);
  }
  return formatter.format(typeof value === "string" ? new Date(value) : value);
}
