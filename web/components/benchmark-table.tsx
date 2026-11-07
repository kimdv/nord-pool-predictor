import type { BenchmarkResponse } from "@/lib/api";

function fmtNum(n: number | null | undefined, decimals = 4): string {
  if (n == null) return "—";
  return n.toFixed(decimals);
}

export default function BenchmarkTable({ benchmarks }: { benchmarks: BenchmarkResponse }) {
  return (
    <div className="min-w-0">
      <h2 className="text-sm font-medium text-gray-500 mb-4">Model vs basislinjer</h2>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-400 border-b border-gray-100">
              <th className="pb-3 pr-4 font-medium">Metode</th>
              <th className="pb-3 pr-4 text-right font-medium">MAE</th>
              <th className="pb-3 pr-4 text-right font-medium">RMSE</th>
              <th className="pb-3 text-right font-medium">Bias</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-b border-gray-50 bg-blue-50/50">
              <td className="py-3 pr-4 font-semibold text-blue-600">ML-model</td>
              <td className="py-3 pr-4 text-right tabular-nums text-gray-700">
                {fmtNum(benchmarks.ml_mae)}
              </td>
              <td className="py-3 pr-4 text-right tabular-nums text-gray-700">
                {fmtNum(benchmarks.ml_rmse)}
              </td>
              <td className="py-3 text-right tabular-nums text-gray-400">—</td>
            </tr>
            {benchmarks.baselines.map((b) => (
              <tr key={b.baseline_name} className="border-b border-gray-50">
                <td className="py-3 pr-4 text-gray-600 capitalize">
                  {b.baseline_name.replace(/_/g, " ")}
                </td>
                <td className="py-3 pr-4 text-right tabular-nums text-gray-700">
                  {fmtNum(b.mae)}
                </td>
                <td className="py-3 pr-4 text-right tabular-nums text-gray-700">
                  {fmtNum(b.rmse)}
                </td>
                <td className="py-3 text-right tabular-nums text-gray-700">
                  {b.bias != null
                    ? `${b.bias >= 0 ? "+" : ""}${b.bias.toFixed(4)}`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
