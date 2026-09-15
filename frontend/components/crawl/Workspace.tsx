"use client";

/**
 * The crawler workspace: Screaming Frog's layout, over a public crawl.
 *
 * A tab per concern across the top, a filter with live counts, a sortable grid,
 * a URL details pane under it, and an insights sidebar (issues by priority,
 * overview, site structure, response times, reports, sitemap, comparison).
 *
 * The server defines every tab, column and filter, so this component renders
 * whatever it is given. Polling follows the crawl: live counts every 10 s and
 * the grid every 5 s while it runs; once finished, one load each.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Download, Search, X } from "lucide-react";
import { getGrid, getWorkspace, gridExportUrl } from "@/lib/crawlWorkspace";
import type { GridColumn, GridQuery, GridResponse, GridRow, Workspace as WorkspaceData } from "@/lib/crawlWorkspace";
import type { PublicJob } from "@/lib/crawl";
import Insights from "@/components/crawl/Insights";
import UrlPane from "@/components/crawl/UrlPane";
import { codeClass, isIndexable, num, pathOf, text } from "@/components/crawl/format";

const PAGE_STEP = 100;

export interface WorkspaceProps {
  jobId: number;
  live: boolean;
  /** Other finished public crawls of the same site, for comparison. */
  compareWith: PublicJob[];
}

function Cell({ column, row }: { column: GridColumn; row: GridRow }) {
  const value = row[column.key];
  switch (column.type) {
    case "url": {
      const url = typeof value === "string" ? value : "";
      if (!url) return <span className="cr-muted">—</span>;
      return (
        <span className="ws-url" title={url}>
          {column.key === "url" ? pathOf(url) : url}
        </span>
      );
    }
    case "code":
      return <span className={codeClass(value)}>{typeof value === "number" ? value : "—"}</span>;
    case "index":
      return (
        <span className={isIndexable(value) ? "cr-idx is-yes" : "cr-idx"}>
          {typeof value === "string" && value ? value : "—"}
        </span>
      );
    default: {
      const shown = text(value, column.type);
      return shown ? <span title={shown}>{shown}</span> : <span className="cr-muted">—</span>;
    }
  }
}

export default function Workspace({ jobId, live, compareWith }: WorkspaceProps) {
  const [data, setData] = useState<WorkspaceData | null>(null);
  const [grid, setGrid] = useState<GridResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState<GridQuery>({
    tab: "internal",
    filter: "all",
    issue: "",
    q: "",
    sort: "",
    dir: "",
    limit: PAGE_STEP,
  });
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [sideOpen, setSideOpen] = useState(true);

  const loadWorkspace = useCallback(
    async function loadWorkspace() {
      try {
        setData(await getWorkspace(jobId));
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Could not load this crawl.");
      }
    },
    [jobId],
  );

  const loadGrid = useCallback(
    async function loadGrid() {
      try {
        setGrid(await getGrid(jobId, query));
        setError(null);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Could not load pages.");
      }
    },
    [jobId, query],
  );

  useEffect(
    function pollWorkspace() {
      void loadWorkspace();
      if (!live) return;
      const id = window.setInterval(loadWorkspace, 10000);
      return function stop() {
        window.clearInterval(id);
      };
    },
    [loadWorkspace, live],
  );

  useEffect(
    function pollGrid() {
      const first = window.setTimeout(loadGrid, 200);
      const id = live ? window.setInterval(loadGrid, 5000) : undefined;
      return function stop() {
        window.clearTimeout(first);
        if (id !== undefined) window.clearInterval(id);
      };
    },
    [loadGrid, live],
  );

  // Typing into the search box is debounced into the query, so each keystroke
  // is not its own request.
  useEffect(
    function debounceSearch() {
      const id = window.setTimeout(function apply() {
        setQuery(function next(prev) {
          return prev.q === search ? prev : { ...prev, q: search, limit: PAGE_STEP };
        });
      }, 300);
      return function stop() {
        window.clearTimeout(id);
      };
    },
    [search],
  );

  const tab = useMemo(
    function currentTab() {
      return data ? data.tabs.find((t) => t.key === query.tab) || data.tabs[0] : null;
    },
    [data, query.tab],
  );

  function pickTab(key: string) {
    setQuery((prev) => ({ ...prev, tab: key, filter: "all", issue: "", sort: "", dir: "", limit: PAGE_STEP }));
  }

  function pick(tabKey: string, filterKey: string) {
    setQuery((prev) => ({ ...prev, tab: tabKey, filter: filterKey, issue: "", sort: "", dir: "", limit: PAGE_STEP }));
  }

  function pickIssue(code: string) {
    setQuery((prev) => ({ ...prev, issue: code, sort: "", dir: "", limit: PAGE_STEP }));
  }

  function sortBy(key: string) {
    setQuery((prev) => {
      const sameKey = (grid?.sort || prev.sort) === key;
      const current = grid?.dir || "asc";
      return { ...prev, sort: key, dir: sameKey && current === "asc" ? "desc" : "asc" };
    });
  }

  if (error && !data) {
    return <p className="cr-error cr-pad">{error}</p>;
  }

  return (
    <div className={sideOpen ? "ws" : "ws is-side-closed"}>
      <div className="ws-main">
        <div className="ws-tabs" role="tablist" aria-label="Crawl tabs">
          {(data ? data.tabs : []).map(function tabButton(t) {
            const total = t.filters[0] ? t.filters[0].count : 0;
            return (
              <button
                key={t.key}
                type="button"
                role="tab"
                aria-selected={query.tab === t.key && !query.issue}
                className={query.tab === t.key && !query.issue ? "ws-tab is-on" : "ws-tab"}
                onClick={function onClick() {
                  pickTab(t.key);
                }}
              >
                {t.label}
                <span className="ws-tab-n">{num(total)}</span>
              </button>
            );
          })}
        </div>

        <div className="ws-toolbar">
          {query.issue ? (
            <span className="ws-issue-chip">
              Issue: {grid?.issue_label || query.issue}
              <button
                type="button"
                aria-label="Clear issue filter"
                onClick={function clear() {
                  setQuery((prev) => ({ ...prev, issue: "", limit: PAGE_STEP }));
                }}
              >
                <X size={13} aria-hidden="true" />
              </button>
            </span>
          ) : (
            <label className="ws-filter">
              <span className="cr-sr">Filter</span>
              <select
                value={query.filter}
                onChange={function onChange(e) {
                  pick(query.tab, e.target.value);
                }}
              >
                {(tab ? tab.filters : []).map(function opt(f) {
                  return (
                    <option key={f.key} value={f.key}>
                      {f.label} ({num(f.count)})
                    </option>
                  );
                })}
              </select>
            </label>
          )}
          <label className="cr-filter ws-search">
            <Search size={14} aria-hidden="true" />
            <span className="cr-sr">Search addresses</span>
            <input
              type="search"
              placeholder="Search URLs"
              value={search}
              onChange={function onChange(e) {
                setSearch(e.target.value);
              }}
            />
          </label>
          <span className="ws-total" aria-live="polite">
            {grid ? num(grid.total) + (grid.total === 1 ? " URL" : " URLs") : ""}
          </span>
          <a className="cr-btn cr-btn-ghost" href={gridExportUrl(jobId, query)} download>
            <Download size={14} aria-hidden="true" />
            Export
          </a>
          <button
            type="button"
            className="cr-btn cr-btn-ghost ws-side-toggle"
            aria-pressed={sideOpen}
            onClick={function toggle() {
              setSideOpen((v) => !v);
            }}
          >
            {sideOpen ? "Hide insights" : "Show insights"}
          </button>
        </div>

        {error ? <p className="cr-error cr-pad">{error}</p> : null}

        <div className="ws-grid-wrap">
          <table className="ws-grid">
            <thead>
              <tr>
                <th scope="col" className="ws-rownum">
                  #
                </th>
                {(grid ? grid.columns : []).map(function head(c) {
                  const sorted = grid && grid.sort === c.key;
                  return (
                    <th
                      key={c.key}
                      scope="col"
                      className={["int", "float", "ms", "bytes", "code"].includes(c.type) ? "is-num" : undefined}
                      aria-sort={sorted ? (grid?.dir === "desc" ? "descending" : "ascending") : "none"}
                    >
                      <button
                        type="button"
                        className="ws-sort"
                        onClick={function onClick() {
                          sortBy(c.key);
                        }}
                      >
                        {c.label}
                        {sorted ? (
                          grid?.dir === "desc" ? (
                            <ArrowDown size={12} aria-hidden="true" />
                          ) : (
                            <ArrowUp size={12} aria-hidden="true" />
                          )
                        ) : null}
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {grid === null ? (
                <tr>
                  <td className="cr-table-empty" colSpan={20}>
                    Loading…
                  </td>
                </tr>
              ) : grid.rows.length === 0 ? (
                <tr>
                  <td className="cr-table-empty" colSpan={grid.columns.length + 1}>
                    {live ? "Nothing here yet. Rows appear as the crawler finds pages." : "No URLs match this filter."}
                  </td>
                </tr>
              ) : (
                grid.rows.map(function row(r, index) {
                  const url = typeof r.url === "string" ? r.url : "";
                  const isSel = url === selected;
                  return (
                    <tr
                      key={url || index}
                      className={isSel ? "is-selected" : undefined}
                      aria-selected={isSel}
                      tabIndex={0}
                      onClick={function select() {
                        setSelected(url);
                      }}
                      onKeyDown={function onKey(e) {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setSelected(url);
                        }
                      }}
                    >
                      <td className="ws-rownum">{index + 1}</td>
                      {grid.columns.map(function cell(c) {
                        return (
                          <td
                            key={c.key}
                            className={
                              ["int", "float", "ms", "bytes", "code"].includes(c.type)
                                ? "is-num"
                                : c.type === "url"
                                  ? "ws-cell-url"
                                  : "ws-cell-text"
                            }
                          >
                            <Cell column={c} row={r} />
                          </td>
                        );
                      })}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {grid && grid.total > grid.rows.length ? (
          <div className="cr-more">
            <span className="cr-muted">
              Showing {num(grid.rows.length)} of {num(grid.total)}
            </span>
            <button
              type="button"
              className="cr-btn cr-btn-ghost"
              onClick={function more() {
                setQuery((prev) => ({ ...prev, limit: Math.min(prev.limit + PAGE_STEP, 200) }));
              }}
              disabled={query.limit >= 200}
            >
              {query.limit >= 200 ? "Export for the full list" : "Load more"}
            </button>
          </div>
        ) : null}

        {selected ? (
          <UrlPane
            jobId={jobId}
            url={selected}
            live={live}
            onClose={function close() {
              setSelected(null);
            }}
          />
        ) : (
          <p className="ws-hint">Select a row to see everything the crawler found on that URL.</p>
        )}
      </div>

      {sideOpen && data ? (
        <Insights
          jobId={jobId}
          data={data}
          activeTab={query.tab}
          activeFilter={query.issue ? "" : query.filter}
          activeIssue={query.issue}
          onPick={pick}
          onIssue={pickIssue}
          compareWith={compareWith}
        />
      ) : null}
    </div>
  );
}
