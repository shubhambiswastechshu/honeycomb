"use client";

/**
 * The sitemap, checked against the crawl.
 *
 * Three lists, one at a time, because they are three different jobs: pages the
 * sitemap promises but nothing links to, pages the crawl found that the sitemap
 * forgot, and sitemap entries that no longer return an indexable 200.
 *
 * The sitemap is read live when this opens, so it can be newer than the crawl.
 * That is why "Look again" exists and why the check time is on show.
 */

import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { getSitemapAudit } from "@/lib/crawlWorkspace";
import type { SitemapAudit as Audit } from "@/lib/crawlWorkspace";
import { num, pathOf } from "@/components/crawl/format";

type View = "missing_from_crawl" | "missing_from_sitemap" | "problems";

const VIEWS: { key: View; label: string; blurb: string; empty: string }[] = [
  {
    key: "missing_from_crawl",
    label: "Orphans",
    blurb: "In the sitemap, but the crawl never reached them. Nothing on the site links here.",
    empty: "Every sitemap URL was reached by the crawl.",
  },
  {
    key: "missing_from_sitemap",
    label: "Not listed",
    blurb: "Crawled, indexable and returning 200, but absent from the sitemap.",
    empty: "Every indexable page is in the sitemap.",
  },
  {
    key: "problems",
    label: "Broken promises",
    blurb: "Listed in the sitemap, but broken, redirecting or noindexed.",
    empty: "Every sitemap URL returns an indexable 200.",
  },
];

export default function SitemapAudit({ jobId }: { jobId: number }) {
  const [audit, setAudit] = useState<Audit | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<View>("missing_from_crawl");

  const load = useCallback(
    function load(refresh: boolean) {
      setBusy(true);
      setError(null);
      return getSitemapAudit(jobId, refresh)
        .then(function got(next) {
          setAudit(next);
        })
        .catch(function failed(caught: unknown) {
          setError(caught instanceof Error ? caught.message : "The sitemap could not be read.");
        })
        .then(function done() {
          setBusy(false);
        });
    },
    [jobId],
  );

  useEffect(
    function first() {
      void load(false);
    },
    [load],
  );

  if (audit === null) {
    return error !== null ? <p className="cr-error">{error}</p> : <p className="ins-empty">Reading the sitemap…</p>;
  }

  const counts = audit.counts;
  const chosen = VIEWS.find((v) => v.key === view) as (typeof VIEWS)[number];
  const rows: { url: string; note: string }[] =
    view === "problems"
      ? audit.problems.map((p) => ({ url: p.url, note: p.problem + (p.detail ? " · " + p.detail : "") }))
      : audit[view].map((url) => ({ url: url, note: "" }));
  const total = counts[view];

  return (
    <div className="sma">
      {audit.files.length === 0 ? (
        <p className="ins-empty">No sitemap found at robots.txt, /sitemap.xml or /sitemap_index.xml.</p>
      ) : (
        <p className="ins-note">
          {num(counts.in_sitemap)} URLs across {audit.files.length}{" "}
          {audit.files.length === 1 ? "file" : "files"} · {num(counts.in_both)} also crawled
        </p>
      )}

      <div className="sma-tabs" role="tablist">
        {VIEWS.map(function tab(v) {
          const n = counts[v.key];
          return (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={view === v.key}
              className={view === v.key ? "sma-tab is-on" : "sma-tab"}
              onClick={function pick() {
                setView(v.key);
              }}
            >
              <span>{v.label}</span>
              <span className={n > 0 ? "sma-n is-hot" : "sma-n"}>{num(n)}</span>
            </button>
          );
        })}
      </div>

      <p className="ins-note sma-blurb">{chosen.blurb}</p>

      {rows.length === 0 ? (
        <p className="ins-empty">{chosen.empty}</p>
      ) : (
        <ul className="sma-list">
          {rows.map(function row(r) {
            return (
              <li key={r.url} className="sma-item">
                <a href={r.url} target="_blank" rel="noreferrer noopener" title={r.url}>
                  {pathOf(r.url)}
                </a>
                {r.note ? <span className="sma-note">{r.note}</span> : null}
              </li>
            );
          })}
        </ul>
      )}
      {total > rows.length ? (
        <p className="ins-note">
          Showing the first {num(rows.length)} of {num(total)}.
        </p>
      ) : null}

      {audit.errors.map(function problem(message) {
        return (
          <p key={message} className="ins-note sma-warn">
            {message}
          </p>
        );
      })}

      <button type="button" className="cr-btn cr-btn-ghost ins-tool" disabled={busy} onClick={() => void load(true)}>
        <RefreshCw size={14} className={busy ? "sma-spin" : undefined} aria-hidden="true" />
        {busy ? "Reading…" : "Look again"}
      </button>
      {error !== null ? <p className="cr-error">{error}</p> : null}
    </div>
  );
}
