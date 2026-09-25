"use client";

/**
 * The report's two charts, drawn as plain SVG (no charting library: the app
 * ships none, and a bar chart is not worth one).
 *
 * TrendChart: one bar per day for a chosen metric, read against three
 * references -- the same days of the previous period (a dashed line), the
 * seven-day trailing average (a solid line), and the spikes, which are the
 * days more than two standard deviations over the window's own normal. The
 * tooltip gives the day's whole story, not just the plotted metric.
 *
 * ColumnChart: an ordinary labelled bar chart for the hour-of-day and
 * day-of-week views, with the best few bars picked out.
 *
 * Both size themselves to the width they are given and never scale their text,
 * take the arrow keys as well as the pointer, and are backed by a table of the
 * same numbers under a "Show the numbers" disclosure.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  dayLong,
  dayShort,
  deltaOf,
  findSpikes,
  fmtCompact,
  fmtDelta,
  fmtKpi,
  fmtMoney,
  fmtNum,
  fmtPct,
  movingAverage,
  derive,
} from "@/components/reports/google-ads/ads-model";
import type { DayRow, KpiKind } from "@/components/reports/google-ads/ads-model";
import { niceCeiling } from "@/components/dashboard/spikes";

export type TrendMetric = "cost" | "impressions" | "clicks" | "conversions" | "conversionValue";

export const TREND_METRICS: Array<{
  id: TrendMetric;
  label: string;
  color: string;
  kind: KpiKind;
}> = [
  { id: "cost", label: "Spend", color: "#1a73e8", kind: "money" },
  { id: "impressions", label: "Impressions", color: "#8e6cd1", kind: "int" },
  { id: "clicks", label: "Clicks", color: "#0f9d9d", kind: "int" },
  { id: "conversions", label: "Conversions", color: "#30a14e", kind: "num" },
  { id: "conversionValue", label: "Conv. value", color: "#e8a23f", kind: "money" },
];

const HEIGHT = 270;
const PAD = { left: 58, right: 14, top: 26, bottom: 28 };
const FALLBACK_W = 720;

function useWidth(): [React.MutableRefObject<HTMLDivElement | null>, number] {
  const frame = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState<number>(FALLBACK_W);
  useEffect(function measure() {
    const node = frame.current;
    if (node === null || typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver(function onResize(entries) {
      const w = entries[0].contentRect.width;
      if (w > 0) {
        setWidth(w);
      }
    });
    observer.observe(node);
    return function stop() {
      observer.disconnect();
    };
  }, []);
  return [frame, width];
}

export function TrendChart({
  days,
  previous,
  currency,
  metric,
}: {
  days: DayRow[];
  /** The previous period, filled to the same length so bars line up by index. */
  previous: DayRow[] | null;
  currency: string;
  metric: TrendMetric;
}) {
  const [frame, boxW] = useWidth();
  const [cursor, setCursor] = useState<number | null>(null);

  const def = TREND_METRICS.filter(function is(m) {
    return m.id === metric;
  })[0];
  const n = days.length;

  const values = useMemo(
    function pick() {
      return days.map(function value(d) {
        return d[metric];
      });
    },
    [days, metric]
  );
  const prevValues = useMemo(
    function pickPrev() {
      return previous === null
        ? null
        : previous.map(function value(d) {
            return d[metric];
          });
    },
    [previous, metric]
  );
  const average = useMemo(
    function avg() {
      return n >= 10 ? movingAverage(values, 7) : [];
    },
    [values, n]
  );
  const spikes = useMemo(
    function find() {
      return findSpikes(values);
    },
    [values]
  );

  if (n === 0) {
    return null;
  }

  const money = def.kind === "money";
  const label = function label(v: number): string {
    return money ? fmtMoney(v, currency, { compact: true, decimals: v >= 1000 ? 0 : v >= 10 ? 0 : 2 }) : fmtCompact(v);
  };
  const full = function full(v: number): string {
    return fmtKpi(def.kind, v, currency);
  };

  const width = Math.max(260, boxW);
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const slot = plotW / n;
  const gap = slot > 8 ? 2 : 1;
  const barW = Math.max(2, Math.min(30, slot - gap));

  const peak = Math.max.apply(
    null,
    values.concat(prevValues === null ? [] : prevValues).concat([0])
  );
  const yMax = niceCeiling(Math.max(peak, 1));
  const y = function y(v: number): number {
    return PAD.top + plotH * (1 - v / yMax);
  };
  const xc = function xc(i: number): number {
    return PAD.left + i * slot + slot / 2;
  };

  const every = Math.max(1, Math.ceil(n / 8));
  const active = cursor;
  const tipX = active === null ? 0 : xc(active);
  const tipSide = tipX < 150 ? " is-left" : tipX > width - 150 ? " is-right" : "";
  const total = values.reduce(function add(a, b) {
    return a + b;
  }, 0);
  const mean = total / n;

  function locate(event: React.PointerEvent<SVGSVGElement>): number | null {
    const box = event.currentTarget.getBoundingClientRect();
    const i = Math.floor((event.clientX - box.left - PAD.left) / slot);
    return i >= 0 && i < n ? i : null;
  }

  function onKey(event: React.KeyboardEvent<SVGSVGElement>): void {
    if (event.key === "Escape") {
      setCursor(null);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
      return;
    }
    event.preventDefault();
    const step = event.key === "ArrowRight" ? 1 : -1;
    setCursor(function move(current) {
      if (current === null) {
        return step > 0 ? 0 : n - 1;
      }
      const next = current + step;
      return next < 0 || next >= n ? current : next;
    });
  }

  const day = active === null ? null : days[active];
  const dayTotals = day === null ? null : derive(day);
  const prevDay = active === null || previous === null ? null : previous[active];
  const delta =
    prevDay === null || day === null ? null : deltaOf(day[metric], prevDay[metric]);

  return (
    <div className="ga-plot" ref={frame} style={{ width: "100%" }}>
      <div className="ga-plot-inner" style={{ width: width }}>
        <svg
          className="ga-svg"
          width={width}
          height={HEIGHT}
          viewBox={"0 0 " + String(width) + " " + String(HEIGHT)}
          role="img"
          tabIndex={0}
          aria-label={
            def.label + " per day: " + full(total) + " in total over " + String(n) +
            " days, " + String(spikes.length) + (spikes.length === 1 ? " spike" : " spikes") +
            ". Use the arrow keys to read a day."
          }
          onPointerMove={function onMove(event) {
            setCursor(locate(event));
          }}
          onPointerLeave={function onLeave() {
            setCursor(null);
          }}
          onKeyDown={onKey}
          onBlur={function onBlur() {
            setCursor(null);
          }}
        >
          {[0, yMax / 2, yMax].map(function tick(value) {
            return (
              <g key={"t" + String(value)}>
                <line
                  className={value === 0 ? "ga-base" : "ga-grid"}
                  x1={PAD.left}
                  x2={width - PAD.right}
                  y1={y(value)}
                  y2={y(value)}
                />
                <text className="ga-tick" x={PAD.left - 8} y={y(value) + 4} textAnchor="end">
                  {label(value)}
                </text>
              </g>
            );
          })}

          {active !== null ? (
            <rect className="ga-band" x={PAD.left + active * slot} y={PAD.top} width={slot} height={plotH} />
          ) : null}

          {values.map(function bar(v, i) {
            if (v <= 0) {
              return null;
            }
            const h = Math.max(1, plotH * (v / yMax));
            return (
              <rect
                key={days[i].date}
                x={xc(i) - barW / 2}
                y={y(0) - h}
                width={barW}
                height={h}
                rx={Math.min(2, barW / 2)}
                fill={def.color}
                opacity={active === null || active === i ? 0.92 : 0.62}
              />
            );
          })}

          {prevValues !== null ? (
            <polyline
              className="ga-prev"
              fill="none"
              points={prevValues
                .map(function point(v, i) {
                  return String(xc(i).toFixed(1)) + "," + String(y(v).toFixed(1));
                })
                .join(" ")}
            />
          ) : null}

          {average.length > 0 ? (
            <polyline
              className="ga-avg"
              fill="none"
              stroke={def.color}
              points={average
                .map(function point(v, i) {
                  return v === null ? "" : String(xc(i).toFixed(1)) + "," + String(y(v).toFixed(1));
                })
                .filter(Boolean)
                .join(" ")}
            />
          ) : null}

          {spikes.map(function marker(i) {
            const top = y(values[i]) - 4;
            const cx = xc(i);
            return (
              <g key={"s" + String(i)}>
                <path
                  className="ga-spike"
                  d={
                    "M" + String(cx - 4.5) + "," + String(top - 6) +
                    " L" + String(cx + 4.5) + "," + String(top - 6) +
                    " L" + String(cx) + "," + String(top) + " Z"
                  }
                />
                <text className="ga-spike-n" x={cx} y={top - 10} textAnchor="middle">
                  {label(values[i])}
                </text>
              </g>
            );
          })}

          {days.map(function xlabel(d, i) {
            if ((n - 1 - i) % every !== 0) {
              return null;
            }
            return (
              <text key={"x" + d.date} className="ga-tick" x={xc(i)} y={HEIGHT - 8} textAnchor="middle">
                {dayShort(d.date)}
              </text>
            );
          })}
        </svg>

        {day !== null && dayTotals !== null && active !== null ? (
          <div className={"ga-tip" + tipSide} style={{ left: tipX, top: PAD.top }} role="presentation">
            <span className="ga-tip-day">{dayLong(day.date)}</span>
            <span className="ga-tip-main">
              <b>{full(day[metric])}</b> {def.label.toLowerCase()}
            </span>
            {prevDay !== null ? (
              <span className="ga-tip-row">
                {"Previous: " + full(prevDay[metric])}
                {delta !== null ? " (" + fmtDelta(delta) + ")" : ""}
              </span>
            ) : null}
            <span className="ga-tip-grid">
              <span>Spend</span>
              <b>{fmtMoney(day.cost, currency, { compact: true })}</b>
              <span>Clicks</span>
              <b>{fmtCompact(day.clicks)}</b>
              <span>Impressions</span>
              <b>{fmtCompact(day.impressions)}</b>
              <span>Conversions</span>
              <b>{fmtNum(day.conversions, 1)}</b>
              <span>CTR</span>
              <b>{fmtPct(dayTotals.ctr)}</b>
            </span>
            {spikes.indexOf(active) !== -1 ? (
              <span className="ga-tip-flag">
                {"Spike · " + fmtNum(mean > 0 ? values[active] / mean : 0, 1) + "× the daily average"}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Columns                                                             */
/* ------------------------------------------------------------------ */

export interface ColumnItem {
  key: string;
  label: string;
  value: number;
  /** A second line for the tooltip: "12 conversions", say. */
  detail?: string;
}

export function ColumnChart({
  items,
  format,
  color,
  highlight = 3,
  ariaLabel,
  labelEvery = 1,
}: {
  items: ColumnItem[];
  format: (n: number) => string;
  color: string;
  /** How many of the tallest bars to picks out. */
  highlight?: number;
  ariaLabel: string;
  /** Label every n-th bar (24 hours would not fit if every one were named). */
  labelEvery?: number;
}) {
  const [frame, boxW] = useWidth();
  const [cursor, setCursor] = useState<number | null>(null);
  const n = items.length;
  if (n === 0) {
    return null;
  }

  const height = 200;
  const pad = { left: 46, right: 10, top: 16, bottom: 26 };
  const width = Math.max(260, boxW);
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const slot = plotW / n;
  const barW = Math.max(3, Math.min(34, slot - 4));
  const peak = Math.max.apply(
    null,
    items.map(function v(i) {
      return i.value;
    }).concat([0])
  );
  const yMax = niceCeiling(Math.max(peak, 1));
  const y = function y(v: number): number {
    return pad.top + plotH * (1 - v / yMax);
  };
  const ranked = items
    .map(function withIndex(item, i) {
      return { i: i, v: item.value };
    })
    .sort(function byValue(a, b) {
      return b.v - a.v;
    })
    .slice(0, highlight)
    .filter(function positive(r) {
      return r.v > 0;
    })
    .map(function index(r) {
      return r.i;
    });

  const active = cursor;
  const tipX = active === null ? 0 : pad.left + active * slot + slot / 2;
  const tipSide = tipX < 100 ? " is-left" : tipX > width - 100 ? " is-right" : "";

  function locate(event: React.PointerEvent<SVGSVGElement>): number | null {
    const box = event.currentTarget.getBoundingClientRect();
    const i = Math.floor((event.clientX - box.left - pad.left) / slot);
    return i >= 0 && i < n ? i : null;
  }

  return (
    <div className="ga-plot" ref={frame} style={{ width: "100%" }}>
      <div className="ga-plot-inner" style={{ width: width }}>
        <svg
          className="ga-svg"
          width={width}
          height={height}
          viewBox={"0 0 " + String(width) + " " + String(height)}
          role="img"
          tabIndex={0}
          aria-label={ariaLabel}
          onPointerMove={function onMove(event) {
            setCursor(locate(event));
          }}
          onPointerLeave={function onLeave() {
            setCursor(null);
          }}
          onKeyDown={function onKey(event) {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
              return;
            }
            event.preventDefault();
            const step = event.key === "ArrowRight" ? 1 : -1;
            setCursor(function move(current) {
              if (current === null) {
                return step > 0 ? 0 : n - 1;
              }
              const next = current + step;
              return next < 0 || next >= n ? current : next;
            });
          }}
          onBlur={function onBlur() {
            setCursor(null);
          }}
        >
          {[0, yMax / 2, yMax].map(function tick(value) {
            return (
              <g key={"t" + String(value)}>
                <line
                  className={value === 0 ? "ga-base" : "ga-grid"}
                  x1={pad.left}
                  x2={width - pad.right}
                  y1={y(value)}
                  y2={y(value)}
                />
                <text className="ga-tick" x={pad.left - 8} y={y(value) + 4} textAnchor="end">
                  {format(value)}
                </text>
              </g>
            );
          })}

          {items.map(function bar(item, i) {
            const h = item.value <= 0 ? 0 : Math.max(1, plotH * (item.value / yMax));
            const isBest = ranked.indexOf(i) !== -1;
            const x = pad.left + i * slot + (slot - barW) / 2;
            return (
              <g key={item.key}>
                {h > 0 ? (
                  <rect
                    x={x}
                    y={y(0) - h}
                    width={barW}
                    height={h}
                    rx={Math.min(3, barW / 2)}
                    fill={color}
                    opacity={active === i ? 1 : isBest ? 0.95 : 0.45}
                  />
                ) : null}
                {i % labelEvery === 0 ? (
                  <text className="ga-tick" x={x + barW / 2} y={height - 8} textAnchor="middle">
                    {item.label}
                  </text>
                ) : null}
              </g>
            );
          })}
        </svg>

        {active !== null ? (
          <div className={"ga-tip" + tipSide} style={{ left: tipX, top: pad.top }} role="presentation">
            <span className="ga-tip-day">{items[active].label}</span>
            <span className="ga-tip-main">
              <b>{format(items[active].value)}</b>
            </span>
            {items[active].detail !== undefined ? (
              <span className="ga-tip-row">{items[active].detail}</span>
            ) : null}
            {ranked.indexOf(active) !== -1 ? <span className="ga-tip-flag">One of the best</span> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
