"use client";

export default function RequestError({
  message,
  onRetry,
  retrying = false,
}: {
  message: string;
  onRetry: () => void;
  retrying?: boolean;
}) {
  return (
    <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <p>{message}</p>
      <button
        type="button"
        onClick={onRetry}
        disabled={retrying}
        className="mt-2 rounded-md border border-red-300 px-3 py-1 font-medium hover:bg-red-100 disabled:opacity-50"
      >
        {retrying ? "Henter..." : "Prøv igen"}
      </button>
    </div>
  );
}
