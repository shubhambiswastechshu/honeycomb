import type { Metadata } from "next";
import type { ReactNode } from "react";
import { JetBrains_Mono, Plus_Jakarta_Sans } from "next/font/google";
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
 * Plus Jakarta Sans for everything read: warmer and rounder than the system
 * stack, which suits a product whose whole identity is amber and hexagons,
 * while staying plain enough for a dashboard full of small labels.
 *
 * JetBrains Mono wherever the product shows machine text -- tool names, MCP
 * URLs, key prefixes. That is a lot of this app, and the system monospace
 * stack renders as something different on every machine. It also has the
 * disambiguation this content needs: a slashed zero and distinguishable
 * l/1/I, which matters when someone is checking a pasted endpoint slug.
 */
const sans = Plus_Jakarta_Sans({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-sans",
});

const mono = JetBrains_Mono({
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
