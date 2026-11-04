import { fetchAreas, fetchHA, fetchHealth } from "@/lib/api";
import DashboardContent from "@/components/dashboard-content";
import BackendUnavailable from "@/components/backend-unavailable";

export default async function DashboardPage() {
  const summaryAsOf = Date.now();
  let areas;
  let health;

  try {
    [areas, health] = await Promise.all([fetchAreas(), fetchHealth()]);
  } catch {
    return <BackendUnavailable />;
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

  return <DashboardContent areaData={areaData} sources={health.sources} summaryAsOf={summaryAsOf} />;
}
