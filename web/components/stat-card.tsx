interface StatCardProps {
  label: string;
  value: string;
  unit?: string;
  color?: "emerald" | "amber";
}

export default function StatCard({ label, value, unit, color }: StatCardProps) {
  return (
    <div className={color ? `price-stat price-stat--${color}` : "price-stat"}>
      <dt>{label}</dt>
      <dd>{value}{unit && <span>{unit}</span>}</dd>
    </div>
  );
}
