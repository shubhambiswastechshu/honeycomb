"use client";

/**
 * The small pieces every section of the Google Ads report is built from, so
 * that thirty sections read as one product: a card with a title and a note, a
 * slot that shows a skeleton, an error with a retry, or the data; a table you
 * can sort, search and export; a segmented control; a delta chip; a sparkline.
 *
 * Nothing here knows about Google Ads. It knows about a loading state, an
 * error, and rows.
 */

import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { ArrowDown, ArrowUp, Download, RefreshCw, Search } from "lucide-react";
import { downloadCsv, fmtDelta, toCsv, verdict } from "@/components/reports/google-ads/ads-model";
import type { Delta, KpiDef } from "@/components/reports/google-ads/ads-model";
import type { Slice } from "@/components/reports/google-ads/useReportPack";

/* ------------------------------------------------------------------ */
/* Card and slot                                                       */
/* ------------------------------------------------------------------ */

export function Card({
  title,
  note,
  actions,
  children,
  wide,
}: {
  title: string;
  note?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  /** Spans both columns of the two-column grid. */
  wide?: boolean;
}) {
  return (
    <section className={wide === true ? "ga-card is-wide" : "ga-card"}>
      <header className="ga-card-head">
        <div className="ga-card-titles">
          <h3 className="ga-card-title">{title}</h3>
          {note !== undefined ? <p className="ga-card-note">{note}</p> : null}
        </div>
        {actions !== undefined ? <div className="ga-card-actions">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}

export function Skeleton({ height }: { height: number }) {
  return <div className="ga-skeleton" style={{ height: height }} aria-hidden="true" />;
}

/** Turns an error into something a person can act on where one is known. */
function hintFor(message: string): string | null {
  if (message.indexOf("switched off") !== -1) {
    return "Turn this tool back on in the Tools tab to see this section.";
  }
  if (message.indexOf("took longer") !== -1) {
    return "Google was slow to answer. A shorter date range is faster.";
  }
  return null;
}

/**
 * One section's data: a skeleton while it loads, an error with a retry when it
 * failed, and the content when it did not. While a refresh of the same view is
 * running the last answer stays on screen, dimmed.
 */
export function Slot({
  slice,
  onRetry,
  height = 140,
  children,
}: {
  slice: Slice;
  onRetry: () => void;
  height?: number;
  children: (data: unknown) => ReactNode;
}) {
  if (slice.status === "loading") {
    return <Skeleton height={height} />;
  }
  if (slice.status === "error") {
    const message = slice.error !== null ? slice.error : "This section could not be loaded.";
    const hint = hintFor(message);
    return (
      <div className="ga-error" role="alert">
        <p className="ga-error-text">{message}</p>
        {hint !== null ? <p className="ga-error-hint">{hint}</p> : null}
        <button type="button" className="ga-btn" onClick={onRetry}>
          <RefreshCw size={13} strokeWidth={2} aria-hidden="true" />
          <span>Try again</span>
        </button>
      </div>
    );
  }
  return (
    <div className={slice.stale === true ? "ga-stale" : undefined} aria-busy={slice.stale === true}>
      {children(slice.data)}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="ga-empty">{children}</p>;
}

/* ------------------------------------------------------------------ */
/* Controls                                                            */
/* ------------------------------------------------------------------ */

export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
  small,
}: {
  label: string;
  options: Array<{ id: T; label: string; disabled?: boolean }>;
  value: T;
  onChange: (next: T) => void;
  small?: boolean;
}) {
  return (
    <div className={small === true ? "ga-seg is-small" : "ga-seg"} role="group" aria-label={label}>
      {options.map(function each(option) {
        return (
          <button
            key={option.id}
            type="button"
            className="ga-seg-btn"
            aria-pressed={value === option.id}
            disabled={option.disabled === true}
            onClick={function pick() {
              onChange(option.id);
            }}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Small marks                                                         */
/* ------------------------------------------------------------------ */

export function DeltaChip({ delta, good }: { delta: Delta | null; good: KpiDef["good"] }) {
  if (delta === null) {
    return null;
  }
  const outcome = verdict(delta, good);
  const tone = outcome === "good" ? " is-good" : outcome === "bad" ? " is-bad" : "";
  const arrow = delta.direction === "up" ? "▲" : delta.direction === "down" ? "▼" : "–";
  return (
    <span
      className={"ga-delta" + tone}
      title={outcome === "good" ? "An improvement" : outcome === "bad" ? "A step back" : "Compared with the previous period"}
    >
      <span aria-hidden="true">{arrow}</span> {fmtDelta(delta)}
    </span>
  );
}

/** A tiny trend line. Gaps (null) break the line; fewer than two points draws nothing. */
export function Sparkline({
  values,
  width = 96,
  height = 30,
}: {
  values: Array<number | null>;
  width?: number;
  height?: number;
}) {
  const real = values.filter(function has(v): v is number {
    return v !== null;
  });
  if (real.length < 2) {
    return <span className="ga-spark-none" aria-hidden="true" />;
  }
  const lo = Math.min.apply(null, real);
  const hi = Math.max.apply(null, real);
  const span = hi - lo === 0 ? 1 : hi - lo;
  const pad = 2;
  const x = function x(i: number): number {
    return pad + (i * (width - pad * 2)) / Math.max(values.length - 1, 1);
  };
  const y = function y(v: number): number {
    return height - pad - ((v - lo) / span) * (height - pad * 2);
  };
  let path = "";
  let open = false;
  values.forEach(function point(v, i) {
    if (v === null) {
      open = false;
      return;
    }
    path += (open ? " L" : " M") + x(i).toFixed(1) + "," + y(v).toFixed(1);
    open = true;
  });
  return (
    <svg className="ga-spark" width={width} height={height} viewBox={"0 0 " + String(width) + " " + String(height)} aria-hidden="true">
      <path d={path} />
    </svg>
  );
}

/** A thin bar showing a share of a whole. */
export function ShareBar({
  value,
  max,
  tone,
}: {
  value: number;
  max: number;
  tone?: "blue" | "green" | "amber" | "red";
}) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <span className="ga-share" aria-hidden="true">
      <span
        className={tone !== undefined ? "ga-share-fill is-" + tone : "ga-share-fill"}
        style={{ width: String(pct) + "%" }}
      />
    </span>
  );
}

export function Pill({
  tone,
  children,
}: {
  tone: "good" | "bad" | "warn" | "muted" | "info";
  children: ReactNode;
}) {
  return <span className={"ga-pill is-" + tone}>{children}</span>;
}

/* ------------------------------------------------------------------ */
/* Table                                                               */
/* ------------------------------------------------------------------ */

export interface Column<T> {
  id: string;
  label: string;
  align?: "left" | "right";
  /** How the column sorts. Omit it and the column is not sortable. */
  sort?: (row: T) => number | string | null;
  cell: (row: T) => ReactNode;
  /** What goes in the CSV. Falls back to the sort value. */
  csv?: (row: T) => string | number | null;
  hint?: string;
  className?: string;
}

function compare(a: number | string | null, b: number | string | null, dir: 1 | -1): number {
  if (a === null && b === null) {
    return 0;
  }
  // Empty values sink to the bottom whichever way the column is sorted.
  if (a === null) {
    return 1;
  }
  if (b === null) {
    return -1;
  }
  if (typeof a === "number" && typeof b === "number") {
    return (a - b) * dir;
  }
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" }) * dir;
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  caption,
  initialSort,
  search,
  pageSize = 15,
  exportName,
  empty = "Nothing to show for this period.",
  footer,
  filters,
  rowClass,
}: {
  rows: T[];
  columns: Array<Column<T>>;
  rowKey: (row: T) => string;
  caption: string;
  initialSort?: { id: string; dir: "asc" | "desc" };
  search?: { placeholder: string; text: (row: T) => string };
  pageSize?: number;
  exportName?: string;
  empty?: string;
  footer?: (rows: T[]) => ReactNode;
  filters?: ReactNode;
  rowClass?: (row: T) => string | undefined;
}) {
  const [sort, setSort] = useState<{ id: string; dir: "asc" | "desc" } | null>(
    initialSort !== undefined ? initialSort : null
  );
  const [query, setQuery] = useState<string>("");
  const [limit, setLimit] = useState<number>(pageSize);

  const shown = useMemo(
    function filterAndSort() {
      const needle = query.trim().toLowerCase();
      let list = rows;
      if (search !== undefined && needle.length > 0) {
        list = list.filter(function match(row) {
          return search.text(row).toLowerCase().indexOf(needle) !== -1;
        });
      }
      if (sort !== null) {
        const column = columns.filter(function is(c) {
          return c.id === sort.id;
        })[0];
        if (column !== undefined && column.sort !== undefined) {
          const by = column.sort;
          const dir = sort.dir === "asc" ? 1 : -1;
          list = list.slice().sort(function order(a, b) {
            return compare(by(a), by(b), dir);
          });
        }
      }
      return list;
    },
    [rows, columns, sort, query, search]
  );

  function toggle(column: Column<T>): void {
    if (column.sort === undefined) {
      return;
    }
    setSort(function next(current) {
      if (current !== null && current.id === column.id) {
        return { id: column.id, dir: current.dir === "asc" ? "desc" : "asc" };
      }
      // Numbers are usually wanted biggest-first; text A to Z.
      const first = column.align === "right" ? "desc" : "asc";
      return { id: column.id, dir: first };
    });
  }

  function exportCsv(): void {
    const headers = columns.map(function head(c) {
      return c.label;
    });
    const body = shown.map(function line(row) {
      return columns.map(function value(c) {
        if (c.csv !== undefined) {
          return c.csv(row);
        }
        return c.sort !== undefined ? c.sort(row) : null;
      });
    });
    downloadCsv((exportName !== undefined ? exportName : "report") + ".csv", toCsv(headers, body));
  }

  const visible = shown.slice(0, limit);
  const hasToolbar = search !== undefined || exportName !== undefined || filters !== undefined;

  return (
    <div className="ga-table">
      {hasToolbar ? (
        <div className="ga-table-bar">
          {search !== undefined ? (
            <label className="ga-table-search">
              <Search size={14} strokeWidth={2} aria-hidden="true" />
              <input
                type="search"
                value={query}
                placeholder={search.placeholder}
                aria-label={search.placeholder}
                onChange={function onChange(event) {
                  setQuery(event.target.value);
                  setLimit(pageSize);
                }}
              />
            </label>
          ) : null}
          {filters !== undefined ? <div className="ga-table-filters">{filters}</div> : null}
          {exportName !== undefined ? (
            <button
              type="button"
              className="ga-btn is-quiet"
              onClick={exportCsv}
              disabled={shown.length === 0}
              title="Download what is shown as a CSV file"
            >
              <Download size={13} strokeWidth={2} aria-hidden="true" />
              <span>Export CSV</span>
            </button>
          ) : null}
        </div>
      ) : null}

      {rows.length === 0 ? (
        <Empty>{empty}</Empty>
      ) : shown.length === 0 ? (
        <Empty>Nothing matches that search.</Empty>
      ) : (
        <div className="ga-table-wrap" role="region" aria-label={caption} tabIndex={0}>
          <table>
            <caption className="ga-sr">{caption}</caption>
            <thead>
              <tr>
                {columns.map(function head(column) {
                  const active = sort !== null && sort.id === column.id;
                  return (
                    <th
                      key={column.id}
                      scope="col"
                      className={(column.align === "right" ? "is-num " : "") + (column.className || "")}
                      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
                      title={column.hint}
                    >
                      {column.sort !== undefined ? (
                        <button type="button" className="ga-th-btn" onClick={function click() {
                          toggle(column);
                        }}>
                          <span>{column.label}</span>
                          {active ? (
                            sort.dir === "asc" ? (
                              <ArrowUp size={12} strokeWidth={2.4} aria-hidden="true" />
                            ) : (
                              <ArrowDown size={12} strokeWidth={2.4} aria-hidden="true" />
                            )
                          ) : null}
                        </button>
                      ) : (
                        <span>{column.label}</span>
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {visible.map(function body(row) {
                return (
                  <tr key={rowKey(row)} className={rowClass !== undefined ? rowClass(row) : undefined}>
                    {columns.map(function cell(column) {
                      return (
                        <td
                          key={column.id}
                          className={(column.align === "right" ? "is-num " : "") + (column.className || "")}
                        >
                          {column.cell(row)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
            {footer !== undefined ? <tfoot>{footer(shown)}</tfoot> : null}
          </table>
        </div>
      )}

      {shown.length > visible.length ? (
        <div className="ga-table-more">
          <span>{"Showing " + String(visible.length) + " of " + String(shown.length)}</span>
          <button type="button" className="ga-btn is-quiet" onClick={function more() {
            setLimit(limit + pageSize);
          }}>
            Show more
          </button>
          <button type="button" className="ga-btn is-quiet" onClick={function all() {
            setLimit(shown.length);
          }}>
            Show all
          </button>
        </div>
      ) : null}
    </div>
  );
}
