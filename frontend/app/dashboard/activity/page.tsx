"use client";

/**
 * The full activity log: every MCP tool call this workspace has made.
 *
 * Four tabs, because one scroll held four different questions badly:
 *
 *   Live        the log itself, refreshing while you watch it
 *   Errors      only what failed, with the message each failed with
 *   Connectors  which connector and which tool, over the last 24 hours
 *   Trends      the year calendar and the spike chart
 *
 * Everything comes from endpoints that already existed and are tenant-scoped
 * by the server -- GET /api/activity/, /api/activity/summary/ and
 * /api/activity/live/. Nothing here is invented.
 *
 * LIVE. The rows refresh on their own timer while the tab is visible and the
 * stream is not paused. That is a deliberate change from this page's old rule
 * of "refresh when the tab is focused": a log you are watching for failures is
 * the one case where a timer earns its keep. It stops dead when the browser
 * tab is hidden, so a forgotten tab costs nothing, and Pause stops it for
 * someone reading a row that keeps sliding away.
 *
 * The timer runs on every tab, not just Live, because the failure alerts are
 * driven by the same rows -- being on Trends must not mean missing an alert.
 *
 * The summary counts and the row list stay separate. They answer different
 * questions, they fail independently, and a failed count must not blank a log
 * that loaded fine.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CalendarDays,
  Pause,
  Play,
  Radio,
} from "lucide-react";
import ActivityAlerts from "@/components/dashboard/ActivityAlerts";
import ActivityCalendar from "@/components/dashboard/ActivityCalendar";
import ActivityTrend from "@/components/dashboard/ActivityTrend";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import EmptyState from "@/components/dashboard/EmptyState";
import { useLive } from "@/components/dashboard/LiveProvider";
import LoadingScreen from "@/components/ui/LoadingScreen";
import { listActivity } from "@/lib/api";
import type { ActivityEvent, LiveShare } from "@/lib/api";
import "@/components/dashboard/activity-tabs.css";

/** The server clamps this to 100; asking for its maximum is the point here. */
const EVENT_LIMIT = 100;

/** How often the log refreshes itself. Matches the live panel's cadence. */
const STREAM_EVERY_MS = 5000;

const EVENTS_FAILED = "The activity log could not be loaded.";
const SUMMARY_FAILED = "The activity counts could not be loaded.";

type TabKey = "live" | "errors" | "connectors" | "trends";

interface TabDef {
  key: TabKey;
  label: string;
  icon: typeof Activity;
}

const TABS: TabDef[] = [
  { key: "live", label: "Live", icon: Radio },
  { key: "errors", label: "Errors", icon: AlertTriangle },
  { key: "connectors", label: "Connectors", icon: BarChart3 },
  { key: "trends", label: "Trends", icon: CalendarDays },
];

function count(n: number, one: string, many: string): string {
  return String(n) + " " + (n === 1 ? one : many);
}

function relativeTime(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) {
    return iso;
  }
  const seconds = Math.floor((Date.now() - when.getTime()) / 1000);
  // A clock a few seconds ahead of the server is not the future.
  if (seconds < 45) {
    return "just now";
  }
  // Rounded, not floored: flooring reports 45-59 seconds as "0 mins ago",
  // which is the most likely row on a live log -- the call you just watched.
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 60) {
    return count(minutes, "min ago", "mins ago");
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return count(hours, "hour ago", "hours ago");
  }
  return count(Math.floor(hours / 24), "day ago", "days ago");
}

function absoluteTime(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
}

/** One row of the log. Shared by the Live and Errors tabs, which differ only in what they are given. */
function EventRow({ row }: { row: ActivityEvent }) {
  const failed = row.status !== "ok";
  return (
    <li className="conn-row ov-event" key={row.id}>
      <ConnectorMark
        slug={row.connector}
        label={row.connector_label || row.connector}
        size={26}
      />
      <div className="conn-row-body">
        <p className="conn-row-title">
          <code className="conn-tool-name">{row.tool_name}</code>
          <span
            className={failed ? "conn-status conn-status-error" : "conn-status"}
          >
            <span className="conn-status-dot" aria-hidden="true" />
            <span>{failed ? "Error" : "OK"}</span>
          </span>
        </p>
        <p className="conn-row-meta">
          {/* Null once the connection has been deleted, which is the case
              McpActivity.connection SET_NULL exists to preserve. The
              connector's label is what is left to name the row by. */}
          {row.connection_name !== null && row.connection_name.length > 0
            ? row.connection_name
            : row.connector_label}
          {" · "}
          <time
            className="ov-event-time"
            dateTime={row.created_at}
            title={absoluteTime(row.created_at)}
          >
            {relativeTime(row.created_at)}
          </time>
          {/* Absent for a call that failed before it could be timed, and an
              invented 0 ms would be a lie. */}
          {row.duration_ms !== null
            ? " · " + String(row.duration_ms) + " ms"
            : ""}
        </p>
        {failed && row.error_message.length > 0 ? (
          <p className="conn-row-error">{row.error_message}</p>
        ) : null}
      </div>
    </li>
  );
}

/**
 * One connector's or one tool's 24 hours.
 *
 * The bar is the share of this row's calls that failed, not its share of all
 * calls: a connector with four calls and three failures is the one worth
 * looking at, and sizing by volume would bury it under a healthy one.
 */
function ShareRow({ share }: { share: LiveShare }) {
  const failRate = share.calls > 0 ? share.error / share.calls : 0;
  const pct = Math.round(failRate * 100);
  return (
    <li className="act-share">
      <ConnectorMark
        slug={share.connector}
        label={share.connector_label || share.connector}
        size={22}
      />
      <div className="act-share-body">
        <p className="act-share-title">
          {typeof share.tool_name === "string" && share.tool_name.length > 0 ? (
            <code className="conn-tool-name">{share.tool_name}</code>
          ) : (
            <span>{share.connector_label || share.connector}</span>
          )}
          <span className="act-share-counts">
            {count(share.calls, "call", "calls")}
            {share.avg_ms !== null
              ? " · " + String(Math.round(share.avg_ms)) + " ms avg"
              : ""}
          </span>
        </p>
        <div
          className="act-bar"
          role="img"
          aria-label={
            share.error === 0
              ? "No failures"
              : String(pct) + "% of calls failed"
          }
        >
          <span
            className={share.error > 0 ? "act-bar-fill act-bar-bad" : "act-bar-fill"}
            style={{ width: String(share.error > 0 ? Math.max(pct, 3) : 0) + "%" }}
          />
        </div>
        <p className="act-share-meta">
          {share.error === 0
            ? "No failures"
            : count(share.error, "failure", "failures") + " · " + String(pct) + "%"}
        </p>
      </div>
    </li>
  );
}

export default function ActivityPage() {
  // null is "not loaded yet"; [] is "the log is empty". Two different facts
  // that render two different things, so they get two different values rather
  // than one array that starts empty and lies for a frame.
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  // The year of counts and the last 24 hours come from the live provider, which
  // the panel beside this page shares. They fail on their own, separately from
  // the rows below.
  const { summary, summaryError, live } = useLive();
  const [tab, setTab] = useState<TabKey>("live");
  const [paused, setPaused] = useState<boolean>(false);
  const [lastAt, setLastAt] = useState<number | null>(null);

  const aliveRef = useRef<boolean>(true);

  const load = useCallback(function load(): void {
    listActivity(EVENT_LIMIT)
      .then(function received(rows: ActivityEvent[]) {
        if (aliveRef.current) {
          setEvents(rows);
          setEventsError(null);
          setLastAt(Date.now());
        }
      })
      .catch(function failed(caught: unknown) {
        if (aliveRef.current) {
          setEventsError(caught instanceof Error ? caught.message : EVENTS_FAILED);
        }
      });
  }, []);

  useEffect(
    function loadOnMount() {
      aliveRef.current = true;
      load();
      return function unmount() {
        aliveRef.current = false;
      };
    },
    [load]
  );

  /* The stream. Runs while the page is visible and not paused, and is torn
     down entirely otherwise -- a hidden tab holds no timer at all, which is
     the rule the live panel follows for the same reason. */
  useEffect(
    function stream() {
      if (paused) {
        return;
      }

      let timer: number | null = null;

      function start(): void {
        if (timer === null) {
          timer = window.setInterval(load, STREAM_EVERY_MS);
        }
      }
      function stop(): void {
        if (timer !== null) {
          window.clearInterval(timer);
          timer = null;
        }
      }
      function onVisibility(): void {
        if (document.visibilityState === "hidden") {
          stop();
        } else {
          // Catch up immediately rather than waiting out a whole interval on
          // a log that may have moved a lot while the tab was away.
          load();
          start();
        }
      }

      if (document.visibilityState !== "hidden") {
        start();
      }
      document.addEventListener("visibilitychange", onVisibility);
      return function unbind() {
        stop();
        document.removeEventListener("visibilitychange", onVisibility);
      };
    },
    [load, paused]
  );

  const failures = useMemo(
    function onlyFailures(): ActivityEvent[] {
      return (events || []).filter(function failed(row: ActivityEvent) {
        return row.status !== "ok";
      });
    },
    [events]
  );

  const counts: Record<TabKey, number | null> = {
    live: events === null ? null : events.length,
    errors: events === null ? null : failures.length,
    connectors: live === null ? null : live.connectors.length,
    trends: null,
  };

  function renderRows(rows: ActivityEvent[], emptyTitle: string, emptyBody: string) {
    if (eventsError !== null) {
      return (
        <p className="error" role="alert">
          {eventsError}
        </p>
      );
    }
    if (events === null) {
      return <LoadingScreen label="Loading activity" />;
    }
    if (rows.length === 0) {
      return (
        <EmptyState icon={Activity} title={emptyTitle} description={emptyBody} />
      );
    }
    return (
      <ul className="conn-list ov-events">
        {rows.map(function renderEvent(row: ActivityEvent) {
          return <EventRow row={row} key={row.id} />;
        })}
      </ul>
    );
  }

  return (
    <div className="panel">
      <h1 className="panel-title">Activity</h1>
      <p className="panel-lede">
        Every tool call an AI client has made through this workspace, newest
        first.
      </p>

      <div className="panel-body">
        <div className="act-toolbar">
          <div
            className="act-tabs"
            role="tablist"
            aria-label="Activity views"
          >
            {TABS.map(function renderTab(def: TabDef) {
              const Icon = def.icon;
              const n = counts[def.key];
              return (
                <button
                  key={def.key}
                  type="button"
                  role="tab"
                  id={"act-tab-" + def.key}
                  aria-selected={tab === def.key}
                  aria-controls={"act-panel-" + def.key}
                  className={
                    tab === def.key ? "act-tab act-tab-on" : "act-tab"
                  }
                  onClick={function choose() {
                    setTab(def.key);
                  }}
                >
                  <Icon size={15} strokeWidth={2.2} aria-hidden="true" />
                  {def.label}
                  {n !== null && n > 0 ? (
                    <span className="mkt-shelf-count">{n}</span>
                  ) : null}
                </button>
              );
            })}
          </div>

          <div className="act-stream-state">
            <span
              className={paused ? "act-pulse act-pulse-off" : "act-pulse"}
              aria-hidden="true"
            />
            <span className="act-stream-label">
              {paused
                ? "Paused"
                : lastAt === null
                  ? "Connecting"
                  : "Live"}
            </span>
            <button
              type="button"
              className="mkt-chip act-pause"
              aria-pressed={paused}
              onClick={function togglePause() {
                setPaused(function flip(was: boolean) {
                  return !was;
                });
              }}
            >
              {paused ? (
                <Play size={14} strokeWidth={2.2} aria-hidden="true" />
              ) : (
                <Pause size={14} strokeWidth={2.2} aria-hidden="true" />
              )}
              {paused ? "Resume" : "Pause"}
            </button>
          </div>
        </div>

        {/* Mounted on every tab, not just Errors: it is what fires the desktop
            alerts, and it must keep watching while you read the charts. */}
        <ActivityAlerts failures={failures} />

        <div
          role="tabpanel"
          id="act-panel-live"
          aria-labelledby="act-tab-live"
          hidden={tab !== "live"}
        >
          {renderRows(
            events || [],
            "No calls yet",
            "Paste an MCP URL into Claude or another AI client, and every tool call it makes shows up here."
          )}
        </div>

        <div
          role="tabpanel"
          id="act-panel-errors"
          aria-labelledby="act-tab-errors"
          hidden={tab !== "errors"}
        >
          {renderRows(
            failures,
            "Nothing has failed",
            "Every call in the window above succeeded. Failures appear here the moment one happens."
          )}
        </div>

        <div
          role="tabpanel"
          id="act-panel-connectors"
          aria-labelledby="act-tab-connectors"
          hidden={tab !== "connectors"}
        >
          {live === null ? (
            <LoadingScreen label="Loading the last 24 hours" />
          ) : live.connectors.length === 0 ? (
            <EmptyState
              icon={BarChart3}
              title="Nothing in the last 24 hours"
              description="This view covers the last day only. The Trends tab goes back a year."
            />
          ) : (
            <div className="act-breakdown">
              <section>
                <h2 className="act-section-title">By connector</h2>
                <ul className="act-shares">
                  {live.connectors.map(function byConnector(share: LiveShare) {
                    return <ShareRow share={share} key={share.connector} />;
                  })}
                </ul>
              </section>
              {live.tools.length > 0 ? (
                <section>
                  <h2 className="act-section-title">By tool</h2>
                  <ul className="act-shares">
                    {live.tools.map(function byTool(share: LiveShare) {
                      return (
                        <ShareRow
                          share={share}
                          key={share.connector + "/" + String(share.tool_name)}
                        />
                      );
                    })}
                  </ul>
                </section>
              ) : null}
            </div>
          )}
        </div>

        <div
          role="tabpanel"
          id="act-panel-trends"
          aria-labelledby="act-tab-trends"
          hidden={tab !== "trends"}
        >
          {summary === null && summaryError ? (
            <p className="error" role="alert">
              {SUMMARY_FAILED}
            </p>
          ) : summary === null ? (
            <LoadingScreen label="Loading trends" />
          ) : (
            /* Both size themselves to the pane, so neither can grow tall
               enough to push the rest below the fold. */
            <div className="act-trend">
              <ActivityCalendar summary={summary} />
              <ActivityTrend summary={summary} live={live} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
