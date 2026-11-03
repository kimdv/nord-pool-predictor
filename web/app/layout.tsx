import type { Metadata } from "next";
import "./globals.css";
import Navbar from "@/components/navbar";
import DisclaimerBanner from "@/components/disclaimer-banner";

export const metadata: Metadata = {
  title: "Nord Pool Predictor",
  description: "Elprisprognose for DK1 og DK2",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="da">
      <body>
        <a className="skip-link" href="#main-content">Gå til indhold</a>
        <Navbar />
        <main id="main-content">{children}</main>
        <DisclaimerBanner />
      </body>
    </html>
  );
}
