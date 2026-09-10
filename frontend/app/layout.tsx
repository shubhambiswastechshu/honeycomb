import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Roboto, Roboto_Mono } from "next/font/google";
import AppChrome from "@/components/dashboard/AppChrome";
import "./globals.css";

/**
 * The two typefaces, self-hosted.
 *
 * next/font downloads them at BUILD time and serves them from this origin, so
 * there is no request to Google at runtime, nothing to block first paint, and
 * no layout shift when they swap in -- the metrics are known before the page
 * is sent. It is part of Next itself, so this costs no new dependency.
 *
 * Roboto ships as static cuts rather than a variable axis, so the weights the
 * stylesheets ask for have to be listed. It has no 600: CSS font matching
 * resolves the app's `font-weight: 600` rules up to 700.
 */
const sans = Roboto({
  subsets: ["latin"],
  display: "swap",
  weight: ["300", "400", "500", "700"],
  variable: "--font-sans",
});

const mono = Roboto_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Honeycomb",
  description: "Honeycomb authentication",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={sans.variable + " " + mono.variable}>
      <body>
        {/* AppChrome adds the centred column + site footer outside /dashboard,
            and steps aside for the dashboard's own full-viewport shell. */}
        <AppChrome>{children}</AppChrome>
      </body>
    </html>
  );
}
