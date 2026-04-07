"use client";

export default function ErrorPage({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="mx-auto max-w-7xl px-4 py-16 text-center">
      <h2 className="text-xl font-semibold text-gray-900 mb-2">
        Noget gik galt
      </h2>
      <p className="text-sm text-gray-500 mb-6">
        {error.message || "En uventet fejl opstod."}
      </p>
      <button
        onClick={reset}
        className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 transition-colors"
      >
        Prøv igen
      </button>
    </div>
  );
}
