"use client";

/**
 * Inlinks, outlinks and images for one URL, read from the crawl's link graph.
 *
 * One component for all three because they are the same question asked from
 * different ends: "which edges touch this URL". Images are outlinks of kind
 * img, with the alt text where anchor text would be.
 */

import { useEffect, useState } from "react";
import { getLinks } from "@/lib/crawlWorkspace";
import type { LinkDirection, LinksResponse } from "@/lib/crawlWorkspace";
import { codeClass, num, pathOf } from "@/components/crawl/format";

const STEP = 100;

const KIND_LABEL: Record<string, string> = {
  a: "Hyperlinks",
  img: "Images",
  canonical: "Canonicals",
  hreflang: "Hreflang",
  iframe: "Iframes",
  stylesheet: "Stylesheets",
  script: "Scripts",
};

export default function LinksTable({
  jobId,
  url,
  dir,
  fixedKind,
  onSelect,
}: {
  jobId: number;
  url: string;
  dir: LinkDirection;
  /** Lock the view to one kind (the Images view) and hide the kind chips. */
  fixedKind?: string;
  onSelect: (url: string) => void;
}) {
  const [kind, setKind] = useState(fixedKind || "");
  const [limit, setLimit] = useState(STEP);
  const [data, setData] = useState<LinksResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(
    function reset() {
      setKind(fixedKind || "");
      setLimit(STEP);
    },
    [url, dir, fixedKind],
  );

  useEffect(
    function load() {
      let cancelled = false;
      setError(null);
      getLinks(jobId, url, dir, kind, limit)
        .then((next) => {
          if (!cancelled) setData(next);
        })
        .catch((caught) => {
          if (!cancelled) setError(caught instanceof Error ? caught.message : "Could not load links.");
        });
      return function stop() {
        cancelled = true;
      };
    },
    [jobId, url, dir, kind, limit],
  );

  if (error) return <p className="cr-error cr-pad">{error}</p>;
  if (!data) return <p className="cr-muted cr-pad">Loading…</p>;

  const images = (fixedKind || kind) === "img";
  if (!data.links_stored) {
    return (
      <p className="cr-muted cr-pad">
        This crawl has no link data. Links are recorded on crawls started after this feature went live, so crawl the
        site again to see them.
      </p>
    );
  }

  return (
    <div className="ws-links">
      {!fixedKind && Object.keys(data.kinds).length > 1 ? (
        <div className="ws-chips" role="group" aria-label="Link type">
          <button type="button" className={kind === "" ? "ws-chip is-on" : "ws-chip"} onClick={() => setKind("")}>
            All <span>{num(Object.values(data.kinds).reduce((a, b) => a + b, 0))}</span>
          </button>
          {Object.entries(data.kinds).map(([k, n]) => (
            <button
              key={k}
              type="button"
              className={kind === k ? "ws-chip is-on" : "ws-chip"}
              onClick={() => setKind(k)}
            >
              {KIND_LABEL[k] || k} <span>{num(n)}</span>
            </button>
          ))}
        </div>
      ) : null}

      {data.rows.length === 0 ? (
        <p className="cr-muted cr-pad">
          {dir === "in" ? "Nothing in this crawl links here." : images ? "No images on this page." : "No links found."}
        </p>
      ) : (
        <div className="ws-links-wrap">
          <table className="ws-links-table">
            <thead>
              <tr>
                <th scope="col">{dir === "in" ? "From" : images ? "Image" : "To"}</th>
                <th scope="col">Status</th>
                <th scope="col">{images ? "Alt text" : "Anchor text"}</th>
                {images ? null : <th scope="col">Type</th>}
                <th scope="col">Rel</th>
                <th scope="col">Site</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map(function row(r, i) {
                return (
                  <tr key={r.url + i}>
                    <td className="ws-cell-url">
                      {r.url ? (
                        <button type="button" className="ws-linkbtn" title={r.url} onClick={() => onSelect(r.url)}>
                          {r.internal ? pathOf(r.url) : r.url}
                        </button>
                      ) : (
                        <span className="cr-muted">—</span>
                      )}
                    </td>
                    <td>
                      <span className={codeClass(r.status_code)}>{r.status_code ?? "—"}</span>
                    </td>
                    <td className="ws-cell-text" title={r.anchor || ""}>
                      {r.anchor === null ? (
                        <span className="ws-flag">{images ? "Missing" : "—"}</span>
                      ) : r.anchor === "" ? (
                        <span className="cr-muted">{images ? "Empty (decorative)" : "No text"}</span>
                      ) : (
                        r.anchor
                      )}
                    </td>
                    {images ? null : <td>{KIND_LABEL[r.kind] || r.kind}</td>}
                    <td>{r.rel || <span className="cr-muted">—</span>}</td>
                    <td>{r.internal ? "Internal" : "External"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {data.total > data.rows.length ? (
        <div className="cr-more">
          <span className="cr-muted">
            Showing {num(data.rows.length)} of {num(data.total)}
          </span>
          {limit < 200 ? (
            <button type="button" className="cr-btn cr-btn-ghost" onClick={() => setLimit(200)}>
              Show more
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
