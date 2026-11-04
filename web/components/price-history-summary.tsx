import { HISTORY_SLOT_COUNT, type PriceRange } from "@/lib/price-history";

export default function PriceHistorySummary({ range, loading = false }: {
  range: PriceRange | null;
  loading?: boolean;
}) {
  if (!range) {
    return <p role="status" className="mb-3 text-xs text-gray-500">{loading
      ? "Henter historisk sammenligning…"
      : "Historisk sammenligning er ikke tilgængelig"}</p>;
  }
  return (
    <div className="mb-3 text-xs text-gray-600">
      <dl className="flex flex-wrap gap-x-5 gap-y-1">
        <div><dt className="inline">Laveste sidste 7 dage: </dt><dd className="inline font-medium">{range.min.toFixed(4)} DKK/kWh</dd></div>
        <div><dt className="inline">Højeste sidste 7 dage: </dt><dd className="inline font-medium">{range.max.toFixed(4)} DKK/kWh</dd></div>
      </dl>
      {range.count < HISTORY_SLOT_COUNT && <p role="status" className="mt-1 text-amber-700">Ufuldstændige historiske data ({range.count} af {HISTORY_SLOT_COUNT} kvarterer).</p>}
    </div>
  );
}
