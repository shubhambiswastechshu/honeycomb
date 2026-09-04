"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ActivitySummary } from "@/lib/api";

/**
 * Call volume as a field of cells: one column per day, one cell per unit of
 * calls, unlit cells showing the headroom.
 *
 * Why cells rather than a line or a bar: the quantity here is a COUNT of
 * discrete events, and a cell field says that out loud -- you can read "nine
 * calls" off a column by counting, which no smooth area can offer. It also
 * degrades honestly on a young workspace, where a line chart through three
 * points invents a trend that is not there and a field of unlit cells simply
 * looks like what it is.
 *
 * Two series, so there is a legend. Failures stack ON TOP of successes in each
 * column rather than beside them, because the column's height is the day's
 * total and splitting it would make two shorter columns that no longer read as
 * one day's volume.
 */

/** Cells in a full-height column. The scale is quantised to this. */
const ROWS = 12;

/* Cell geometry is MEASURED, not fixed. One SVG unit is one CSS pixel here, so
   the columns divide whatever width the panel gives them and the cells stay
   square at whatever size that implies. A fixed cell size would either strand
   a narrow chart in a wide panel -- the blank space this dashboard has been
   fighting -- or overflow a phone. The clamp keeps cells readable at both ends. */
const GAP = 2;
const MIN_CELL = 4;
const MAX_CELL = 14;
/** Width assumed for the first paint, before the panel has been measured. */
const FALLBACK_W = 900;

/** Room under the plot for the date labels, so the axis is never clipped. */
const AXIS_H = 20;
/** Room at the left for the value ticks. */
const AXIS_W = 26;

export interface ActivityMatrixProps {
  summary: ActivitySummary;
}

interface Column {
  label: string;
  iso: string;
  ok: number;
  error: number;
  total: number;
  okCells: number;
  errorCells: number;
}

/* Dates are formatted from fixed tables, NOT toLocaleDateString.
   An implicit locale resolves to the SERVER's on the server and the BROWSER's
   in the client, and the two disagree about punctuation -- Node rendered
   "Thursday, 6 August" where Chrome rendered "Thursday 6 August", which React
   reports as a hydration mismatch and recovers from by throwing the
   server-rendered subtree away. Naming a locale explicitly is not a fix
   either: ICU data differs between the Node build and the visitor's browser.
   Fixed tables render identically in both. */
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July",
                     "August", "September", "October", "November", "December"];
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday",
                  "Thursday", "Friday", "Saturday"];

/** "6 Sep" -- short enough for an axis, unambiguous across months. */
function axisDate(iso: string): string {
  const parsed = new Date(iso + "T00:00:00Z");
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return String(parsed.getUTCDate()) + " " + MONTHS[parsed.getUTCMonth()];
}

/** "Thursday 6 August" -- what the tooltip and the table row say. */
function fullDate(iso: string): string {
  const parsed = new Date(iso + "T00:00:00Z");
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return (
    WEEKDAYS[parsed.getUTCDay()] +
    " " +
    String(parsed.getUTCDate()) +
    " " +
    MONTHS_LONG[parsed.getUTCMonth()]
  );
}

function plural(n: number, one: string, many: string): string {
  return String(n) + " " + (n === 1 ? one : many);
}

export default function ActivityMatrix({ summary }: ActivityMatrixProps) {
  const [cursor, setCursor] = useState<number | null>(null);
  const [boxW, setBoxW] = useState(FALLBACK_W);
  const frame = useRef<HTMLDivElement | null>(null);

  useEffect(function measure() {
    const node = frame.current;
    if (node === null || typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver(function onResize(entries) {
      const w = entries[0].contentRect.width;
      if (w > 0) {
        setBoxW(w);
      }
    });
    observer.observe(node);
    return function stop() {
      observer.disconnect();
    };
  }, []);

  const model = useMemo(
    function buildModel() {
      const days = summary.days;
      const totals = days.map(function total(day) {
        return day.ok + day.error;
      });
      const peak = totals.reduce(function larger(a, b) {
        return a > b ? a : b;
      }, 0);

      // One cell is `unit` calls. Quantising to ROWS keeps every column on the
      // same scale, so two columns of equal height mean equal volume -- the
      // whole point of a cell field.
      const unit = peak === 0 ? 1 : Math.ceil(peak / ROWS);
      const ceiling = unit * ROWS;

      const columns: Column[] = days.map(function toColumn(day, index) {
        const total = totals[index];
        // A day with any calls at all lights at least one cell: rounding a
        // real 1 down to an unlit column would say "nothing happened".
        const filled = total === 0 ? 0 : Math.max(1, Math.min(ROWS, Math.ceil(total / unit)));
        const errorCells =
          day.error === 0 ? 0 : Math.max(1, Math.min(filled, Math.round(day.error / unit)));
        return {
          label: axisDate(day.date),
          iso: day.date,
          ok: day.ok,
          error: day.error,
          total: total,
          okCells: filled - errorCells,
          errorCells: errorCells,
        };
      });

      return { columns: columns, unit: unit, ceiling: ceiling, peak: peak };
    },
    [summary],
  );

  const columns = model.columns;
  // Divide the measured width among the columns, then square the cell.
  const raw = columns.length === 0 ? 0 : (boxW - AXIS_W) / columns.length - GAP;
  const CELL = Math.max(MIN_CELL, Math.min(MAX_CELL, Math.round(raw)));
  const STEP = CELL + GAP;
  const width = AXIS_W + columns.length * STEP;
  const plotH = ROWS * STEP;
  const height = plotH + AXIS_H;

  const onMove = useCallback(
    function onMove(event: React.PointerEvent<SVGSVGElement>) {
      const box = event.currentTarget.getBoundingClientRect();
      if (box.width === 0) {
        return;
      }
      // Map the pointer through the viewBox rather than trusting pixel maths:
      // the SVG scales with the panel, so client pixels are not SVG units.
      const x = ((event.clientX - box.left) / box.width) * width - AXIS_W;
      const index = Math.floor(x / STEP);
      setCursor(index >= 0 && index < columns.length ? index : null);
    },
    [columns.length, width, STEP],
  );

  const onKey = useCallback(
    function onKey(event: React.KeyboardEvent<SVGSVGElement>) {
      // The whole field is ONE tab stop and the arrows walk it. Thirty
      // focusable columns would bury the rest of the page behind a chart.
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") {
        if (event.key === "Escape") {
          setCursor(null);
        }
        return;
      }
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      setCursor(function next(current) {
        if (current === null) {
          return step > 0 ? 0 : columns.length - 1;
        }
        const moved = current + step;
        return moved < 0 || moved >= columns.length ? current : moved;
      });
    },
    [columns.length],
  );

  if (columns.length === 0) {
    return null;
  }

  const active = cursor === null ? null : columns[cursor];

  // Ticks at the top and middle of the scale. Two labels, not twelve: the axis
  // says what the scale is, the tooltip says what a day was.
  const ticks = [
    { value: model.ceiling, y: 0 },
    { value: Math.round(model.ceiling / 2), y: plotH / 2 },
  ];

  return (
    <figure className="ov-matrix" ref={frame}>
      <figcaption className="ov-matrix-head">
        <span className="ov-matrix-title">Activity trend</span>
        <span className="ov-matrix-legend">
          <span className="ov-matrix-key">
            <span className="ov-matrix-swatch ov-matrix-swatch-ok" aria-hidden="true" />
            Succeeded
          </span>
          <span className="ov-matrix-key">
            <span className="ov-matrix-swatch ov-matrix-swatch-bad" aria-hidden="true" />
            Failed
          </span>
        </span>
      </figcaption>

      <div className="ov-matrix-plot">
        <svg
          className="ov-matrix-svg"
          viewBox={"0 0 " + width + " " + height}
          preserveAspectRatio="xMidYMid meet"
          role="img"
          tabIndex={0}
          aria-label={
            "Call volume over " +
            plural(columns.length, "day", "days") +
            ": " +
            plural(summary.total, "call", "calls") +
            ", " +
            plural(summary.errors, "failure", "failures") +
            ". Use the arrow keys to read a day."
          }
          onPointerMove={onMove}
          onPointerLeave={function clear() {
            setCursor(null);
          }}
          onKeyDown={onKey}
          onBlur={function clear() {
            setCursor(null);
          }}
        >
          {ticks.map(function tick(entry) {
            return (
              <g key={entry.value + "@" + entry.y}>
                <text className="ov-matrix-tick" x={AXIS_W - 7} y={entry.y + 7} textAnchor="end">
                  {entry.value}
                </text>
                <line
                  className="ov-matrix-rule"
                  x1={AXIS_W - 2}
                  x2={width}
                  y1={entry.y - GAP / 2}
                  y2={entry.y - GAP / 2}
                />
              </g>
            );
          })}

          {columns.map(function column(day, index) {
            const x = AXIS_W + index * STEP;
            const cells = [];
            for (let row = 0; row < ROWS; row += 1) {
              // Row 0 is the BOTTOM of the column: volume grows upward from a
              // single baseline, like every other magnitude mark.
              const y = plotH - (row + 1) * STEP + GAP;
              let tone = "ov-matrix-cell-empty";
              if (row < day.okCells) {
                tone = "ov-matrix-cell-ok";
              } else if (row < day.okCells + day.errorCells) {
                tone = "ov-matrix-cell-bad";
              }
              cells.push(
                <rect
                  key={row}
                  className={"ov-matrix-cell " + tone}
                  x={x}
                  y={y}
                  width={CELL}
                  height={CELL}
                  rx={1.5}
                />,
              );
            }
            return (
              <g key={day.iso} className={cursor === index ? "ov-matrix-col is-on" : "ov-matrix-col"}>
                {cells}
              </g>
            );
          })}

          {cursor !== null ? (
            <line
              className="ov-matrix-cursor"
              x1={AXIS_W + cursor * STEP + CELL / 2}
              x2={AXIS_W + cursor * STEP + CELL / 2}
              y1={-2}
              y2={plotH + 2}
            />
          ) : null}

          {columns.map(function label(day, index) {
            // Space labels by what fits: a date needs ~44px, so derive the
            // stride from the column width instead of guessing a constant.
            const every = Math.max(1, Math.ceil(44 / STEP));
            if (index % every !== 0) {
              return null;
            }
            return (
              <text
                key={"x" + day.iso}
                className="ov-matrix-xlabel"
                x={AXIS_W + index * STEP + CELL / 2}
                y={plotH + 15}
                textAnchor="middle"
              >
                {day.label}
              </text>
            );
          })}
        </svg>

        {active !== null ? (
          <div
            className="ov-matrix-tip"
            role="status"
            style={{
              left:
                "calc(" +
                (((AXIS_W + cursor! * STEP + CELL / 2) / width) * 100).toFixed(3) +
                "% )",
            }}
          >
            <span className="ov-matrix-tip-day">{fullDate(active.iso)}</span>
            <span className="ov-matrix-tip-row">
              <b>{active.total}</b> {active.total === 1 ? "call" : "calls"}
            </span>
            {active.error > 0 ? (
              <span className="ov-matrix-tip-row ov-matrix-tip-bad">
                <b>{active.error}</b> failed
              </span>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* The relief the palette check requires: brand amber sits under 3:1 on
          this cream surface, so every value the field encodes is also readable
          as text. It doubles as the screen-reader route through the data. */}
      <details className="ov-matrix-table">
        <summary>Show the numbers</summary>
        <div className="ov-matrix-table-scroll">
          <table>
            <caption>Calls per day, most recent last</caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">Succeeded</th>
                <th scope="col">Failed</th>
              </tr>
            </thead>
            <tbody>
              {columns.map(function row(day) {
                return (
                  <tr key={"t" + day.iso}>
                    <th scope="row">{fullDate(day.iso)}</th>
                    <td>{day.ok}</td>
                    <td>{day.error}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
