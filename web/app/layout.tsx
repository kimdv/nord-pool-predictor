import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import Navbar from "@/components/navbar";
import DisclaimerBanner from "@/components/disclaimer-banner";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Nord Pool Predictor",
  description: "Elprisprognose for DK1 og DK2",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="da">
      <body className={inter.className}>
        <Navbar />
        <main className="min-h-screen pb-20">{children}</main>
        <DisclaimerBanner />
      </body>
    </html>
  );
}
