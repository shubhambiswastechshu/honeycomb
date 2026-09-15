"use client";

/**
 * The lower pane: everything the crawl knows about one URL.
 *
 * The views Screaming Frog puts under its grid: the raw details, a search
 * result preview truncated at the pixel widths the crawler measures, the
 * issues with what each means and how to fix it, inlinks, outlinks, images,
 * response headers, and what JavaScript changed when the crawl rendered it.
 *
 * A URL that is not a crawled page -- an image file, say -- still has inlinks,
 * so the pane falls back to that view instead of an error.
 */

import { useEffect, useState } from "react";
import { ExternalLink, X } from "lucide-react";
import { getUrlDetail } from "@/lib/crawlWorkspace";
import type { DetailField, UrlDetail } from "@/lib/crawlWorkspace";
import LinksTable from "@/components/crawl/LinksTable";
import { codeClass, isIndexable, text } from "@/components/crawl/format";

type View = "details" | "serp" | "issues" | "inlinks" | "outlinks" | "images" | "headers" | "javascript";

/** Roughly where a truncated search result cuts off, by character count. */
function clip(value: string, truncated: boolean, keep: number): string {
  if (!truncated || value.length <= keep) return value;
  return value.slice(0, keep).trimEnd() + " …";
}

function Fields({ fields }: { fields: DetailField[] }) {
  return (
    <dl className="ws-details">
      {fields.map(function field(f) {
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
    </dl>
  );
}

export default function UrlPane({
  jobId,
  url,
  live,
  onClose,
  onSelect,
}: {
  jobId: number;
  url: string;
  live: boolean;
  onClose: () => void;
  onSelect: (url: string) => void;
}) {
  const [detail, setDetail] = useState<UrlDetail | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("details");

  useEffect(
    function load() {
      let cancelled = false;
      setDetail(null);
      setMissing(false);
      setError(null);
      async function fetchOnce() {
        try {
          const next = await getUrlDetail(jobId, url);
          if (!cancelled) setDetail(next);
        } catch (caught) {
          if (cancelled) return;
          if (caught && typeof caught === "object" && "status" in caught && caught.status === 404) {
            setMissing(true);
          } else {
            setError(caught instanceof Error ? caught.message : "Could not load this URL.");
          }
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

  useEffect(
    function fallBack() {
      // Not a crawled page: the only thing to show is what links to it.
      if (missing) setView("inlinks");
    },
    [missing],
  );

  const views: { key: View; label: string }[] = missing
    ? [{ key: "inlinks", label: "Used on / linked from" }]
    : [
        { key: "details", label: "URL details" },
        { key: "issues", label: "Issues" + (detail ? " (" + detail.issues.length + ")" : "") },
        { key: "serp", label: "SERP snippet" },
        { key: "inlinks", label: "Inlinks" },
        { key: "outlinks", label: "Outlinks" },
        { key: "images", label: "Images" },
        { key: "headers", label: "Headers" },
        { key: "javascript", label: "JavaScript" },
      ];

  return (
    <section className="ws-pane" aria-label="URL details">
      <div className="ws-pane-head">
        <div className="ws-pane-tabs" role="tablist" aria-label="URL detail views">
          {views.map(function tabButton(v) {
            return (
              <button
                key={v.key}
                type="button"
                role="tab"
                aria-selected={view === v.key}
                className={view === v.key ? "cr-tab is-on" : "cr-tab"}
                onClick={function onClick() {
                  setView(v.key);
                }}
              >
                {v.label}
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

      <div className="ws-pane-body">
        {error ? <p className="cr-error cr-pad">{error}</p> : null}
        {!detail && !error && !missing ? <p className="cr-muted cr-pad">Loading…</p> : null}

        {view === "inlinks" && (detail || missing) ? (
          <LinksTable jobId={jobId} url={url} dir="in" onSelect={onSelect} />
        ) : null}
        {view === "outlinks" && detail ? <LinksTable jobId={jobId} url={url} dir="out" onSelect={onSelect} /> : null}
        {view === "images" && detail ? (
          <LinksTable jobId={jobId} url={url} dir="out" fixedKind="img" onSelect={onSelect} />
        ) : null}

        {detail && view === "details" ? (
          <>
            <Fields fields={detail.fields} />
            {detail.content_hash ? (
              <dl className="ws-details">
                <div className="ws-detail">
                  <dt>Content hash</dt>
                  <dd className="ws-mono">{detail.content_hash}</dd>
                </div>
              </dl>
            ) : null}
          </>
        ) : null}

        {detail && view === "serp" ? (
          <div className="ws-serp-wrap">
            <div className="ws-serp">
              <p className="ws-serp-url">{detail.serp.url}</p>
              <p className="ws-serp-title">
                {detail.serp.title ? clip(detail.serp.title, detail.serp.title_truncated, 60) : <em>No title</em>}
              </p>
              <p className="ws-serp-desc">
                {detail.serp.meta ? (
                  clip(detail.serp.meta, detail.serp.meta_truncated, 155)
                ) : (
                  <em>No meta description — Google will pick text from the page.</em>
                )}
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
                    <p className="ws-issue-title">
                      <span className={"ws-sev is-" + i.severity}>{i.severity}</span>
                      {i.label}
                    </p>
                    {i.why ? <p className="ws-issue-why">{i.why}</p> : null}
                    {i.fix ? (
                      <p className="ws-issue-fix">
                        <strong>How to fix:</strong> {i.fix}
                      </p>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )
        ) : null}

        {detail && view === "headers" ? (
          Object.keys(detail.headers).length === 0 ? (
            <p className="cr-muted cr-pad">
              No response headers stored. Headers are recorded on crawls started after this feature went live.
            </p>
          ) : (
            <dl className="ws-details ws-headers">
              {Object.entries(detail.headers)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([name, value]) => (
                  <div key={name} className="ws-detail">
                    <dt className="ws-mono">{name}</dt>
                    <dd className="ws-mono">{value}</dd>
                  </div>
                ))}
            </dl>
          )
        ) : null}

        {detail && view === "javascript" ? (
          detail.javascript ? (
            <Fields fields={detail.javascript} />
          ) : (
            <p className="cr-muted cr-pad">
              This crawl did not render JavaScript. Start a crawl with <strong>Render JavaScript</strong> turned on in
              Settings to compare the raw HTML with what a browser shows.
            </p>
          )
        ) : null}
      </div>
    </section>
  );
}
