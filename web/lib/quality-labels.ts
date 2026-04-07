export const QUALITY_LABELS: Record<string, string> = {
  excellent: "Fremragende",
  good: "God",
  fair: "Acceptabel",
  poor: "Dårlig",
};

export function translateQuality(q: string | null | undefined): string {
  if (!q) return "—";
  return QUALITY_LABELS[q.toLowerCase()] ?? q;
}

export const QUALITY_COLORS: Record<string, string> = {
  excellent: "emerald",
  good: "blue",
  fair: "amber",
  poor: "red",
};

export function qualityColor(q: string | null | undefined): string {
  if (!q) return "slate";
  return QUALITY_COLORS[q.toLowerCase()] ?? "red";
}

export const QUALITY_CSS: Record<string, string> = {
  excellent: "bg-emerald-50 text-emerald-700 border-emerald-200",
  good: "bg-blue-50 text-blue-700 border-blue-200",
  fair: "bg-amber-50 text-amber-700 border-amber-200",
  poor: "bg-red-50 text-red-700 border-red-200",
};
