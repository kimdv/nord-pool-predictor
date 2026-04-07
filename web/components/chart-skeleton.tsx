export default function ChartSkeleton({ height = "h-80" }: { height?: string }) {
  return (
    <div className="card p-6 animate-pulse">
      <div className="h-4 w-48 bg-gray-200 rounded mb-4" />
      <div className={`${height} bg-gray-100 rounded`} />
    </div>
  );
}
