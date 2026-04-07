"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

interface GridCompany {
  gln: string;
  name: string;
}

interface TariffCode {
  code: string;
  note: string;
  description: string;
}

interface Selection {
  gln: string;
  companyName: string;
  code: string;
  codeName: string;
}

const STORAGE_KEY = "gridCompanySelection";

function loadSelection(): Selection | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Selection) : null;
  } catch {
    return null;
  }
}

function saveSelection(sel: Selection) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sel));
}

export function useGridSelection() {
  const [selection, setSelection] = useState<Selection | null>(() => loadSelection());

  const update = useCallback((sel: Selection) => {
    saveSelection(sel);
    setSelection(sel);
  }, []);

  return { selection, update };
}

export default function GridCompanySelector({
  selection,
  onSelect,
}: {
  selection: Selection | null;
  onSelect: (sel: Selection) => void;
}) {
  const [companies, setCompanies] = useState<GridCompany[]>([]);
  const [codes, setCodes] = useState<TariffCode[]>([]);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<"company" | "code">("company");
  const [pendingGln, setPendingGln] = useState<{
    gln: string;
    name: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/api/backend/tariffs/grid-companies")
      .then((r) => (r.ok ? r.json() : []))
      .then((data: GridCompany[]) => setCompanies(data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const filtered = useMemo(() => {
    if (!query) return companies;
    const q = query.toLowerCase();
    return companies.filter((c) => c.name.toLowerCase().includes(q));
  }, [companies, query]);

  function handlePickCompany(c: GridCompany) {
    setPendingGln({ gln: c.gln, name: c.name });
    setStep("code");
    setQuery("");
    setLoading(true);
    fetch(`/api/backend/tariffs/grid-companies/${c.gln}/codes`)
      .then((r) => (r.ok ? r.json() : []))
      .then((data: TariffCode[]) => {
        setCodes(data);
        setLoading(false);
        const residential = data.find(
          (t) =>
            /nettarif c\b/i.test(t.note) || /^c[d ]?$/i.test(t.code),
        );
        if (residential && data.length > 1) {
          handlePickCode(residential, c.gln, c.name);
        }
      })
      .catch(() => setLoading(false));
  }

  function handlePickCode(t: TariffCode, gln?: string, name?: string) {
    const g = gln ?? pendingGln?.gln ?? "";
    const n = name ?? pendingGln?.name ?? "";
    const sel: Selection = {
      gln: g,
      companyName: n,
      code: t.code,
      codeName: t.note,
    };
    onSelect(sel);
    setOpen(false);
    setStep("company");
    setQuery("");
  }

  const label = selection
    ? `${selection.companyName} · ${selection.codeName || selection.code}`
    : "Vælg netselskab";

  return (
    <div ref={wrapperRef} className="relative">
      <button
        onClick={() => {
          setOpen(!open);
          setStep("company");
          setQuery("");
        }}
        className="card inline-flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-gray-700 hover:bg-gray-50 transition-colors"
      >
        <svg
          className="h-4 w-4 text-gray-400"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"
          />
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"
          />
        </svg>
        <span className="truncate max-w-xs">{label}</span>
        <svg
          className="h-4 w-4 text-gray-400"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M19 9l-7 7-7-7"
          />
        </svg>
      </button>

      {open && (
        <div className="absolute left-0 top-full mt-1 z-50 w-80 rounded-lg bg-white border border-gray-200 shadow-lg overflow-hidden">
          {step === "company" && (
            <>
              <div className="p-2 border-b border-gray-100">
                <input
                  autoFocus
                  type="text"
                  placeholder="Søg netselskab..."
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  className="w-full rounded-md border border-gray-200 px-3 py-2 text-sm text-gray-800 placeholder-gray-400 focus:border-blue-300 focus:outline-none focus:ring-1 focus:ring-blue-300"
                />
              </div>
              <ul className="max-h-64 overflow-y-auto py-1">
                {filtered.length === 0 && (
                  <li className="px-4 py-3 text-sm text-gray-400">
                    Ingen resultater
                  </li>
                )}
                {filtered.map((c) => (
                  <li key={c.gln}>
                    <button
                      onClick={() => handlePickCompany(c)}
                      className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-blue-50 hover:text-blue-700 transition-colors"
                    >
                      {c.name}
                    </button>
                  </li>
                ))}
              </ul>
              <div className="px-4 py-2 border-t border-gray-100 text-xs text-gray-400">
                Find dit netselskab på din elregning
              </div>
            </>
          )}

          {step === "code" && (
            <>
              <div className="px-4 py-3 border-b border-gray-100 text-sm font-medium text-gray-700">
                Vælg tarif for {pendingGln?.name}
              </div>
              {loading ? (
                <div className="px-4 py-6 text-center text-sm text-gray-400">
                  Henter tariffer...
                </div>
              ) : (
                <ul className="max-h-64 overflow-y-auto py-1">
                  {codes.map((t) => (
                    <li key={t.code}>
                      <button
                        onClick={() => handlePickCode(t)}
                        className="w-full text-left px-4 py-2.5 hover:bg-blue-50 transition-colors"
                      >
                        <span className="block text-sm font-medium text-gray-700">
                          {t.note || t.code}
                        </span>
                        {t.description && (
                          <span className="block text-xs text-gray-400 mt-0.5 line-clamp-2">
                            {t.description.replace(/\r?\n/g, " ").trim()}
                          </span>
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <button
                onClick={() => {
                  setStep("company");
                  setQuery("");
                }}
                className="w-full px-4 py-2 border-t border-gray-100 text-xs text-blue-600 hover:text-blue-700 text-left"
              >
                &larr; Tilbage
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
