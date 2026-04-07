"use client";

import { useState, useCallback } from "react";

export const AREA_CODES = ["DK1", "DK2"] as const;
export type AreaCode = (typeof AREA_CODES)[number];

const AREAS: readonly { code: AreaCode; label: string }[] = [
  { code: "DK1", label: "Vest" },
  { code: "DK2", label: "Øst" },
];

export function isAreaCode(value: string): value is AreaCode {
  return (AREA_CODES as readonly string[]).includes(value);
}

const STORAGE_KEY = "preferredArea";

export function useSelectedArea() {
  const [selected, setSelected] = useState<AreaCode>(() => {
    if (typeof window === "undefined") return "DK1";
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored && isAreaCode(stored) ? stored : "DK1";
  });

  const select = useCallback((code: AreaCode) => {
    setSelected(code);
    localStorage.setItem(STORAGE_KEY, code);
  }, []);

  return { selected, select };
}

export default function AreaSwitcher({
  selected,
  onSelect,
}: {
  selected: AreaCode;
  onSelect: (code: AreaCode) => void;
}) {
  return (
    <div className="inline-flex rounded-lg bg-gray-100 p-1 gap-0.5">
      {AREAS.map((a) => (
        <button
          key={a.code}
          onClick={() => onSelect(a.code)}
          className={`px-3.5 py-1.5 rounded-md text-sm font-medium transition-all ${
            selected === a.code
              ? "bg-white text-gray-900 shadow-sm"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          {a.label} ({a.code})
        </button>
      ))}
    </div>
  );
}
