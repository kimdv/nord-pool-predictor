import { fetchAreas, fetchHA, fetchHealth } from "@/lib/api";
import DashboardContent from "@/components/dashboard-content";

export default async function DashboardPage() {
  let areas;
  let health;

  try {
    [areas, health] = await Promise.all([fetchAreas(), fetchHealth()]);
  } catch {
    return (
      <div className="mx-auto max-w-7xl px-4 py-16 text-center">
        <h2 className="text-xl font-semibold text-gray-900 mb-2">
          Backend ikke tilgængelig
        </h2>
        <p className="text-sm text-gray-500">
          Kunne ikke oprette forbindelse til API&apos;et. Prøv igen om lidt.
        </p>
      </div>
    );
  }

  const areaData = await Promise.all(
    areas.map(async (info) => {
      try {
        const ha = await fetchHA(info.code);
        return { info, ha };
      } catch {
        return { info, ha: null };
      }
    }),
  );

  return <DashboardContent areaData={areaData} sources={health.sources} />;
}
