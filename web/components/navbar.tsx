import Link from "next/link";

export default function Navbar() {
  return (
    <header className="site-header">
      <nav className="site-nav" aria-label="Hovednavigation">
        <Link href="/" className="brand" aria-label="Nord Pool Predictor, forside">
          <svg className="brand-mark" viewBox="0 0 40 40" fill="none" aria-hidden="true">
            <path d="M4 25h7l6-15 7 23 5-15h7" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span>Nord Pool <span className="brand-secondary">Predictor</span></span>
        </Link>
        <div className="nav-links">
          <Link href="/#prisoverblik">Prisoverblik</Link>
          <Link href="/#analyse">Prognoseanalyse</Link>
          <Link href="/#datakilder">Datakilder</Link>
        </div>
        <span className="country-label"><span aria-hidden="true" className="danish-flag" />Danmark</span>
      </nav>
    </header>
  );
}
