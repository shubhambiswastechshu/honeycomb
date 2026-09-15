import { Suspense } from "react";
import CrawlApp from "@/components/crawl/CrawlApp";

/** Suspense because CrawlApp reads ?job= from the URL, which Next requires be suspended. */
export default function CrawlPage() {
  return (
    <Suspense fallback={null}>
      <CrawlApp />
    </Suspense>
  );
}
