import Link from "next/link";

export default function Navbar() {
  return (
    <nav className="sticky top-0 z-40 bg-white border-b border-gray-100 shadow-sm">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="flex h-16 items-center justify-between">
          <Link href="/" className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-600 text-white font-bold text-sm">
              NP
            </div>
            <span className="text-lg font-semibold text-gray-900">
              Nord Pool Predictor
            </span>
          </Link>
          <span className="hidden sm:inline text-sm text-gray-400">
            Elprisprognose for Danmark
          </span>
        </div>
      </div>
    </nav>
  );
}
