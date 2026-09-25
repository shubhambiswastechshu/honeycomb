"use client";

/**
 * Calls over time as bars, with the spikes called out.
 *
 * The calendar above it answers "which days were busy over the year"; this
 * answers "what did the last month, or the last day, actually look like, and
 * what stood out". Each bar is one day (or one hour) split into succeeded and
 * failed, drawn against three reference marks:
 *
 *   - the AVERAGE, a dashed line, so a bar is read against normal rather than
 *     against the tallest bar on the page;
 *   - the SPIKE LINE, a dotted line at the average plus two standard
 *     deviations -- a bar over it (with at least three calls) is a volume
 *     spike, and gets a marker and its count above it;
 *   - a red marker for a FAILURE spike: three or more failures making up at
 *     least a third of the bucket, which the volume test would never notice in
 *     a quiet hour.
 *
 * The rule lives in spikes.ts and is shared with the calendar, so the two
 * charts cannot disagree, and it is stated under the chart in words rather
 * than left as a mystery. Under the chart, the spikes are listed with their
 * numbers -- how many calls, how many times the average, how many failed --
 * because a marker says "look here" and a person then needs the details.
 *
 * Hourly comes from the live snapshot (the last 24 clock hours) and is shown
 * in the viewer's local time; the daily ranges come from the year of day
 * counts and are UTC days, which the caption says.
 *
 * Every number is counted server-side. An empty window says so instead of
 * drawing a flat line that looks like a measurement.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { dayLabel, hourLabel, tailSummary } from "@/components/dashboard/live-model";
import { analyse, niceCeiling, plural, toBucket } from "@/components/dashboard/spikes";
import type { Bucket, Spike } from "@/components/dashboard/spikes";
import type { ActivityLive, ActivitySummary } from "@/lib/api";
import "@/components/dashboard/activity-charts.css";

type Range = "30d" | "90d" | "24h";

interface RangeOption {
  id: Range;
  label: string;
  unit: "day" | "hour";
}

const RANGES: RangeOption[] = [
  { id: "30d", label: "30 days", unit: "day" },
  { id: "90d", label: "90 days", unit: "day" },
  { id: "24h", label: "24 hours", unit: "hour" },
];

const HEIGHT = 230;
const PAD = { left: 34, right: 14, top: 22, bottom: 26 };
const FALLBACK_W = 760;
const MAX_SPIKES_LISTED = 6;
const WEEKDAY_NAMES = [
  "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
];

function shortDay(iso: string): string {
  return new Date(iso + "T00:00:00Z").toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });
}

function longHour(iso: string): string {
  return new Date(iso).toLocaleString([], {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function spikeWords(kind: Spike["kind"]): string {
  if (kind === "both") {
    return "Volume and failure spike";
  }
  return kind === "volume" ? "Volume spike" : "Failure spike";
}

function oneDecimal(n: number): string {
  return n >= 10 ? String(Math.round(n)) : (Math.round(n * 10) / 10).toFixed(1);
}

export interface ActivityTrendProps {
  summary: ActivitySummary;
  /** The last-24-hours snapshot; the hourly view is offered once it exists. */
  live: ActivityLive | null;
}

export default function ActivityTrend({ summary, live }: ActivityTrendProps) {
  const frame = useRef<HTMLDivElement | null>(null);
  const [range, setRange] = useState<Range>("30d");
  const [boxW, setBoxW] = useState<number>(FALLBACK_W);
  const [cursor, setCursor] = useState<number | null>(null);
  const [showNumbers, setShowNumbers] = useState<boolean>(false);

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

  // If the hourly view was chosen and the snapshot is not there any more,
  // fall back rather than draw nothing.
  const effective: Range = range === "24h" && live === null ? "30d" : range;
  const option = RANGES.filter(function match(r) {
    return r.id === effective;
  })[0];
  const hourly = option.unit === "hour";

  const model = useMemo(
    function build() {
      let buckets: Bucket[];
      let axis: string[];
      let long: string[];
      if (hourly && live !== null) {
        buckets = live.hours.map(function bucket(h) {
          return toBucket(h.start, h.ok, h.error);
        });
        axis = live.hours.map(function label(h) {
          return hourLabel(h.start);
        });
        long = live.hours.map(function label(h) {
          return longHour(h.start);
        });
      } else {
        const windowed = tailSummary(summary, effective === "90d" ? 90 : 30);
        buckets = windowed.days.map(function bucket(d) {
          return toBucket(d.date, d.ok, d.error);
        });
        axis = windowed.days.map(function label(d) {
          return shortDay(d.date);
        });
        long = windowed.days.map(function label(d) {
          return dayLabel(d.date, true);
        });
      }
      return { buckets: buckets, axis: axis, long: long, analysis: analyse(buckets) };
    },
    [summary, live, effective, hourly]
  );

  const { buckets, axis, long, analysis } = model;
  const n = buckets.length;
  const unit = option.unit;

  const spikeAt = useMemo(
    function index() {
      const map: Record<number, Spike> = {};
      analysis.spikes.forEach(function add(spike) {
        map[spike.index] = spike;
      });
      return map;
    },
    [analysis]
  );

  // Busiest weekday, for the daily views only: an hour has no weekday.
  const weekday = useMemo(
    function busiestWeekday() {
      if (hourly) {
        return null;
      }
      const sums = [0, 0, 0, 0, 0, 0, 0];
      const counts = [0, 0, 0, 0, 0, 0, 0];
      buckets.forEach(function add(b) {
        const dow = new Date(b.key + "T00:00:00Z").getUTCDay();
        sums[dow] += b.total;
        counts[dow] += 1;
      });
      let best = -1;
      let bestMean = 0;
      sums.forEach(function pick(sum, dow) {
        const mean = counts[dow] === 0 ? 0 : sum / counts[dow];
        if (mean > bestMean) {
          bestMean = mean;
          best = dow;
        }
      });
      return best === -1 ? null : { name: WEEKDAY_NAMES[best], mean: bestMean };
    },
    [buckets, hourly]
  );

  if (n === 0) {
    return null;
  }

  const width = Math.max(320, boxW);
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const slot = plotW / n;
  const gap = slot > 8 ? 2 : 1;
  const barW = Math.max(2, Math.min(26, slot - gap));

  const yMax = niceCeiling(Math.max(analysis.peak, 1));
  const y = function y(value: number): number {
    return PAD.top + plotH * (1 - value / yMax);
  };
  const xCenter = function xCenter(i: number): number {
    return PAD.left + i * slot + slot / 2;
  };

  const every = Math.max(1, Math.ceil(n / 8));
  const active = cursor === null ? null : cursor;
  const activeBucket = active === null ? null : buckets[active];
  const activeSpike = active === null ? undefined : spikeAt[active];
  const tipX = active === null ? 0 : xCenter(active);
  const tipSide = tipX < 120 ? " is-left" : tipX > width - 120 ? " is-right" : "";

  const listed = analysis.spikes
    .slice()
    .sort(function biggest(a, b) {
      return buckets[b.index].total - buckets[a.index].total;
    })
    .slice(0, MAX_SPIKES_LISTED);
  const hidden = analysis.spikes.length - listed.length;

  const showThreshold =
    analysis.total > 0 && analysis.threshold > 0 && analysis.threshold <= yMax;

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

  return (
    <figure className="trd" ref={frame}>
      <figcaption className="trd-head">
        <span className="trd-title">Calls per {unit}</span>
        <div className="trd-range" role="group" aria-label="Chart range">
          {RANGES.map(function choice(r) {
            const disabled = r.id === "24h" && live === null;
            return (
              <button
                key={r.id}
                type="button"
                className="trd-range-btn"
                aria-pressed={effective === r.id}
                disabled={disabled}
                onClick={function pick() {
                  setRange(r.id);
                  setCursor(null);
                }}
              >
                {r.label}
              </button>
            );
          })}
        </div>
      </figcaption>

      <dl className="trd-stats">
        <div className="trd-stat">
          <dt>Calls</dt>
          <dd>{analysis.total}</dd>
        </div>
        <div className="trd-stat">
          <dt>{"Average per " + unit}</dt>
          <dd>{oneDecimal(analysis.mean)}</dd>
        </div>
        <div className="trd-stat">
          <dt>Peak</dt>
          <dd>
            {analysis.peak}
            {analysis.peakIndex >= 0 ? (
              <span className="trd-stat-sub">{axis[analysis.peakIndex]}</span>
            ) : null}
          </dd>
        </div>
        <div className={analysis.errors > 0 ? "trd-stat is-bad" : "trd-stat"}>
          <dt>Failure rate</dt>
          <dd>
            {String(analysis.failRate) + "%"}
            <span className="trd-stat-sub">{plural(analysis.errors, "failure", "failures")}</span>
          </dd>
        </div>
        <div className={analysis.spikes.length > 0 ? "trd-stat is-flag" : "trd-stat"}>
          <dt>Spikes</dt>
          <dd>{analysis.spikes.length}</dd>
        </div>
      </dl>

      <div className="trd-plot" style={{ width: width }}>
        <svg
          className="trd-svg"
          width={width}
          height={HEIGHT}
          viewBox={"0 0 " + String(width) + " " + String(HEIGHT)}
          role="img"
          tabIndex={0}
          aria-label={
            "Calls per " +
            unit +
            " over the last " +
            option.label +
            ": " +
            plural(analysis.total, "call", "calls") +
            ", " +
            plural(analysis.spikes.length, "spike", "spikes") +
            ". Use the arrow keys to read one " +
            unit +
            "."
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
          {/* Gridlines and the value axis: bottom, middle, top. */}
          {[0, yMax / 2, yMax].map(function tick(value) {
            return (
              <g key={"t" + String(value)}>
                <line
                  className={value === 0 ? "trd-base" : "trd-grid"}
                  x1={PAD.left}
                  x2={width - PAD.right}
                  y1={y(value)}
                  y2={y(value)}
                />
                <text className="trd-tick" x={PAD.left - 8} y={y(value) + 4} textAnchor="end">
                  {value}
                </text>
              </g>
            );
          })}

          {active !== null ? (
            <rect
              className="trd-band"
              x={PAD.left + active * slot}
              y={PAD.top}
              width={slot}
              height={plotH}
            />
          ) : null}

          {buckets.map(function bar(bucket, i) {
            const x = xCenter(i) - barW / 2;
            const okH = (bucket.ok / yMax) * plotH;
            const errH = (bucket.error / yMax) * plotH;
            return (
              <g key={bucket.key}>
                {okH > 0 ? (
                  <rect
                    className="trd-ok"
                    x={x}
                    y={y(0) - okH}
                    width={barW}
                    height={okH}
                    rx={Math.min(2, barW / 2)}
                  />
                ) : null}
                {errH > 0 ? (
                  <rect
                    className="trd-bad"
                    x={x}
                    y={y(0) - okH - errH}
                    width={barW}
                    height={errH}
                    rx={Math.min(2, barW / 2)}
                  />
                ) : null}
              </g>
            );
          })}

          {analysis.mean > 0 ? (
            <g>
              <line
                className="trd-avg"
                x1={PAD.left}
                x2={width - PAD.right}
                y1={y(analysis.mean)}
                y2={y(analysis.mean)}
              />
              <text
                className="trd-ref"
                x={width - PAD.right - 2}
                y={y(analysis.mean) - 4}
                textAnchor="end"
              >
                {"avg " + oneDecimal(analysis.mean)}
              </text>
            </g>
          ) : null}

          {showThreshold ? (
            <g>
              <line
                className="trd-limit"
                x1={PAD.left}
                x2={width - PAD.right}
                y1={y(analysis.threshold)}
                y2={y(analysis.threshold)}
              />
              <text
                className="trd-ref"
                x={PAD.left + 4}
                y={y(analysis.threshold) - 4}
              >
                spike line
              </text>
            </g>
          ) : null}

          {analysis.spikes.map(function marker(spike) {
            const bucket = buckets[spike.index];
            const cx = xCenter(spike.index);
            const top = y(bucket.total) - 4;
            const failing = spike.kind === "failures";
            return (
              <g key={"s" + String(spike.index)}>
                <path
                  className={failing ? "trd-spike is-bad" : "trd-spike"}
                  d={
                    "M" + String(cx - 4.5) + "," + String(top - 6) +
                    " L" + String(cx + 4.5) + "," + String(top - 6) +
                    " L" + String(cx) + "," + String(top) + " Z"
                  }
                />
                <text
                  className={failing ? "trd-spike-n is-bad" : "trd-spike-n"}
                  x={cx}
                  y={top - 10}
                  textAnchor="middle"
                >
                  {bucket.total}
                </text>
              </g>
            );
          })}

          {axis.map(function label(text, i) {
            if ((n - 1 - i) % every !== 0) {
              return null;
            }
            return (
              <text
                key={"x" + String(i)}
                className="trd-tick"
                x={xCenter(i)}
                y={HEIGHT - 8}
                textAnchor="middle"
              >
                {text}
              </text>
            );
          })}
        </svg>

        {activeBucket !== null && active !== null ? (
          <div
            className={"trd-tip" + tipSide}
            style={{ left: tipX, top: PAD.top }}
            role="presentation"
          >
            <span className="trd-tip-day">{long[active]}</span>
            <span className="trd-tip-row">
              <b>{plural(activeBucket.total, "call", "calls")}</b>
            </span>
            {activeBucket.total > 0 ? (
              <span className="trd-tip-row">
                {String(activeBucket.ok) + " succeeded"}
                {activeBucket.error > 0 ? (
                  <span className="trd-tip-bad">
                    {" · " + String(activeBucket.error) + " failed"}
                  </span>
                ) : null}
              </span>
            ) : null}
            {analysis.mean > 0 && activeBucket.total > 0 ? (
              <span className="trd-tip-row">
                {oneDecimal(activeBucket.total / analysis.mean) +
                  "× the " +
                  (hourly ? "hourly" : "daily") +
                  " average"}
              </span>
            ) : null}
            {activeSpike !== undefined ? (
              <span className="trd-tip-flag">{spikeWords(activeSpike.kind)}</span>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="trd-legend" aria-hidden="true">
        <span className="trd-key"><span className="trd-swatch trd-ok" /> Succeeded</span>
        <span className="trd-key"><span className="trd-swatch trd-bad" /> Failed</span>
        <span className="trd-key"><span className="trd-line trd-avg" /> Average</span>
        <span className="trd-key"><span className="trd-line trd-limit" /> Spike line</span>
      </div>

      <section className="trd-spikes" aria-label="Spikes">
        <h3 className="trd-spikes-title">
          {analysis.spikes.length === 0
            ? "No spikes"
            : plural(analysis.spikes.length, "spike", "spikes")}
        </h3>
        {analysis.total === 0 ? (
          <p className="trd-none">{"No calls in this window yet, so there is nothing to compare."}</p>
        ) : analysis.spikes.length === 0 ? (
          <p className="trd-none">
            {"Every " + unit + " stayed within the normal range for this window."}
          </p>
        ) : (
          <ul className="trd-spike-list">
            {listed.map(function item(spike) {
              const bucket = buckets[spike.index];
              return (
                <li className="trd-spike-row" key={bucket.key}>
                  <span
                    className={
                      spike.kind === "failures"
                        ? "trd-spike-kind is-bad"
                        : "trd-spike-kind"
                    }
                  >
                    {spikeWords(spike.kind)}
                  </span>
                  <span className="trd-spike-when">{long[spike.index]}</span>
                  <span className="trd-spike-facts">
                    {plural(bucket.total, "call", "calls")}
                    {spike.ratio > 0 ? " · " + oneDecimal(spike.ratio) + "× average" : ""}
                    {bucket.error > 0 ? " · " + String(bucket.error) + " failed" : ""}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
        {hidden > 0 ? (
          <p className="trd-none">{"and " + String(hidden) + " smaller"}</p>
        ) : null}
        <p className="trd-rule">
          {"A volume spike is a " + unit + " more than two standard deviations above the average, with at least 3 calls. A failure spike is 3 or more failures making up at least 30% of the " + unit + "’s calls."}
          {weekday !== null
            ? " Busiest weekday: " + weekday.name + " (" + oneDecimal(weekday.mean) + " calls on average)."
            : ""}
          {hourly ? " Hours are in your local time." : " Days are counted in UTC."}
        </p>
      </section>

      <details
        className="trd-table"
        onToggle={function toggled(event) {
          setShowNumbers((event.currentTarget as HTMLDetailsElement).open);
        }}
      >
        <summary>Show the numbers</summary>
        {showNumbers ? (
          <div className="trd-table-scroll">
            <table>
              <caption>{"Calls per " + unit + ", most recent last"}</caption>
              <thead>
                <tr>
                  <th scope="col">{hourly ? "Hour" : "Day"}</th>
                  <th scope="col">Succeeded</th>
                  <th scope="col">Failed</th>
                  <th scope="col">Total</th>
                </tr>
              </thead>
              <tbody>
                {buckets.map(function row(bucket, i) {
                  return (
                    <tr key={bucket.key}>
                      <th scope="row">{long[i]}</th>
                      <td>{bucket.ok}</td>
                      <td>{bucket.error}</td>
                      <td>{bucket.total}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </details>
    </figure>
  );
}
