import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./crawl.css";

export const metadata: Metadata = {
  title: "Site crawler | Honeycomb",
  description: "Crawl any public website and see every page, status code and title as it happens.",
};

/** Public: no session provider, no auth gate. middleware.ts does not match /crawl. */
export default function CrawlLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
