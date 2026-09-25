"use client";

/**
 * The live panel: a column docked beside every dashboard page that stays open
 * while you move around, showing what is being called right now.
 *
 * It replaces the bell's popover, which vanished on the first click outside
 * and only ever refreshed when opened. This one stays put, refreshes every
 * few seconds while it is open and the tab is visible, and answers the three
 * questions the popover could not: how much is being called, how fast, and
 * what is failing.
 *
 * It invents nothing. Every figure is counted by the server from tool-call
 * rows (see ActivityLiveView) and every row is one of them. The four tiles are
 * absent until the first snapshot lands, because "0 calls" and "not loaded
 * yet" are different claims, and an empty workspace shows an empty state, not
 * sample traffic.
 *
 * "Live" is honest about what it is: the server writes a call's row when the
 * call finishes, so this is a feed of completed calls arriving within a few
 * seconds, not a view of calls still in flight. The status line says how
 * often it refreshes rather than implying a socket that does not exist.
 *
 * Wide screens dock it beside the page; narrower ones slide it over the page
 * with a scrim, because a 340px column beside a 700px page leaves neither
 * usable.
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Check, PlugZap, X } from "lucide-react";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import { LIVE_EVERY_MS, useLive } from "@/components/dashboard/LiveProvider";
import {
  buildProblems,
  formatMs,
  hourLabel,
  relativeTime,
} from "@/components/dashboard/live-model";
import type { ProblemRow } from "@/components/dashboard/live-model";
import { plural } from "@/components/dashboard/spikes";
import type { ActivityEvent, LiveHour, LiveShare } from "@/lib/api";
import "@/components/dashboard/live-panel.css";

type Tab = "live" | "problems" | "breakdown";

/** How long a newly arrived row keeps its highlight. */
const FRESH_MS = 2400;

/** The id the bell points aria-controls at, and returns focus to. */
export const LIVE_PANEL_ID = "live-panel";
export const LIVE_TOGGLE_ID = "live-toggle";

/* ------------------------------------------------------------------ */
/* Last 24 hours, as bars                                              */
/* ------------------------------------------------------------------ */

const BAR_W = 288;
const BAR_H = 52;
const BAR_GAP = 2;

function HourBars({ hours }: { hours: LiveHour[] }) {
  const n = hours.length;
  if (n === 0) {
    return null;
  }
  const bar = (BAR_W - BAR_GAP * (n - 1)) / n;
  let peak = 0;
  let peakAt = -1;
  hours.forEach(function find(hour, index) {
    const total = hour.ok + hour.error;
    if (total > peak) {
      peak = total;
      peakAt = index;
    }
  });
  const scale = Math.max(1, peak);

  return (
    <figure className="lp-bars">
      <svg
        viewBox={"0 0 " + String(BAR_W) + " " + String(BAR_H)}
        role="img"
        aria-label={
          peak === 0
            ? "No calls in the last 24 hours."
            : "Calls per hour over the last 24 hours. The busiest hour was " +
              hourLabel(hours[peakAt].start) +
              " with " +
              plural(peak, "call", "calls") +
              "."
        }
      >
        {hours.map(function draw(hour, index) {
          const total = hour.ok + hour.error;
          const x = index * (bar + BAR_GAP);
          const full = total === 0 ? 0 : Math.max(2, (total / scale) * (BAR_H - 2));
          const errH = total === 0 ? 0 : (full * hour.error) / total;
          const okH = full - errH;
          return (
            <g key={hour.start} className={index === peakAt ? "is-peak" : undefined}>
              <title>
                {hourLabel(hour.start) +
                  " — " +
                  plural(total, "call", "calls") +
                  (hour.error > 0 ? ", " + String(hour.error) + " failed" : "")}
              </title>
              {/* A quiet hour still gets a hairline, so the rhythm of the
                  day reads and an empty stretch does not look like a gap. */}
              <rect
                className="lp-bar-idle"
                x={x}
                y={BAR_H - 2}
                width={bar}
                height={2}
                rx={1}
              />
              {okH > 0 ? (
                <rect
                  className="lp-bar-ok"
                  x={x}
                  y={BAR_H - okH}
                  width={bar}
                  height={okH}
                  rx={1.5}
                />
              ) : null}
              {errH > 0 ? (
                <rect
                  className="lp-bar-bad"
                  x={x}
                  y={BAR_H - okH - errH}
                  width={bar}
                  height={errH}
                  rx={1.5}
                />
              ) : null}
            </g>
          );
        })}
      </svg>
      <figcaption className="lp-bars-cap">
        <span>24 h ago</span>
        <span>
          {peak > 0
            ? "Peak " + plural(peak, "call", "calls") + " at " + hourLabel(hours[peakAt].start)
            : "Quiet"}
        </span>
        <span>Now</span>
      </figcaption>
    </figure>
  );
}

/* ------------------------------------------------------------------ */
/* Rows                                                                */
/* ------------------------------------------------------------------ */

function CallRow({
  event,
  now,
  fresh,
}: {
  event: ActivityEvent;
  now: number;
  fresh: boolean;
}) {
  const failed = event.status !== "ok";
  const href =
    event.connection === null ? null : "/dashboard/connectors/" + event.connector;
  const label = event.connector_label || event.connector;

  const body = (
    <>
      <span className="lp-mark">
        <ConnectorMark slug={event.connector} label={label} size={26} />
        {failed ? (
          <span className="lp-flag" aria-hidden="true">
            <AlertTriangle size={9} strokeWidth={2.6} />
          </span>
        ) : null}
      </span>
      <span className="lp-body">
        <span className="lp-tool">{event.tool_name}</span>
        <span className="lp-sub">
          {failed
            ? event.error_message.trim() || "The call failed without a message."
            : event.connection_name || label}
        </span>
      </span>
      <span className="lp-meta">
        <span className="lp-ms">{failed ? "Failed" : formatMs(event.duration_ms)}</span>
        <span className="lp-when">{relativeTime(event.created_at, now)}</span>
      </span>
    </>
  );

  const className =
    "lp-row" + (failed ? " is-error" : "") + (fresh ? " is-new" : "");

  return (
    <li className="lp-cell">
      {href === null ? (
        <span className={className} title={label}>
          {body}
        </span>
      ) : (
        <Link className={className} href={href} title={label}>
          {body}
        </Link>
      )}
    </li>
  );
}

function ProblemItem({ row, now }: { row: ProblemRow; now: number }) {
  const body = (
    <>
      <span className="lp-mark">
        <ConnectorMark slug={row.connector} label={row.connectorLabel} size={26} />
        <span className="lp-flag" aria-hidden="true">
          <AlertTriangle size={9} strokeWidth={2.6} />
        </span>
      </span>
      <span className="lp-body">
        <span className="lp-tool">{row.title}</span>
        <span className="lp-sub">{row.detail}</span>
      </span>
      <span className="lp-meta">
        <span className="lp-ms">{row.kind === "connection" ? "Down" : "Failed"}</span>
        <span className="lp-when">{relativeTime(row.at, now)}</span>
      </span>
    </>
  );
  return (
    <li className="lp-cell">
      {row.href === null ? (
        <span className="lp-row is-error" title={row.connectorLabel}>
          {body}
        </span>
      ) : (
        <Link className="lp-row is-error" href={row.href} title={row.connectorLabel}>
          {body}
        </Link>
      )}
    </li>
  );
}

/** A connector's or tool's share of the window, as a two-tone bar. */
function ShareRow({ share, top }: { share: LiveShare; top: number }) {
  const width = top === 0 ? 0 : Math.max(4, (share.calls / top) * 100);
  const okWidth = share.calls === 0 ? 0 : (share.ok / share.calls) * 100;
  const isTool = share.tool_name !== undefined;
  return (
    <li className="lp-share">
      <span className="lp-share-head">
        {isTool ? null : (
          <ConnectorMark
            slug={share.connector}
            label={share.connector_label}
            size={22}
          />
        )}
        <span className="lp-share-name">
          {isTool ? share.tool_name : share.connector_label}
          {isTool ? (
            <span className="lp-share-of"> {share.connector_label}</span>
          ) : null}
        </span>
        <span className="lp-share-n">{share.calls}</span>
      </span>
      <span className="lp-share-track" aria-hidden="true">
        <span className="lp-share-fill" style={{ width: String(width) + "%" }}>
          <span className="lp-share-ok" style={{ width: String(okWidth) + "%" }} />
        </span>
      </span>
      <span className="lp-share-foot">
        <span>{formatMs(share.avg_ms)} avg</span>
        <span className={share.error > 0 ? "is-bad" : undefined}>
          {share.error > 0 ? plural(share.error, "failure", "failures") : "no failures"}
        </span>
      </span>
    </li>
  );
}

/* ------------------------------------------------------------------ */
/* The panel                                                           */
/* ------------------------------------------------------------------ */

export default function LivePanel() {
  const {
    open,
    setOpen,
    live,
    liveError,
    connections,
    connectionsError,
    polling,
    updatedAt,
  } = useLive();

  const [tab, setTab] = useState<Tab>("live");
  const [now, setNow] = useState<number>(function start() {
    return Date.now();
  });
  const [fresh, setFresh] = useState<Record<number, boolean>>({});

  const seenRef = useRef<Set<number> | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const restoredRef = useRef<boolean>(open);

  // A clock, so "12 s ago" keeps counting between snapshots. Only while open.
  useEffect(
    function tickClock() {
      if (!open) {
        return;
      }
      setNow(Date.now());
      const timer = window.setInterval(function tick() {
        setNow(Date.now());
      }, 1000);
      return function stop() {
        window.clearInterval(timer);
      };
    },
    [open]
  );

  // Mark rows that arrived since the last snapshot. The first snapshot marks
  // nothing: a panel that opens with thirty rows flashing is noise, not news.
  useEffect(
    function markFresh() {
      if (live === null) {
        return;
      }
      const ids = live.recent.map(function id(event) {
        return event.id;
      });
      const before = seenRef.current;
      seenRef.current = new Set(ids);
      if (before === null) {
        return;
      }
      const marks: Record<number, boolean> = {};
      let any = false;
      ids.forEach(function check(id) {
        if (!before.has(id)) {
          marks[id] = true;
          any = true;
        }
      });
      if (!any) {
        return;
      }
      setFresh(marks);
      const timer = window.setTimeout(function clear() {
        setFresh({});
      }, FRESH_MS);
      return function cancel() {
        window.clearTimeout(timer);
      };
    },
    [live]
  );

  // Move focus into the panel when it is opened on purpose, so a keyboard user
  // is not left tabbing through the whole page to reach it. Not when it was
  // restored on load -- that would steal focus from wherever the page put it.
  useEffect(
    function focusOnOpen() {
      if (!open) {
        return;
      }
      if (restoredRef.current) {
        restoredRef.current = false;
        return;
      }
      panelRef.current?.focus({ preventScroll: true });
    },
    [open]
  );

  function close(): void {
    setOpen(false);
    document.getElementById(LIVE_TOGGLE_ID)?.focus();
  }

  if (!open) {
    return null;
  }

  const problems = buildProblems(connections, live);
  const recent = live === null ? [] : live.recent;
  const currentHour =
    live === null || live.hours.length === 0
      ? null
      : live.hours[live.hours.length - 1];
  const topCalls = live === null ? 0 : live.connectors.reduce(function most(best, row) {
    return row.calls > best ? row.calls : best;
  }, 0);
  const topToolCalls = live === null ? 0 : live.tools.reduce(function most(best, row) {
    return row.calls > best ? row.calls : best;
  }, 0);

  let status = "Live";
  let statusNote = "refreshes every " + String(LIVE_EVERY_MS / 1000) + " s";
  let statusTone = "is-live";
  if (liveError) {
    status = "Reconnecting";
    statusNote = "the last snapshot is still shown";
    statusTone = "is-bad";
  } else if (!polling) {
    status = "Paused";
    statusNote = "tab in the background";
    statusTone = "is-paused";
  }

  return (
    <>
      <button
        type="button"
        className="lp-scrim"
        aria-label="Close the live panel"
        tabIndex={-1}
        onClick={close}
      />
      <aside
        id={LIVE_PANEL_ID}
        className="lp-panel"
        aria-label="Live activity"
        tabIndex={-1}
        ref={panelRef}
        onKeyDown={function onKey(event) {
          if (event.key === "Escape") {
            event.stopPropagation();
            close();
          }
        }}
      >
        <header className="lp-head">
          <div className="lp-title-row">
            <h2 className="lp-title">Live activity</h2>
            <button
              type="button"
              className="lp-close"
              aria-label="Close the live panel"
              title="Close"
              onClick={close}
            >
              <X size={16} strokeWidth={2} aria-hidden="true" />
            </button>
          </div>
          <p className={"lp-status " + statusTone} role="status">
            <span className="lp-dot" aria-hidden="true" />
            <b>{status}</b>
            <span className="lp-status-note">
              {"· " + statusNote}
              {updatedAt !== null && !liveError
                ? " · updated " + relativeTime(new Date(updatedAt).toISOString(), now)
                : ""}
            </span>
          </p>
        </header>

        <div className="lp-scroll">
          {live === null ? (
            <p className="lp-empty">
              {liveError ? "Live activity could not be loaded." : "Loading…"}
            </p>
          ) : (
            <>
              <ul className="lp-tiles">
                <li className="lp-tile">
                  <span className="lp-tile-value">{live.calls}</span>
                  <span className="lp-tile-label">Calls · 24 h</span>
                </li>
                <li className={live.errors > 0 ? "lp-tile is-bad" : "lp-tile"}>
                  <span className="lp-tile-value">{live.errors}</span>
                  <span className="lp-tile-label">
                    {live.calls > 0
                      ? "Failed · " +
                        String(Math.round((live.errors / live.calls) * 100)) +
                        "%"
                      : "Failed"}
                  </span>
                </li>
                <li className="lp-tile">
                  <span className="lp-tile-value">{formatMs(live.avg_ms)}</span>
                  <span className="lp-tile-label">Avg time</span>
                </li>
                <li className="lp-tile">
                  <span className="lp-tile-value">
                    {currentHour === null ? 0 : currentHour.ok + currentHour.error}
                  </span>
                  <span className="lp-tile-label">This hour</span>
                </li>
              </ul>

              <HourBars hours={live.hours} />

              <div className="lp-tabs" role="group" aria-label="Panel view">
                <button
                  type="button"
                  className="lp-tab"
                  aria-pressed={tab === "live"}
                  onClick={function pickLive() {
                    setTab("live");
                  }}
                >
                  Live feed
                </button>
                <button
                  type="button"
                  className="lp-tab"
                  aria-pressed={tab === "problems"}
                  onClick={function pickProblems() {
                    setTab("problems");
                  }}
                >
                  Problems
                  {problems.length > 0 ? (
                    <span className="lp-tab-count">{problems.length}</span>
                  ) : null}
                </button>
                <button
                  type="button"
                  className="lp-tab"
                  aria-pressed={tab === "breakdown"}
                  onClick={function pickBreakdown() {
                    setTab("breakdown");
                  }}
                >
                  Breakdown
                </button>
              </div>

              {tab === "live" ? (
                recent.length === 0 ? (
                  <div className="lp-empty">
                    <PlugZap size={18} strokeWidth={2} aria-hidden="true" />
                    <span>
                      No tool calls yet. Calls made through your MCP URLs appear
                      here as they finish.
                    </span>
                  </div>
                ) : (
                  <>
                    <ul className="lp-list" aria-live="off">
                      {recent.map(function row(event) {
                        return (
                          <CallRow
                            key={event.id}
                            event={event}
                            now={now}
                            fresh={fresh[event.id] === true}
                          />
                        );
                      })}
                    </ul>
                    <p className="lp-note">
                      Latest {recent.length} calls. The full log is on the
                      Activity page.
                    </p>
                  </>
                )
              ) : null}

              {tab === "problems" ? (
                <>
                  {connectionsError ? (
                    <p className="lp-error" role="alert">
                      Connection status could not be loaded.
                    </p>
                  ) : null}
                  {problems.length === 0 ? (
                    <div className="lp-empty">
                      <Check size={18} strokeWidth={2} aria-hidden="true" />
                      <span>Nothing needs attention.</span>
                    </div>
                  ) : (
                    <ul className="lp-list">
                      {problems.map(function item(row) {
                        return <ProblemItem key={row.key} row={row} now={now} />;
                      })}
                    </ul>
                  )}
                </>
              ) : null}

              {tab === "breakdown" ? (
                live.connectors.length === 0 ? (
                  <p className="lp-empty">Nothing was called in the last 24 hours.</p>
                ) : (
                  <>
                    <h3 className="lp-section">By connector · last 24 h</h3>
                    <ul className="lp-shares">
                      {live.connectors.map(function share(row) {
                        return (
                          <ShareRow key={row.connector} share={row} top={topCalls} />
                        );
                      })}
                    </ul>
                    <h3 className="lp-section">Most-called tools</h3>
                    <ul className="lp-shares">
                      {live.tools.map(function share(row) {
                        return (
                          <ShareRow
                            key={row.connector + "/" + String(row.tool_name)}
                            share={row}
                            top={topToolCalls}
                          />
                        );
                      })}
                    </ul>
                  </>
                )
              ) : null}
            </>
          )}
        </div>

        <Link className="lp-foot" href="/dashboard/activity">
          Open activity
        </Link>
      </aside>
    </>
  );
}
