"use client";

/**
 * A year of calls as a calendar of days, laid out like GitHub's contribution
 * graph: one column per week, one row per weekday, and a green that deepens
 * with the day's volume.
 *
 * What it adds to that familiar shape, because a call log has things a commit
 * history does not:
 *
 *   - a red dot on any day that had failures, so a bad day is visible at a
 *     glance even when it was also a busy one;
 *   - a dark outline on a SPIKE day, using the same rule as the trend chart
 *     under it (see spikes.ts), so the two never disagree about which days
 *     stood out;
 *   - a tooltip with the day's real numbers, and a line under the title with
 *     the active days, the longest streak and the busiest day.
 *
 * The scale is relative to this workspace's own busiest day, like GitHub's:
 * the five greens mean "none, a little, some, a lot, the most" and the
 * tooltip says the actual count. A day with any call at all is at least the
 * first green -- rounding a real call down to "none" would say nothing
 * happened.
 *
 * Nothing is drawn for days the API did not return, and days after today are
 * left empty rather than painted as zeros. The number of weeks shown is
 * whatever fits the width, newest on the right, so a narrow pane shows recent
 * weeks in full instead of shrinking a year into unreadable specks.
 *
 * Days are UTC days; that is how the server counts them and the caption says
 * so rather than pretending to a local midnight it does not have.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { dayLabel } from "@/components/dashboard/live-model";
import { analyse, plural, toBucket } from "@/components/dashboard/spikes";
import type { ActivityDay, ActivitySummary } from "@/lib/api";
import "@/components/dashboard/activity-charts.css";

/* Cells grow to fill the card, like GitHub's do in a wide container, between a
   floor that stays readable and a ceiling past which they stop looking like a
   calendar. The gap is fixed: it is the grid, and it should not scale. */
const GAP = 3;
const MIN_STEP = 14;
const MAX_STEP = 26;
const LABEL_W = 30;
const MONTH_H = 18;
const MIN_WEEKS = 8;
const FALLBACK_W = 860;

const WEEKDAYS = ["", "Mon", "", "Wed", "", "Fri", ""];
const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

type Level = 0 | 1 | 2 | 3 | 4;

interface Cell {
  iso: string;
  ok: number;
  error: number;
  total: number;
  level: Level;
  /** Index into the day list, which is also into the analysis buckets. */
  index: number;
  spike: boolean;
  peak: boolean;
}

interface Cursor {
  col: number;
  row: number;
}

function toLevel(total: number, max: number): Level {
  if (total <= 0 || max <= 0) {
    return 0;
  }
  return Math.min(4, Math.max(1, Math.ceil((total / max) * 4))) as Level;
}

/** Sunday-first weekday of an ISO date, read as UTC so no timezone shifts it. */
function weekdayOf(iso: string): number {
  return new Date(iso + "T00:00:00Z").getUTCDay();
}

function monthOf(iso: string): number {
  return new Date(iso + "T00:00:00Z").getUTCMonth();
}

/** Lays days out into weeks of seven slots; a slot is null before the first day and after the last. */
function toWeeks(days: ActivityDay[], max: number, spikes: Set<number>, peakIndex: number): Array<Array<Cell | null>> {
  if (days.length === 0) {
    return [];
  }
  const lead = weekdayOf(days[0].date);
  const weeks: Array<Array<Cell | null>> = [];
  days.forEach(function place(day, i) {
    const slot = lead + i;
    const col = Math.floor(slot / 7);
    const row = slot % 7;
    while (weeks.length <= col) {
      weeks.push([null, null, null, null, null, null, null]);
    }
    const total = day.ok + day.error;
    weeks[col][row] = {
      iso: day.date,
      ok: day.ok,
      error: day.error,
      total: total,
      level: toLevel(total, max),
      index: i,
      spike: spikes.has(i),
      peak: i === peakIndex,
    };
  });
  return weeks;
}

function cellSummary(cell: Cell): string {
  const when = dayLabel(cell.iso, true);
  if (cell.total === 0) {
    return "No calls on " + when;
  }
  return (
    plural(cell.total, "call", "calls") +
    " on " +
    when +
    ", " +
    String(cell.ok) +
    " succeeded, " +
    String(cell.error) +
    " failed" +
    (cell.spike ? ". A spike day." : ".")
  );
}

export default function ActivityCalendar({ summary }: { summary: ActivitySummary }) {
  const frame = useRef<HTMLDivElement | null>(null);
  const [boxW, setBoxW] = useState<number>(FALLBACK_W);
  const [cursor, setCursor] = useState<Cursor | null>(null);
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

  const model = useMemo(
    function build() {
      const days = summary.days;
      const analysis = analyse(
        days.map(function bucket(day) {
          return toBucket(day.date, day.ok, day.error);
        })
      );
      const spikes = new Set<number>(
        analysis.spikes.map(function index(spike) {
          return spike.index;
        })
      );
      const weeks = toWeeks(days, analysis.peak, spikes, analysis.peakIndex);
      return { days: days, analysis: analysis, weeks: weeks };
    },
    [summary]
  );

  const allWeeks = model.weeks;
  if (allWeeks.length === 0) {
    return null;
  }

  // Size the cells so the whole year fills the width; when even the smallest
  // cell cannot fit a year, show as many recent weeks as do, newest on the right.
  const room = boxW - LABEL_W - 4;
  const step = Math.max(MIN_STEP, Math.min(MAX_STEP, Math.floor(room / allWeeks.length)));
  const cell = step - GAP;
  const cols = Math.max(MIN_WEEKS, Math.min(allWeeks.length, Math.floor(room / step)));
  const weeks = allWeeks.slice(allWeeks.length - cols);
  const width = LABEL_W + cols * step;
  const height = MONTH_H + 7 * step;
  const round = Math.max(2, Math.round(cell * 0.18));
  const dotR = Math.max(2.4, cell * 0.16);

  // Everything the header says is counted over the days actually on screen.
  let shownTotal = 0;
  let shownActive = 0;
  let streak = 0;
  let longest = 0;
  let busiest: Cell | null = null;
  // Plain loops, not forEach: `busiest` is assigned inside them, and TypeScript
  // does not follow an assignment made in a callback.
  for (const week of weeks) {
    for (const cell of week) {
      if (cell === null) {
        continue;
      }
      shownTotal += cell.total;
      if (cell.total > 0) {
        shownActive += 1;
        streak += 1;
        longest = Math.max(longest, streak);
        if (busiest === null || cell.total > busiest.total) {
          busiest = cell;
        }
      } else {
        streak = 0;
      }
    }
  }

  // A month label sits over the first column of a month, and is dropped when it
  // would land within three columns of the previous one.
  const months: Array<{ col: number; label: string }> = [];
  let lastMonth = -1;
  let lastAt = -10;
  weeks.forEach(function label(week, col) {
    const first = week.find(function real(cell) {
      return cell !== null;
    });
    if (first === undefined || first === null) {
      return;
    }
    const month = monthOf(first.iso);
    if (month !== lastMonth) {
      if (col - lastAt >= 3) {
        months.push({ col: col, label: MONTHS[month] });
        lastAt = col;
      }
      lastMonth = month;
    }
  });

  const active: Cell | null =
    cursor === null || cursor.col >= weeks.length
      ? null
      : weeks[cursor.col][cursor.row];

  function locate(event: React.PointerEvent<SVGSVGElement>): Cursor | null {
    const box = event.currentTarget.getBoundingClientRect();
    const col = Math.floor((event.clientX - box.left - LABEL_W) / step);
    const row = Math.floor((event.clientY - box.top - MONTH_H) / step);
    if (col < 0 || col >= weeks.length || row < 0 || row > 6) {
      return null;
    }
    return weeks[col][row] === null ? null : { col: col, row: row };
  }

  function onKey(event: React.KeyboardEvent<SVGSVGElement>): void {
    const keys: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    };
    if (event.key === "Escape") {
      setCursor(null);
      return;
    }
    const step = keys[event.key];
    if (step === undefined) {
      return;
    }
    event.preventDefault();
    setCursor(function move(current) {
      if (current === null) {
        // Start on today: the newest real cell.
        const lastCol = weeks.length - 1;
        for (let row = 6; row >= 0; row -= 1) {
          if (weeks[lastCol][row] !== null) {
            return { col: lastCol, row: row };
          }
        }
        return null;
      }
      const col = current.col + step[0];
      const row = current.row + step[1];
      if (col < 0 || col >= weeks.length || row < 0 || row > 6) {
        return current;
      }
      return weeks[col][row] === null ? current : { col: col, row: row };
    });
  }

  const tipX = cursor === null ? 0 : LABEL_W + cursor.col * step + cell / 2;
  const tipY = cursor === null ? 0 : MONTH_H + cursor.row * step;
  const tipSide = tipX < 110 ? " is-left" : tipX > width - 110 ? " is-right" : "";

  const activeDays = model.days.filter(function had(day) {
    return day.ok + day.error > 0;
  });

  return (
    <figure className="cal" ref={frame}>
      <figcaption className="cal-head">
        <div className="cal-titles">
          <span className="cal-title">
            {plural(shownTotal, "call", "calls") +
              " in the last " +
              (cols >= 52 ? "year" : plural(cols, "week", "weeks"))}
          </span>
          <span className="cal-sub">
            {plural(shownActive, "active day", "active days")}
            {longest > 1 ? " · longest streak " + plural(longest, "day", "days") : ""}
            {busiest !== null
              ? " · busiest " +
                dayLabel(busiest.iso, false) +
                " (" +
                String(busiest.total) +
                ")"
              : ""}
          </span>
        </div>
        <div className="cal-legend" aria-hidden="true">
          <span className="cal-key">
            <span className="cal-dot" /> Failures
          </span>
          <span className="cal-key">
            <span className="cal-swatch cal-l0 is-spike" /> Spike
          </span>
          <span className="cal-key">
            Less
            <span className="cal-swatch cal-l0" />
            <span className="cal-swatch cal-l1" />
            <span className="cal-swatch cal-l2" />
            <span className="cal-swatch cal-l3" />
            <span className="cal-swatch cal-l4" />
            More
          </span>
        </div>
      </figcaption>

      <div className="cal-scroll">
        <div className="cal-plot" style={{ width: width }}>
          <svg
            className="cal-svg"
            width={width}
            height={height}
            viewBox={"0 0 " + String(width) + " " + String(height)}
            role="img"
            tabIndex={0}
            aria-label={
              plural(shownTotal, "call", "calls") +
              " across " +
              plural(shownActive, "active day", "active days") +
              ". Use the arrow keys to move between days."
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
            {months.map(function month(entry) {
              return (
                <text
                  key={"m" + String(entry.col)}
                  className="cal-label"
                  x={LABEL_W + entry.col * step}
                  y={MONTH_H - 6}
                >
                  {entry.label}
                </text>
              );
            })}

            {WEEKDAYS.map(function weekday(name, row) {
              return name === "" ? null : (
                <text
                  key={"d" + String(row)}
                  className="cal-label"
                  x={0}
                  y={MONTH_H + row * step + cell - 1}
                >
                  {name}
                </text>
              );
            })}

            {weeks.map(function column(week, col) {
              return week.map(function renderCell(entry, row) {
                if (entry === null) {
                  return null;
                }
                const x = LABEL_W + col * step;
                const y = MONTH_H + row * step;
                const isActive = cursor !== null && cursor.col === col && cursor.row === row;
                return (
                  <g key={entry.iso}>
                    <rect
                      className={
                        "cal-cell cal-l" +
                        String(entry.level) +
                        (entry.spike ? " is-spike" : "") +
                        (isActive ? " is-active" : "")
                      }
                      x={x}
                      y={y}
                      width={cell}
                      height={cell}
                      rx={round}
                    />
                    {entry.error > 0 ? (
                      <circle
                        className="cal-bad"
                        cx={x + cell - dotR * 0.8}
                        cy={y + dotR * 0.8}
                        r={dotR}
                      />
                    ) : null}
                  </g>
                );
              });
            })}
          </svg>

          {active !== null ? (
            <div
              className={"cal-tip" + tipSide}
              style={{ left: tipX, top: tipY }}
              role="presentation"
            >
              <span className="cal-tip-day">{dayLabel(active.iso, true)}</span>
              {active.total === 0 ? (
                <span className="cal-tip-row">No calls</span>
              ) : (
                <>
                  <span className="cal-tip-row">
                    <b>{plural(active.total, "call", "calls")}</b>
                  </span>
                  <span className="cal-tip-row">
                    {String(active.ok) + " succeeded"}
                    {active.error > 0 ? (
                      <span className="cal-tip-bad">
                        {" · " + String(active.error) + " failed"}
                      </span>
                    ) : null}
                  </span>
                  {active.spike ? (
                    <span className="cal-tip-flag">Spike day</span>
                  ) : active.peak ? (
                    <span className="cal-tip-flag">Busiest day</span>
                  ) : null}
                </>
              )}
            </div>
          ) : null}
        </div>
      </div>

      {/* Read out as the arrow keys move, so the chart is not only for eyes. */}
      <p className="cal-sr" aria-live="polite">
        {active === null ? "" : cellSummary(active)}
      </p>

      <p className="cal-foot">Days are counted in UTC. Darker means more calls.</p>

      <details
        className="cal-table"
        onToggle={function toggled(event) {
          setShowNumbers((event.currentTarget as HTMLDetailsElement).open);
        }}
      >
        <summary>Show the numbers</summary>
        {showNumbers ? (
          activeDays.length === 0 ? (
            <p className="cal-none">No calls in this window yet.</p>
          ) : (
            <div className="cal-table-scroll">
              <table>
                <caption>Days with calls, most recent last</caption>
                <thead>
                  <tr>
                    <th scope="col">Day</th>
                    <th scope="col">Succeeded</th>
                    <th scope="col">Failed</th>
                  </tr>
                </thead>
                <tbody>
                  {activeDays.map(function row(day) {
                    return (
                      <tr key={day.date}>
                        <th scope="row">{dayLabel(day.date, true)}</th>
                        <td>{day.ok}</td>
                        <td>{day.error}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        ) : null}
      </details>
    </figure>
  );
}
