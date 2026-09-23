"use client";

/**
 * The top bar's notification panel: what is currently broken, and the log of
 * recent tool calls behind it.
 *
 * It invents nothing and it stores nothing. Both lists are derived from two
 * endpoints the dashboard already calls:
 *
 *   GET /api/connections/  -> a connection carries status and last_error, so a
 *                             connection sitting in "error" IS the problem,
 *                             not a notification about one.
 *   GET /api/activity/     -> McpActivity rows, tenant-scoped by the server.
 *                             status is "ok" or "error"; the error ones are
 *                             failed tool calls, all of them together are the
 *                             log.
 *
 * There is deliberately no read/unread state. Marking a notification read
 * needs somewhere to keep the mark -- a new table and endpoint, or browser
 * storage -- and both would be a second source of truth about whether
 * something is wrong. The badge counts the problems that exist RIGHT NOW, so
 * it falls on its own the moment a connection is fixed, and there is nothing
 * to get out of step with the thing it describes.
 *
 * Refreshes on open and on window focus, never on a timer. That is the
 * convention the Overview already follows, and it keeps a bell that nobody
 * opens from spending a request a minute for the life of the tab.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Bell, Check, PlugZap } from "lucide-react";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import { listActivity, listConnections } from "@/lib/api";
import type { ActivityEvent, Connection } from "@/lib/api";

/** How many activity rows the log shows. The server clamps this to 100. */
const EVENT_LIMIT = 30;

/** A refetch inside this window reuses what is already on screen. */
const STALE_AFTER_MS = 15000;

const LOAD_FAILED = "Notifications could not be loaded.";

type Tab = "problems" | "log";

/**
 * One row in either list.
 *
 * `href` is null when the thing it describes can no longer be opened -- the
 * server nulls an activity row's connection when that connection is deleted,
 * and a row that links to a connection that is gone is worse than one that
 * does not link at all.
 */
interface Item {
  key: string;
  failed: boolean;
  title: string;
  detail: string;
  at: string | null;
  href: string | null;
  /** Drives the connector mark, which is what makes a row scannable. */
  connector: string;
  connectorLabel: string;
}

/**
 * Rows split into "Today" and "Earlier".
 *
 * Both lists are already newest-first from the server, so this only has to
 * find the boundary rather than sort. A heading is emitted only for a group
 * that has rows in it -- an empty "Earlier" label is a lie about the shape of
 * the data.
 */
function groupByDay(items: Item[]): Array<{ label: string; rows: Item[] }> {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const midnight = start.getTime();

  const today: Item[] = [];
  const earlier: Item[] = [];
  for (const item of items) {
    const when = item.at === null ? NaN : new Date(item.at).getTime();
    if (!Number.isNaN(when) && when >= midnight) {
      today.push(item);
    } else {
      earlier.push(item);
    }
  }

  const groups: Array<{ label: string; rows: Item[] }> = [];
  if (today.length > 0) {
    groups.push({ label: "Today", rows: today });
  }
  if (earlier.length > 0) {
    groups.push({ label: "Earlier", rows: earlier });
  }
  return groups;
}

function whenLabel(iso: string | null): string {
  if (iso === null) {
    return "";
  }
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) {
    return "";
  }
  const secs = Math.round((Date.now() - when.getTime()) / 1000);
  if (secs < 60) {
    return "just now";
  }
  const mins = Math.round(secs / 60);
  if (mins < 60) {
    return mins + (mins === 1 ? " min ago" : " mins ago");
  }
  const hours = Math.round(mins / 60);
  if (hours < 24) {
    return hours + (hours === 1 ? " hr ago" : " hrs ago");
  }
  return when.toLocaleDateString();
}

/** A connection sitting in "error" is a problem that is still true. */
function connectionProblems(rows: Connection[]): Item[] {
  return rows
    .filter(function broken(row: Connection) {
      return row.status === "error";
    })
    .map(function toItem(row: Connection): Item {
      return {
        key: "connection:" + String(row.id),
        failed: true,
        title: row.name.trim() || row.connector_label,
        detail:
          row.last_error.trim() ||
          "This connection reported an error and has stopped working.",
        at: row.updated_at,
        href: "/dashboard/connectors/" + row.connector,
        connector: row.connector,
        connectorLabel: row.connector_label,
      };
    });
}

function eventItem(row: ActivityEvent): Item {
  const failed = row.status !== "ok";
  return {
    key: "event:" + String(row.id),
    failed: failed,
    title: row.tool_name,
    detail: failed
      ? row.error_message.trim() || "The call failed without a message."
      : (row.connection_name || row.connector_label) +
        (typeof row.duration_ms === "number"
          ? " · " + String(row.duration_ms) + " ms"
          : ""),
    at: row.created_at,
    href:
      row.connection === null
        ? null
        : "/dashboard/connectors/" + row.connector,
    connector: row.connector,
    connectorLabel: row.connector_label || row.connector,
  };
}

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("problems");

  // null means "not loaded yet", which is a different thing from an empty
  // list and must not render as "nothing is wrong".
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const fetchedAtRef = useRef<number>(0);
  const aliveRef = useRef<boolean>(true);

  const load = useCallback(function load(force: boolean): void {
    const now = Date.now();
    if (!force && now - fetchedAtRef.current < STALE_AFTER_MS) {
      return;
    }
    fetchedAtRef.current = now;

    // Separate catches: a connections failure must not blank the log, and a
    // log failure must not hide a connection that is actually broken.
    listConnections()
      .then(function received(rows: Connection[]) {
        if (aliveRef.current) {
          setConnections(rows);
        }
      })
      .catch(function failed() {
        if (aliveRef.current) {
          setError(LOAD_FAILED);
        }
      });

    listActivity(EVENT_LIMIT)
      .then(function received(rows: ActivityEvent[]) {
        if (aliveRef.current) {
          setEvents(rows);
        }
      })
      .catch(function failed() {
        if (aliveRef.current) {
          setError(LOAD_FAILED);
        }
      });
  }, []);

  useEffect(
    function loadOnMount() {
      aliveRef.current = true;
      load(true);
      return function unmount() {
        aliveRef.current = false;
      };
    },
    [load]
  );

  useEffect(
    function refreshOnFocus() {
      function onFocus(): void {
        if (document.visibilityState !== "hidden") {
          load(false);
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

  /* Dismissal. Pointerdown rather than click, so the panel is gone before the
     thing underneath reacts -- a click listener lets the first press outside
     land on whatever it hit AND close the panel, which reads as a misfire. */
  useEffect(
    function dismiss() {
      if (!open) {
        return;
      }
      function onPointerDown(event: PointerEvent): void {
        const root = rootRef.current;
        if (root !== null && !root.contains(event.target as Node)) {
          setOpen(false);
        }
      }
      function onKeyDown(event: KeyboardEvent): void {
        if (event.key === "Escape") {
          setOpen(false);
          buttonRef.current?.focus();
        }
      }
      document.addEventListener("pointerdown", onPointerDown);
      document.addEventListener("keydown", onKeyDown);
      return function unbind() {
        document.removeEventListener("pointerdown", onPointerDown);
        document.removeEventListener("keydown", onKeyDown);
      };
    },
    [open]
  );

  const problems = (connections === null ? [] : connectionProblems(connections))
    .concat(
      (events === null ? [] : events)
        .filter(function onlyFailed(row: ActivityEvent) {
          return row.status !== "ok";
        })
        .map(eventItem)
    );

  const log = (events === null ? [] : events).map(eventItem);

  // The badge is exactly the length of the Problems list, so the number on the
  // bell and the rows behind it can never disagree.
  const count = problems.length;
  const loaded = connections !== null && events !== null;
  const items = tab === "problems" ? problems : log;

  function toggle(): void {
    const next = !open;
    setOpen(next);
    if (next) {
      load(false);
    }
  }

  return (
    <div className="dash-notif" ref={rootRef}>
      <button
        type="button"
        ref={buttonRef}
        className="dash-notif-button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={
          count > 0
            ? "Notifications, " +
              String(count) +
              (count === 1 ? " problem" : " problems")
            : "Notifications"
        }
        title="Notifications"
        onClick={toggle}
      >
        <Bell size={18} strokeWidth={1.9} aria-hidden="true" />
        {count > 0 ? (
          <span className="dash-notif-badge" aria-hidden="true">
            {count > 99 ? "99+" : count}
          </span>
        ) : null}
      </button>

      {open ? (
        <div className="dash-notif-panel" role="dialog" aria-label="Notifications">
          {/* Sticky, so the tabs stay reachable once the log is long enough
              to scroll. */}
          <div className="dash-notif-head">
            <p className="dash-notif-heading">
              Notifications
              {count > 0 ? (
                <span className="dash-notif-heading-count">
                  {count} {count === 1 ? "problem" : "problems"}
                </span>
              ) : null}
            </p>
            <div className="dash-notif-tabs" role="group" aria-label="Filter">
              <button
                type="button"
                className="dash-notif-tab"
                aria-pressed={tab === "problems"}
                onClick={function pickProblems() {
                  setTab("problems");
                }}
              >
                Problems
                {count > 0 ? (
                  <span className="dash-notif-tab-count">{count}</span>
                ) : null}
              </button>
              <button
                type="button"
                className="dash-notif-tab"
                aria-pressed={tab === "log"}
                onClick={function pickLog() {
                  setTab("log");
                }}
              >
                Log
              </button>
            </div>
          </div>

          {error !== null ? (
            <p className="dash-notif-error" role="alert">
              {error}
            </p>
          ) : null}

          {!loaded && error === null ? (
            <p className="dash-notif-empty">Loading…</p>
          ) : items.length === 0 ? (
            <div className="dash-notif-empty">
              {tab === "problems" ? (
                <>
                  <Check size={18} strokeWidth={2} aria-hidden="true" />
                  <span>Nothing needs attention.</span>
                </>
              ) : (
                <>
                  <PlugZap size={18} strokeWidth={2} aria-hidden="true" />
                  <span>No tool calls recorded yet.</span>
                </>
              )}
            </div>
          ) : (
            <div className="dash-notif-scroll" data-tab={tab}>
              {groupByDay(items).map(function renderGroup(group) {
                return (
                  <section className="dash-notif-group" key={group.label}>
                    <h3 className="dash-notif-group-title">{group.label}</h3>
                    <ul className="dash-notif-list">
                      {group.rows.map(function renderItem(item: Item) {
                        const body = (
                          <>
                            {/* The connector mark, not a status dot: with
                                eighteen connectors in the catalogue, "which
                                one broke" is the first thing you need and a
                                dot cannot say it. The failure flag rides on
                                the corner of the mark instead. */}
                            <span className="dash-notif-mark">
                              <ConnectorMark
                                slug={item.connector}
                                label={item.connectorLabel}
                                size={28}
                              />
                              {item.failed ? (
                                <span
                                  className="dash-notif-flag"
                                  aria-hidden="true"
                                >
                                  <AlertTriangle size={9} strokeWidth={2.6} />
                                </span>
                              ) : null}
                            </span>
                            <span className="dash-notif-body">
                              <span className="dash-notif-title">
                                {item.title}
                              </span>
                              <span className="dash-notif-detail">
                                {item.detail}
                              </span>
                            </span>
                            <span className="dash-notif-when">
                              {whenLabel(item.at)}
                            </span>
                          </>
                        );

                        const className = item.failed
                          ? "dash-notif-row is-error"
                          : "dash-notif-row";

                        return (
                          <li className="dash-notif-cell" key={item.key}>
                            {item.href === null ? (
                              <span
                                className={className}
                                title={item.connectorLabel}
                              >
                                {body}
                              </span>
                            ) : (
                              <Link
                                className={className}
                                href={item.href}
                                title={item.connectorLabel}
                                onClick={function close() {
                                  setOpen(false);
                                }}
                              >
                                {body}
                              </Link>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  </section>
                );
              })}
            </div>
          )}

          <Link
            className="dash-notif-foot"
            href="/dashboard/activity"
            onClick={function close() {
              setOpen(false);
            }}
          >
            Open activity
          </Link>
        </div>
      ) : null}
    </div>
  );
}
