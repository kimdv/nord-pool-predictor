"use client";

import { useState, useCallback, useEffect } from "react";
import { readPreference, writePreference } from "@/lib/preference-storage";

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
  const [selected, setSelected] = useState<AreaCode>("DK1");

  useEffect(() => {
    const stored = readPreference(STORAGE_KEY);
    if (stored && isAreaCode(stored)) setSelected(stored);
  }, []);

  const select = useCallback((code: AreaCode) => {
    setSelected(code);
    writePreference(STORAGE_KEY, code);
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
    <div className="area-switcher" role="group" aria-label="Prisområde">
      {AREAS.map((a) => (
        <button
          key={a.code}
          onClick={() => onSelect(a.code)}
          aria-pressed={selected === a.code}
          className={selected === a.code ? "area-option is-selected" : "area-option"}
        >
          {a.label} ({a.code})
        </button>
      ))}
    </div>
  );
}
