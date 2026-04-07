import type { AccuracyRecord } from "@/lib/api";
import { translateQuality, QUALITY_CSS } from "@/lib/quality-labels";

function fmtNum(n: number | null | undefined, decimals = 4): string {
  if (n == null) return "—";
  return n.toFixed(decimals);
}

function Badge({ label }: { label: string | null }) {
  const key = (label ?? "").toLowerCase();
  const cls = QUALITY_CSS[key] ?? QUALITY_CSS.fair;
  return (
    <span className={`inline-block rounded-full border px-2.5 py-0.5 text-xs font-medium ${cls}`}>
      {translateQuality(label)}
    </span>
  );
}

export default function AccuracyCard({ records }: { records: AccuracyRecord[] }) {
  if (records.length === 0) {
    return (
      <div className="card p-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-2">Seneste prognosenøjagtighed</h2>
        <p className="text-sm text-gray-400">Ingen nøjagtighedsdata tilgængelig endnu.</p>
      </div>
    );
  }

  return (
    <div className="card p-6">
      <h2 className="text-lg font-semibold text-gray-900 mb-4">Seneste prognosenøjagtighed</h2>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-gray-400 border-b border-gray-100">
              <th className="pb-3 pr-4 font-medium">Udgivet</th>
              <th className="pb-3 pr-4 text-right font-medium">MAE</th>
              <th className="pb-3 pr-4 text-right font-medium">RMSE</th>
              <th className="pb-3 pr-4 text-right font-medium">Bias</th>
              <th className="pb-3 font-medium">Kvalitet</th>
            </tr>
          </thead>
          <tbody>
            {records.map((r) => (
              <tr key={r.run_id} className="border-b border-gray-50">
                <td className="py-3 pr-4 text-gray-600">
                  {new Date(r.issued_at).toLocaleDateString("da-DK", {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </td>
                <td className="py-3 pr-4 text-right font-mono text-gray-700">
                  {fmtNum(r.mae_24h)}
                </td>
                <td className="py-3 pr-4 text-right font-mono text-gray-700">
                  {fmtNum(r.rmse_24h)}
                </td>
                <td className="py-3 pr-4 text-right font-mono text-gray-700">
                  {r.bias_24h != null
                    ? `${r.bias_24h >= 0 ? "+" : ""}${r.bias_24h.toFixed(4)}`
                    : "—"}
                </td>
                <td className="py-3">
                  <Badge label={r.quality_label} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
