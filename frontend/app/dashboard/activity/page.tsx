"use client";

/**
 * The full activity log: every MCP tool call this workspace has made.
 *
 * The Overview shows the newest handful of these beside everything else it
 * has to fit. This page is the one that shows the log itself, so it asks for
 * the server's maximum rather than a preview slice, and adds the filter the
 * Overview has no room for.
 *
 * Both halves come from endpoints that already existed and are tenant-scoped
 * by the server -- GET /api/activity/ and /api/activity/summary/. Nothing
 * here is invented: this page was a hardcoded "No activity yet" empty state,
 * which said the workspace was quiet whether or not it was.
 *
 * The summary counts and the row list are fetched separately and kept
 * separate. They answer different questions, they fail independently, and a
 * failed count must not blank a log that loaded fine.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, AlertTriangle } from "lucide-react";
import ActivityCalendar from "@/components/dashboard/ActivityCalendar";
import ActivityTrend from "@/components/dashboard/ActivityTrend";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import EmptyState from "@/components/dashboard/EmptyState";
import { useLive } from "@/components/dashboard/LiveProvider";
import LoadingScreen from "@/components/ui/LoadingScreen";
import { listActivity } from "@/lib/api";
import type { ActivityEvent } from "@/lib/api";

/** The server clamps this to 100; asking for its maximum is the point here. */
const EVENT_LIMIT = 100;

const EVENTS_FAILED = "The activity log could not be loaded.";
const SUMMARY_FAILED = "The activity counts could not be loaded.";

type Filter = "all" | "errors";

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
  const [filter, setFilter] = useState<Filter>("all");

  const aliveRef = useRef<boolean>(true);

  const load = useCallback(function load(): void {
    listActivity(EVENT_LIMIT)
      .then(function received(rows: ActivityEvent[]) {
        if (aliveRef.current) {
          setEvents(rows);
          setEventsError(null);
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

  /* Refreshed when the tab comes back, never on a timer -- the same rule the
     Overview follows. A log nobody is looking at should not cost a request a
     minute for the life of the tab. */
  useEffect(
    function refreshOnFocus() {
      function onFocus(): void {
        if (document.visibilityState !== "hidden") {
          load();
        }
      }
      window.addEventListener("focus", onFocus);
      document.addEventListener("visibilitychange", onFocus);
      return function unbind() {
        window.removeEventListener("focus", onFocus);
        document.removeEventListener("visibilitychange", onFocus);
      };
    },
    [load]
  );

  const errorCount = useMemo(
    function countErrors(): number {
      return (events || []).filter(function failed(row: ActivityEvent) {
        return row.status !== "ok";
      }).length;
    },
    [events]
  );

  const visible = useMemo(
    function applyFilter(): ActivityEvent[] {
      const rows = events || [];
      return filter === "errors"
        ? rows.filter(function failed(row: ActivityEvent) {
            return row.status !== "ok";
          })
        : rows;
    },
    [events, filter]
  );

  return (
    <div className="panel">
      <h1 className="panel-title">Activity</h1>
      <p className="panel-lede">
        Every tool call an AI client has made through this workspace, newest
        first.
      </p>

      <div className="panel-body">
        {summary === null && summaryError ? (
          <p className="error" role="alert">
            {SUMMARY_FAILED}
          </p>
        ) : summary !== null ? (
          /* The calendar and the spike trend, above the log they summarise.
             Both size themselves to the pane, so neither can grow tall enough
             to push the rows below the fold. */
          <div className="act-trend">
            <ActivityCalendar summary={summary} />
            <ActivityTrend summary={summary} live={live} />
          </div>
        ) : null}

        {eventsError !== null ? (
          <p className="error" role="alert">
            {eventsError}
          </p>
        ) : events === null ? (
          <LoadingScreen label="Loading activity" />
        ) : events.length === 0 ? (
          <EmptyState
            icon={Activity}
            title="No calls yet"
            description="Paste an MCP URL into Claude or another AI client, and every tool call it makes shows up here."
          />
        ) : (
          <>
            {/* Only when there is something to filter to. A lone "Errors 0"
                chip beside "All" answers a question nobody asked. */}
            {errorCount > 0 ? (
              <div
                className="mkt-chips act-filter"
                role="group"
                aria-label="Filter activity"
              >
                <button
                  type="button"
                  className="mkt-chip"
                  aria-pressed={filter === "all"}
                  onClick={function showAll() {
                    setFilter("all");
                  }}
                >
                  All
                  <span className="mkt-shelf-count">{events.length}</span>
                </button>
                <button
                  type="button"
                  className="mkt-chip"
                  aria-pressed={filter === "errors"}
                  onClick={function showErrors() {
                    setFilter("errors");
                  }}
                >
                  <AlertTriangle size={15} strokeWidth={2.2} aria-hidden="true" />
                  Errors
                  <span className="mkt-shelf-count">{errorCount}</span>
                </button>
              </div>
            ) : null}

            <ul className="conn-list ov-events">
              {visible.map(function renderEvent(row: ActivityEvent) {
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
                          className={
                            failed
                              ? "conn-status conn-status-error"
                              : "conn-status"
                          }
                        >
                          <span className="conn-status-dot" aria-hidden="true" />
                          <span>{failed ? "Error" : "OK"}</span>
                        </span>
                      </p>
                      <p className="conn-row-meta">
                        {/* Null once the connection has been deleted, which is
                            the case McpActivity.connection SET_NULL exists to
                            preserve. The connector's label is what is left to
                            name the row by. */}
                        {row.connection_name !== null &&
                        row.connection_name.length > 0
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
                        {/* Absent for a call that failed before it could be
                            timed, and an invented 0 ms would be a lie. */}
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
              })}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
