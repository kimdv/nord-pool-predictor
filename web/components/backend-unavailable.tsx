"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function BackendUnavailable() {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => router.refresh(), 5000);
    return () => clearInterval(timer);
  }, [router]);

  return (
    <div className="mx-auto max-w-7xl px-4 py-16 text-center">
      <h2 className="text-xl font-semibold text-gray-900 mb-2">Backend ikke tilgængelig</h2>
      <p className="text-sm text-gray-500">Forbindelsen forsøges igen automatisk.</p>
      <button onClick={() => router.refresh()} className="mt-4 underline">Prøv igen</button>
    </div>
  );
}
