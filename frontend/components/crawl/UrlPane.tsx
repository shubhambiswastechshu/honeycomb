"use client";

/**
 * The lower pane: everything the crawler recorded about one URL.
 *
 * Three views, as in Screaming Frog: the raw details, a search-result preview
 * that truncates the title and description at the same pixel widths the
 * crawler measures against, and the issues on this URL by priority.
 */

import { useEffect, useState } from "react";
import { ExternalLink, X } from "lucide-react";
import { getUrlDetail } from "@/lib/crawlWorkspace";
import type { UrlDetail } from "@/lib/crawlWorkspace";
import { codeClass, isIndexable, text } from "@/components/crawl/format";

type View = "details" | "serp" | "issues";

/** Roughly where a truncated search result cuts off, by character count. */
function clip(value: string, truncated: boolean, keep: number): string {
  if (!truncated || value.length <= keep) return value;
  return value.slice(0, keep).trimEnd() + " …";
}

export default function UrlPane({
  jobId,
  url,
  live,
  onClose,
}: {
  jobId: number;
  url: string;
  live: boolean;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<UrlDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("details");

  useEffect(
    function load() {
      let cancelled = false;
      setDetail(null);
      setError(null);
      async function fetchOnce() {
        try {
          const next = await getUrlDetail(jobId, url);
          if (!cancelled) setDetail(next);
        } catch (caught) {
          if (!cancelled) setError(caught instanceof Error ? caught.message : "Could not load this URL.");
        }
      }
      void fetchOnce();
      // Analysis columns fill in after a live crawl finishes, so keep this fresh.
      const id = live ? window.setInterval(fetchOnce, 8000) : undefined;
      return function stop() {
        cancelled = true;
        if (id !== undefined) window.clearInterval(id);
      };
    },
    [jobId, url, live],
  );

  return (
    <section className="ws-pane" aria-label="URL details">
      <div className="ws-pane-head">
        <div className="ws-pane-tabs" role="tablist" aria-label="URL detail views">
          {(["details", "serp", "issues"] as View[]).map(function tabButton(v) {
            const label =
              v === "details" ? "URL details" : v === "serp" ? "SERP snippet" : "Issues" + (detail ? " (" + detail.issues.length + ")" : "");
            return (
              <button
                key={v}
                type="button"
                role="tab"
                aria-selected={view === v}
                className={view === v ? "cr-tab is-on" : "cr-tab"}
                onClick={function onClick() {
                  setView(v);
                }}
              >
                {label}
              </button>
            );
          })}
        </div>
        <span className="ws-pane-url" title={url}>
          {url}
        </span>
        <a
          className="ws-pane-close"
          href={url}
          target="_blank"
          rel="noreferrer noopener"
          aria-label="Open this URL in a new tab"
          title="Open in a new tab"
        >
          <ExternalLink size={14} aria-hidden="true" />
        </a>
        <button type="button" className="ws-pane-close" aria-label="Close URL details" onClick={onClose}>
          <X size={15} aria-hidden="true" />
        </button>
      </div>

      {error ? <p className="cr-error cr-pad">{error}</p> : null}
      {!detail && !error ? <p className="cr-muted cr-pad">Loading…</p> : null}

      {detail && view === "details" ? (
        <dl className="ws-details">
          {detail.fields.map(function field(f) {
            let shown: React.ReactNode = text(f.value, f.type) || <span className="cr-muted">—</span>;
            if (f.type === "code") shown = <span className={codeClass(f.value)}>{text(f.value, f.type) || "—"}</span>;
            if (f.type === "index")
              shown = <span className={isIndexable(f.value) ? "cr-idx is-yes" : "cr-idx"}>{text(f.value, f.type) || "—"}</span>;
            return (
              <div key={f.key} className="ws-detail">
                <dt>{f.label}</dt>
                <dd>{shown}</dd>
              </div>
            );
          })}
          {detail.content_hash ? (
            <div className="ws-detail">
              <dt>Content hash</dt>
              <dd className="ws-mono">{detail.content_hash}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      {detail && view === "serp" ? (
        <div className="ws-serp-wrap">
          <div className="ws-serp">
            <p className="ws-serp-url">{detail.serp.url}</p>
            <p className="ws-serp-title">
              {detail.serp.title ? clip(detail.serp.title, detail.serp.title_truncated, 60) : <em>No title</em>}
            </p>
            <p className="ws-serp-desc">
              {detail.serp.meta ? clip(detail.serp.meta, detail.serp.meta_truncated, 155) : <em>No meta description — Google will pick text from the page.</em>}
            </p>
          </div>
          <ul className="ws-serp-facts">
            <li className={detail.serp.title_truncated ? "is-warn" : undefined}>
              Title: {detail.serp.title_pixel_width} px of {detail.serp.title_max_px} px
              {detail.serp.title_truncated ? " — will be truncated" : ""}
            </li>
            <li className={detail.serp.meta_truncated ? "is-warn" : undefined}>
              Description: {detail.serp.meta_pixel_width} px of {detail.serp.meta_max_px} px
              {detail.serp.meta_truncated ? " — will be truncated" : ""}
            </li>
          </ul>
        </div>
      ) : null}

      {detail && view === "issues" ? (
        detail.issues.length === 0 ? (
          <p className="cr-muted cr-pad">No issues found on this URL.</p>
        ) : (
          <ul className="ws-url-issues">
            {detail.issues.map(function issue(i) {
              return (
                <li key={i.code}>
                  <span className={"ws-sev is-" + i.severity}>{i.severity}</span>
                  {i.label}
                </li>
              );
            })}
          </ul>
        )
      ) : null}
    </section>
  );
}
