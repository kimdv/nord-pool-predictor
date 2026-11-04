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
    <div className="connection-state">
      <div className="connection-symbol" aria-hidden="true">~</div>
      <h1>Vi kan ikke hente elpriserne lige nu.</h1>
      <p className="text-sm text-gray-500">Forbindelsen til prisdata er afbrudt. Vi prøver igen automatisk hvert femte sekund.</p>
      <button onClick={() => router.refresh()} className="retry-button">Prøv igen</button>
    </div>
  );
}
