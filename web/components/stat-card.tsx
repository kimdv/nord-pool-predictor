interface StatCardProps {
  label: string;
  value: string;
  unit?: string;
  color?: string;
}

const accents: Record<string, { border: string; text: string; bg: string }> = {
  blue:    { border: "border-l-blue-500",    text: "text-blue-600",    bg: "bg-blue-50" },
  emerald: { border: "border-l-emerald-500", text: "text-emerald-600", bg: "bg-emerald-50" },
  amber:   { border: "border-l-amber-500",   text: "text-amber-600",   bg: "bg-amber-50" },
  red:     { border: "border-l-red-500",     text: "text-red-600",     bg: "bg-red-50" },
  slate:   { border: "border-l-gray-400",    text: "text-gray-700",    bg: "bg-gray-50" },
};

export default function StatCard({ label, value, unit, color = "blue" }: StatCardProps) {
  const a = accents[color] ?? accents.blue;

  return (
    <div className={`card border-l-4 ${a.border} p-4`}>
      <p className="text-xs font-medium uppercase tracking-wide text-gray-400 mb-1.5">
        {label}
      </p>
      <p className={`text-2xl font-bold ${a.text}`}>
        {value}
      </p>
      {unit && (
        <p className="mt-0.5 text-xs text-gray-400">{unit}</p>
      )}
    </div>
  );
}
