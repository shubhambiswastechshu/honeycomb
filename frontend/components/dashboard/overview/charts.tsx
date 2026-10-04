"use client";

/**
 * The Overview's charts, drawn by hand in SVG and plain HTML.
 *
 * No chart library: the product's rule is plain CSS and lucide-react only, and
 * the three shapes needed here are small enough to own outright.
 *
 *   StackedColumns  calls over time, successful calls under failed ones
 *   ShareBars       one row per connector or tool, its calls split the same way
 *   ValueBars       one row per item, a single measure (response time)
 *
 * COLOUR. Successful calls are blue and failed calls red. The rest of the
 * product draws success in green, but green beside red is the one pairing a
 * red-green colour-blind reader -- about one man in twelve -- cannot separate,
 * and these charts exist to make failures stand out. The pair was run through
 * the palette validator: colour-blind separation 25.6 against a target of 8,
 * and both clear 3:1 against the surface. Values and labels are always in ink,
 * never in the series colour, and every chart with two series has a legend, so
 * colour is never the only way to tell them apart.
 *
 * Every chart carries a "Show the numbers" table, so nothing is reachable only
 * by hovering, and every mark is focusable with the same readout on focus as on
 * hover.
 */

import { useEffect, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";

/** The two series. Kept here so every chart on the page agrees. */
export const SERIES_OK = "Succeeded";
export const SERIES_FAILED = "Failed";

/* ------------------------------------------------------------------ */
/* Layout helpers                                                      */
/* ------------------------------------------------------------------ */

/**
 * The rendered width of an element, kept current as the layout changes.
 *
 * The SVG is drawn at its real pixel width rather than scaled through a
 * viewBox: scaling stretches the text and turns 4px rounded ends into ovals.
 * Zero until the first measurement, which the callers treat as "not yet".
 */
function useWidth<T extends HTMLElement>(): [RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState<number>(0);

  useEffect(function observe() {
    const node = ref.current;
    if (node === null) {
      return;
    }
    setWidth(Math.floor(node.getBoundingClientRect().width));
    if (typeof ResizeObserver === "undefined") {
      return;
    }
    const watcher = new ResizeObserver(function resized(entries) {
      const box = entries[0];
      if (box !== undefined) {
        setWidth(Math.floor(box.contentRect.width));
      }
    });
    watcher.observe(node);
    return function stop() {
      watcher.disconnect();
    };
  }, []);

  return [ref, width];
}

/**
 * A rectangle with 4px rounded corners on top and a square base: the data end
 * of a column is soft, the end that sits on the baseline is not.
 */
function roundedTop(x: number, y: number, w: number, h: number): string {
  const r = Math.max(0, Math.min(4, w / 2, h));
  return [
    "M", x, y + h,
    "L", x, y + r,
    "Q", x, y, x + r, y,
    "L", x + w - r, y,
    "Q", x + w, y, x + w, y + r,
    "L", x + w, y + h,
    "Z",
  ].join(" ");
}

function thousands(n: number): string {
  return n.toLocaleString();
}

/**
 * A clean y axis for a count: a step of 1, 2 or 5 times a power of ten, and a
 * top that is a whole number of steps at or above the peak. A peak of 66 gives
 * 0/20/40/60/80 rather than 0/17/34/51/68 -- every tick is a number a reader
 * would say out loud. Never a fractional step, because these are counts.
 */
export function niceScale(peak: number): { top: number; step: number } {
  if (peak <= 0) {
    return { top: 4, step: 1 };
  }
  const raw = peak / 4;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const factors = [1, 2, 5, 10];
  let step = 1;
  for (let i = 0; i < factors.length; i++) {
    const candidate = factors[i] * magnitude;
    if (candidate >= raw) {
      step = candidate;
      break;
    }
  }
  step = Math.max(1, Math.round(step));
  return { top: Math.ceil(peak / step) * step, step: step };
}

/** The rough rendered width of an 11px axis label, for spacing them. */
function labelWidth(text: string): number {
  return text.length * 6.4 + 10;
}

/**
 * Space under a bar's tip for its value. Bars are scaled into what is left,
 * so the longest bar still has room for "1,234 · 100% failed" and every bar
 * keeps its true proportion to the others.
 */
const TIP_ROOM = "8rem";

function barWidth(fraction: number): string {
  return "calc((100% - " + TIP_ROOM + ") * " + String(Math.max(0, fraction)) + ")";
}

/* ------------------------------------------------------------------ */
/* Pieces every chart shares                                           */
/* ------------------------------------------------------------------ */

/** A legend for the two series. Rect keys, because the marks are bars. */
export function CallsLegend() {
  return (
    <ul className="ovc-legend" aria-label="Legend">
      <li>
        <span className="ovc-key ovc-key-ok" aria-hidden="true" />
        {SERIES_OK}
      </li>
      <li>
        <span className="ovc-key ovc-key-bad" aria-hidden="true" />
        {SERIES_FAILED}
      </li>
    </ul>
  );
}

/** The table behind a chart. Collapsed, so it costs nothing until it is wanted. */
function NumbersTable({
  caption,
  head,
  rows,
}: {
  caption: string;
  head: string[];
  rows: (string | number)[][];
}) {
  return (
    <details className="ovc-table">
      <summary>Show the numbers</summary>
      <div className="ovc-table-scroll">
        <table>
          <caption className="dash-visually-hidden">{caption}</caption>
          <thead>
            <tr>
              {head.map(function th(cell, i) {
                return (
                  <th key={i} scope="col">
                    {cell}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map(function tr(row, r) {
              return (
                <tr key={r}>
                  {row.map(function td(cell, c) {
                    return c === 0 ? (
                      <th key={c} scope="row">
                        {cell}
                      </th>
                    ) : (
                      <td key={c}>{typeof cell === "number" ? thousands(cell) : cell}</td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/* ------------------------------------------------------------------ */
/* StackedColumns                                                      */
/* ------------------------------------------------------------------ */

export interface ColumnPoint {
  /** Stable key, and what the table's first column shows. */
  key: string;
  /** Short label under the axis, e.g. "14:00" or "3 Oct". */
  label: string;
  /** Long label for the tooltip and the table, e.g. "Fri, 3 Oct". */
  full: string;
  ok: number;
  failed: number;
}

const COL_TOP = 10;
const COL_BOTTOM = 24;
const COL_LEFT = 40;
const COL_RIGHT = 6;
const COL_GAP = 2;
const COL_MAX_BAR = 24;

/**
 * Calls over time: one column per hour or per day, successful calls on the
 * baseline and failed calls stacked above them with a 2px gap.
 */
export function StackedColumns({
  points,
  height,
  caption,
  firstColumn,
}: {
  points: ColumnPoint[];
  height: number;
  caption: string;
  /** Heading of the table's first column, e.g. "Hour" or "Day". */
  firstColumn: string;
}) {
  const [box, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const peak = points.reduce(function biggest(max, p) {
    return Math.max(max, p.ok + p.failed);
  }, 0);
  const scale = niceScale(peak);
  const top = scale.top;
  const ticks: number[] = [];
  for (let t = 0; t <= top; t += scale.step) {
    ticks.push(t);
  }

  const innerW = Math.max(0, width - COL_LEFT - COL_RIGHT);
  const innerH = height - COL_TOP - COL_BOTTOM;
  const band = points.length > 0 ? innerW / points.length : 0;
  const bar = Math.max(2, Math.min(COL_MAX_BAR, band * 0.66));
  const baseline = COL_TOP + innerH;

  function y(value: number): number {
    return baseline - (top > 0 ? (value / top) * innerH : 0);
  }

  // Thin the axis labels so they never collide: spaced by the widest label,
  // and counted back from the last column so "now" is always labelled and the
  // final two can never overprint each other.
  const widest = points.reduce(function longest(max, p) {
    return Math.max(max, labelWidth(p.label));
  }, 0);
  const every = band > 0 ? Math.max(1, Math.ceil(widest / band)) : 1;

  const active = hover === null ? null : points[hover];
  const activeX =
    hover === null ? 0 : COL_LEFT + band * hover + band / 2;

  return (
    <div className="ovc-chart">
      <div className="ovc-plot" ref={box} style={{ height: String(height) + "px" }}>
        {width > 0 ? (
          <svg
            width={width}
            height={height}
            role="img"
            aria-label={caption}
            className="ovc-svg"
          >
            {/* Gridlines and their values. Hairline, solid, recessive. */}
            {ticks.map(function grid(t) {
              return (
                <g key={t}>
                  <line
                    className="ovc-grid"
                    x1={COL_LEFT}
                    x2={width - COL_RIGHT}
                    y1={y(t)}
                    y2={y(t)}
                  />
                  <text className="ovc-tick" x={COL_LEFT - 8} y={y(t) + 4} textAnchor="end">
                    {thousands(t)}
                  </text>
                </g>
              );
            })}

            {points.map(function column(p, i) {
              const cx = COL_LEFT + band * i + band / 2;
              const x = cx - bar / 2;
              const hOk = p.ok > 0 ? Math.max(1.5, baseline - y(p.ok)) : 0;
              const hBad = p.failed > 0 ? Math.max(1.5, baseline - y(p.failed)) : 0;
              const gap = hOk > 0 && hBad > 0 ? COL_GAP : 0;
              const okTop = baseline - hOk;
              const badTop = okTop - gap - hBad;
              const total = p.ok + p.failed;
              const showLabel = (points.length - 1 - i) % every === 0;
              return (
                <g
                  key={p.key}
                  className={hover === i ? "ovc-col is-hover" : "ovc-col"}
                  tabIndex={0}
                  role="img"
                  aria-label={
                    p.full +
                    ": " +
                    thousands(total) +
                    (total === 1 ? " call" : " calls") +
                    (p.failed > 0 ? ", " + thousands(p.failed) + " failed" : "")
                  }
                  onPointerEnter={function enter() {
                    setHover(i);
                  }}
                  onPointerLeave={function leave() {
                    setHover(null);
                  }}
                  onFocus={function focus() {
                    setHover(i);
                  }}
                  onBlur={function blur() {
                    setHover(null);
                  }}
                >
                  {/* The hit target is the whole band, not the painted bar. */}
                  <rect
                    className="ovc-hit"
                    x={COL_LEFT + band * i}
                    y={COL_TOP}
                    width={band}
                    height={innerH}
                  />
                  {hOk > 0 ? (
                    hBad > 0 ? (
                      <rect className="ovc-ok" x={x} y={okTop} width={bar} height={hOk} />
                    ) : (
                      <path className="ovc-ok" d={roundedTop(x, okTop, bar, hOk)} />
                    )
                  ) : null}
                  {hBad > 0 ? (
                    <path className="ovc-bad" d={roundedTop(x, badTop, bar, hBad)} />
                  ) : null}
                  {showLabel ? (
                    <text className="ovc-tick" x={cx} y={height - 6} textAnchor="middle">
                      {p.label}
                    </text>
                  ) : null}
                </g>
              );
            })}

            <line
              className="ovc-baseline"
              x1={COL_LEFT}
              x2={width - COL_RIGHT}
              y1={baseline}
              y2={baseline}
            />
          </svg>
        ) : null}

        {active !== null ? (
          <div
            className={
              activeX > width * 0.66 ? "ovc-tip is-left" : "ovc-tip"
            }
            style={{ left: String(activeX) + "px", top: String(COL_TOP) + "px" }}
            role="presentation"
          >
            <p className="ovc-tip-value">
              {thousands(active.ok + active.failed)}
              <span>{active.ok + active.failed === 1 ? " call" : " calls"}</span>
            </p>
            <p className="ovc-tip-row">
              <span className="ovc-line ovc-line-ok" aria-hidden="true" />
              <strong>{thousands(active.ok)}</strong> {SERIES_OK.toLowerCase()}
            </p>
            <p className="ovc-tip-row">
              <span className="ovc-line ovc-line-bad" aria-hidden="true" />
              <strong>{thousands(active.failed)}</strong> {SERIES_FAILED.toLowerCase()}
            </p>
            <p className="ovc-tip-label">{active.full}</p>
          </div>
        ) : null}
      </div>

      <NumbersTable
        caption={caption}
        head={[firstColumn, SERIES_OK, SERIES_FAILED, "Total"]}
        rows={points.map(function row(p) {
          return [p.full, p.ok, p.failed, p.ok + p.failed];
        })}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* ShareBars                                                           */
/* ------------------------------------------------------------------ */

export interface ShareRow {
  key: string;
  /** What the row is, as text. Rendered as text, never as markup. */
  label: string;
  /** Something to draw before the label, e.g. the connector's mark. */
  mark?: ReactNode;
  /** A smaller line under the label, e.g. the connector a tool belongs to. */
  sub?: string;
  ok: number;
  failed: number;
}

/**
 * One horizontal bar per row, scaled to the busiest row, successful calls from
 * the left and failed calls after them. The value sits at the tip in ink, so
 * the bar carries the magnitude and the text carries the number.
 */
export function ShareBars({ rows, caption }: { rows: ShareRow[]; caption: string }) {
  const peak = rows.reduce(function biggest(max, r) {
    return Math.max(max, r.ok + r.failed);
  }, 0);

  return (
    <div className="ovc-chart">
      <ul className="ovc-bars" aria-label={caption}>
        {rows.map(function bar(r) {
          const total = r.ok + r.failed;
          const okShare = peak > 0 ? r.ok / peak : 0;
          const badShare = peak > 0 ? r.failed / peak : 0;
          const failRate = total > 0 ? Math.round((r.failed / total) * 100) : 0;
          return (
            <li
              key={r.key}
              className="ovc-bar-row"
              tabIndex={0}
              aria-label={
                r.label +
                ": " +
                thousands(total) +
                (total === 1 ? " call" : " calls") +
                (r.failed > 0
                  ? ", " + thousands(r.failed) + " failed (" + String(failRate) + "%)"
                  : ", none failed")
              }
            >
              <div className="ovc-bar-name">
                {r.mark}
                <span className="ovc-bar-text">
                  <span className="ovc-bar-label" title={r.label}>{r.label}</span>
                  {r.sub !== undefined ? <span className="ovc-bar-sub">{r.sub}</span> : null}
                </span>
              </div>
              <div className="ovc-bar-track">
                {r.ok > 0 ? (
                  <span
                    className={r.failed > 0 ? "ovc-seg ovc-ok-bg" : "ovc-seg ovc-ok-bg is-end"}
                    style={{ width: barWidth(okShare) }}
                  />
                ) : null}
                {r.failed > 0 ? (
                  <span className="ovc-seg ovc-bad-bg is-end" style={{ width: barWidth(badShare) }} />
                ) : null}
                <span className="ovc-bar-value">
                  {thousands(total)}
                  {r.failed > 0 ? (
                    <span className="ovc-bar-fail">{" · " + String(failRate) + "% failed"}</span>
                  ) : null}
                </span>
              </div>
            </li>
          );
        })}
      </ul>

      <NumbersTable
        caption={caption}
        head={["Name", SERIES_OK, SERIES_FAILED, "Total", "Failure rate"]}
        rows={rows.map(function row(r) {
          const total = r.ok + r.failed;
          return [
            r.sub !== undefined ? r.label + " (" + r.sub + ")" : r.label,
            r.ok,
            r.failed,
            total,
            total > 0 ? String(Math.round((r.failed / total) * 100)) + "%" : "—",
          ];
        })}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* ValueBars                                                           */
/* ------------------------------------------------------------------ */

export interface ValueRow {
  key: string;
  label: string;
  mark?: ReactNode;
  sub?: string;
  value: number;
  /** The value as a person reads it, e.g. "1.4 s". */
  display: string;
}

/**
 * One measure per row: a single series, so it needs no legend -- the section
 * title already says what is being measured.
 */
export function ValueBars({
  rows,
  caption,
  measure,
}: {
  rows: ValueRow[];
  caption: string;
  /** The measure's name, for the table heading, e.g. "Average response". */
  measure: string;
}) {
  const peak = rows.reduce(function biggest(max, r) {
    return Math.max(max, r.value);
  }, 0);

  return (
    <div className="ovc-chart">
      <ul className="ovc-bars" aria-label={caption}>
        {rows.map(function bar(r) {
          const share = peak > 0 ? r.value / peak : 0;
          return (
            <li
              key={r.key}
              className="ovc-bar-row"
              tabIndex={0}
              aria-label={r.label + ": " + r.display}
            >
              <div className="ovc-bar-name">
                {r.mark}
                <span className="ovc-bar-text">
                  <span className="ovc-bar-label" title={r.label}>{r.label}</span>
                  {r.sub !== undefined ? <span className="ovc-bar-sub">{r.sub}</span> : null}
                </span>
              </div>
              <div className="ovc-bar-track">
                <span className="ovc-seg ovc-measure-bg is-end" style={{ width: barWidth(share) }} />
                <span className="ovc-bar-value">{r.display}</span>
              </div>
            </li>
          );
        })}
      </ul>

      <NumbersTable
        caption={caption}
        head={["Name", measure]}
        rows={rows.map(function row(r) {
          return [r.sub !== undefined ? r.label + " (" + r.sub + ")" : r.label, r.display];
        })}
      />
    </div>
  );
}
