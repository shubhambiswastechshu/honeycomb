"use client";

/**
 * Overview: what is wrong, what is happening, what you are serving.
 *
 * Three things stay above the tabs because they are true whichever tab is
 * open: the counts, anything that is failing, and -- only while the workspace
 * is new -- the steps that get the first call to land. Everything else sits in
 * five tabs, each answering one question:
 *
 *   Overview     how much is being called, and is it working?
 *   Activity     what exactly happened, and why did calls fail?
 *   Endpoints    every MCP URL this workspace serves, with its state
 *   Performance  how fast are the calls, and which are slow?
 *   Trends       the year, and the spikes in it
 *
 * Every number on this page is counted from a response that has arrived. There
 * is no health score, no quota bar and no sample row, and nothing renders a
 * figure while its own request is still in flight: a tile reading 0 mid-request
 * is a wrong answer wearing the costume of a loading state. Each request owns
 * its own slice of the page, so one endpoint being down costs the part that
 * needed it and nothing else.
 *
 * The open tab is kept in the URL's hash, so a refresh or a shared link lands
 * on the same view.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import Link from "next/link";
import {
  Activity,
  CalendarDays,
  Check,
  Database,
  Gauge,
  LayoutDashboard,
  TriangleAlert,
} from "lucide-react";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import EmptyState from "@/components/dashboard/EmptyState";
import ActivityCalendar from "@/components/dashboard/ActivityCalendar";
import ActivityTrend from "@/components/dashboard/ActivityTrend";
import McpUrl from "@/components/dashboard/McpUrl";
import PanelCover from "@/components/dashboard/PanelCover";
import { useLive } from "@/components/dashboard/LiveProvider";
import {
  dayLabel,
  formatMs,
  tailSummary,
} from "@/components/dashboard/live-model";
import { useSession } from "@/components/dashboard/SessionProvider";
import {
  CallsLegend,
  ShareBars,
  StackedColumns,
  ValueBars,
} from "@/components/dashboard/overview/charts";
import type {
  ColumnPoint,
  ShareRow,
  ValueRow,
} from "@/components/dashboard/overview/charts";
import { listActivity, listConnections } from "@/lib/api";
import type { ActivityEvent, Connection, LiveShare } from "@/lib/api";
import "@/components/dashboard/activity-tabs.css";
import "@/components/dashboard/overview/overview.css";

/**
 * How many calls to fetch. The server's maximum: the Activity tab lists the
 * newest of them, and the failure report and the response-time percentiles are
 * computed over all of them.
 */
const EVENT_LIMIT = 100;

/** How many calls the Activity tab lists before handing off to the full log. */
const LOG_ROWS = 25;

/** The window the 30-day figures and chart cover. */
const SPARK_DAYS = 30;

/** Rows in each 24-hour ranking. The server returns its own top slice. */
const TOP_ROWS = 8;

const CONNECTIONS_ERROR = "Could not load your connections.";
const ACTIVITY_ERROR = "Could not load recent activity.";

type TabKey = "overview" | "activity" | "endpoints" | "performance" | "trends";

interface TabDef {
  key: TabKey;
  label: string;
  icon: typeof Activity;
}

const TABS: TabDef[] = [
  { key: "overview", label: "Overview", icon: LayoutDashboard },
  { key: "activity", label: "Activity", icon: Activity },
  { key: "endpoints", label: "Endpoints", icon: Database },
  { key: "performance", label: "Performance", icon: Gauge },
  { key: "trends", label: "Trends", icon: CalendarDays },
];

function isTab(value: string): value is TabKey {
  return TABS.some(function match(t) {
    return t.key === value;
  });
}

/** "1 tool" / "3 tools" -- never a bare number with no noun. */
function count(n: number, one: string, many: string): string {
  return n.toLocaleString() + " " + (n === 1 ? one : many);
}

/** The name a connection shows when its owner left the field blank. */
function connectionTitle(row: Connection): string {
  const name = row.name.trim();
  return name.length > 0 ? name : row.connector_label;
}

/** The name a call is shown under: its connection, or what is left once that is deleted. */
function eventSource(row: ActivityEvent): string {
  return row.connection_name !== null && row.connection_name.length > 0
    ? row.connection_name
    : row.connector_label || row.connector;
}

/**
 * "4 mins ago" for a timestamp.
 *
 * Past a week the relative form stops meaning anything, so it falls back to
 * the date, and an unparseable value is returned untouched rather than
 * rendered as "NaN days ago".
 */
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
  // Rounded, not floored. Flooring reports 45-59 seconds as "0 mins ago",
  // which is the most likely row on a live dashboard.
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 60) {
    return count(minutes, "min ago", "mins ago");
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return count(hours, "hour ago", "hours ago");
  }
  const days = Math.floor(hours / 24);
  if (days === 1) {
    return "yesterday";
  }
  if (days < 7) {
    return String(days) + " days ago";
  }
  return when.toLocaleDateString();
}

/** The full timestamp, for the title of a relative one. */
function absoluteTime(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
}

/** The first word of a name, so the greeting is a greeting and not a record. */
function firstName(fullName: string): string {
  const trimmed = fullName.trim();
  return trimmed.length === 0 ? "" : trimmed.split(/\s+/)[0];
}

/**
 * The value at a percentile of an already-sorted list, by nearest rank.
 *
 * Nearest rank rather than interpolation: every figure it returns is a call
 * that actually took that long, so "p95 1.8 s" names a real call.
 */
function percentile(sorted: number[], p: number): number {
  const rank = Math.ceil((p / 100) * sorted.length);
  return sorted[Math.min(sorted.length - 1, Math.max(0, rank - 1))];
}

/* ------------------------------------------------------------------ */
/* Getting started                                                     */
/* ------------------------------------------------------------------ */

export interface StartStepProps {
  index: number;
  done: boolean;
  title: string;
  text: string;
  /** The page that completes the step, when there is one to link to yet. */
  href?: string;
  linkLabel?: string;
}

function StartStep({ index, done, title, text, href, linkLabel }: StartStepProps) {
  return (
    <li className={done ? "ov-step is-done" : "ov-step"}>
      <span className="ov-step-mark" aria-hidden="true">
        {done ? <Check size={14} strokeWidth={2.4} /> : String(index)}
      </span>
      <div className="ov-step-body">
        <p className="ov-step-title">
          {title}
          {/* The tick is decorative, so the state is spelled out for a reader
              who cannot see it. */}
          <span className="dash-visually-hidden">
            {done ? " — done" : " — not done yet"}
          </span>
        </p>
        <p className="ov-step-text">{text}</p>
        {!done && href !== undefined ? (
          <Link className="ov-step-link" href={href}>
            {linkLabel !== undefined ? linkLabel : "Open"}
          </Link>
        ) : null}
      </div>
    </li>
  );
}

/** One call in the log. */
function EventRow({ row }: { row: ActivityEvent }) {
  const failed = row.status !== "ok";
  return (
    <li className="conn-row ov-event">
      <ConnectorMark
        slug={row.connector}
        label={row.connector_label || row.connector}
        size={26}
      />
      <div className="conn-row-body">
        <p className="conn-row-title">
          <code className="conn-tool-name">{row.tool_name}</code>
          <span className={failed ? "conn-status conn-status-error" : "conn-status"}>
            <span className="conn-status-dot" aria-hidden="true" />
            <span>{failed ? "Error" : "OK"}</span>
          </span>
        </p>
        <p className="conn-row-meta">
          {eventSource(row)}
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
          {row.duration_ms !== null ? " · " + formatMs(row.duration_ms) : ""}
        </p>
        {failed && row.error_message.length > 0 ? (
          <p className="conn-row-error">{row.error_message}</p>
        ) : null}
      </div>
    </li>
  );
}

/** A connector's or tool's 24 hours, as a row the share chart can draw. */
function shareRow(share: LiveShare, byTool: boolean): ShareRow {
  const connector = share.connector_label || share.connector;
  return {
    key: byTool ? share.connector + "/" + String(share.tool_name) : share.connector,
    label: byTool && share.tool_name !== undefined ? share.tool_name : connector,
    sub: byTool ? connector : undefined,
    mark: <ConnectorMark slug={share.connector} label={connector} size={20} />,
    ok: share.ok,
    failed: share.error,
  };
}

/* ------------------------------------------------------------------ */
/* The page                                                            */
/* ------------------------------------------------------------------ */

export default function OverviewPage() {
  const { session } = useSession();

  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabKey>("overview");
  const [logFilter, setLogFilter] = useState<"all" | "failed">("all");

  // A year of per-day counts and the last-24-hours snapshot come from the live
  // provider, which the panel beside this page shares: one fetch, one answer.
  const { summary: history, live, liveError } = useLive();
  const month = useMemo(
    function thirtyDays() {
      return history === null ? null : tailSummary(history, SPARK_DAYS);
    },
    [history]
  );

  // The tab comes from the hash, so a refresh or a shared link keeps it.
  useEffect(function readHash() {
    const fromHash = window.location.hash.replace(/^#/, "");
    if (isTab(fromHash)) {
      setTab(fromHash);
    }
  }, []);

  const choose = useCallback(function choose(next: TabKey): void {
    setTab(next);
    try {
      window.history.replaceState(null, "", "#" + next);
    } catch {
      /* A sandboxed frame may refuse; the tab still changes. */
    }
  }, []);

  // A dashboard is a tab people leave open. Refetching when the tab is looked
  // at again keeps the relative times and "today" honest. Not a poll: a tab
  // nobody is looking at has nothing to be stale for.
  const load = useCallback(function load(alive: () => boolean): Promise<void> {
    // Each request settles into its own slice of state, so one endpoint
    // answering 500 cannot blank the parts of the page that loaded fine.
    return Promise.all([
      listConnections()
        .then(function apply(rows: Connection[]) {
          if (alive()) {
            setConnections(rows);
            setConnectionsError(null);
          }
        })
        .catch(function fail() {
          if (alive()) {
            setConnectionsError(CONNECTIONS_ERROR);
          }
        }),
      listActivity(EVENT_LIMIT)
        .then(function apply(rows: ActivityEvent[]) {
          if (alive()) {
            setEvents(rows);
            setEventsError(null);
          }
        })
        .catch(function fail() {
          if (alive()) {
            setEventsError(ACTIVITY_ERROR);
          }
        }),
    ]).then(function done() {
      return undefined;
    });
  }, []);

  useEffect(
    function loadAndRefresh() {
      let mounted = true;
      function alive(): boolean {
        return mounted;
      }
      void load(alive);
      function onWake(): void {
        if (document.visibilityState === "visible") {
          void load(alive);
        }
      }
      document.addEventListener("visibilitychange", onWake);
      window.addEventListener("focus", onWake);
      return function stop() {
        mounted = false;
        document.removeEventListener("visibilitychange", onWake);
        window.removeEventListener("focus", onWake);
      };
    },
    [load]
  );

  /* ---------------- derived figures ---------------- */

  const failing =
    connections === null
      ? null
      : connections.filter(function isDown(row) {
          return row.status === "error";
        });

  const totals =
    connections === null
      ? null
      : {
          connections: connections.length,
          tools: connections.reduce(function add(sum, row) {
            return sum + (row.tool_count - row.disabled_tools.length);
          }, 0),
          keys: connections.reduce(function add(sum, row) {
            return sum + row.key_count;
          }, 0),
          errors: failing === null ? 0 : failing.length,
        };

  // Read off the server's own sums rather than re-added from the day buckets:
  // the number over a chart must not disagree with the chart.
  const monthTotals =
    month === null
      ? null
      : {
          calls: month.total,
          failed: month.errors,
          // Whole percent: two decimals over a handful of calls is precision
          // the number does not have.
          failRate: month.total > 0 ? Math.round((month.errors / month.total) * 100) : 0,
        };

  const hourPoints: ColumnPoint[] = useMemo(
    function hours() {
      if (live === null) {
        return [];
      }
      return live.hours.map(function point(h) {
        return {
          key: h.start,
          // "1 PM" rather than "01:00 pm": half the width, so twice as many
          // hours get a label before they would collide.
          label: new Date(h.start).toLocaleTimeString([], { hour: "numeric" }),
          full: new Date(h.start).toLocaleString([], {
            weekday: "short",
            hour: "2-digit",
            minute: "2-digit",
          }),
          ok: h.ok,
          failed: h.error,
        };
      });
    },
    [live]
  );

  const dayPoints: ColumnPoint[] = useMemo(
    function days() {
      if (month === null) {
        return [];
      }
      return month.days.map(function point(d) {
        const when = new Date(d.date + "T00:00:00Z");
        return {
          key: d.date,
          label: Number.isNaN(when.getTime())
            ? d.date
            : when.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }),
          full: dayLabel(d.date, true),
          ok: d.ok,
          failed: d.error,
        };
      });
    },
    [month]
  );

  const connectorRows: ShareRow[] =
    live === null
      ? []
      : live.connectors.slice(0, TOP_ROWS).map(function row(s) {
          return shareRow(s, false);
        });

  const toolRows: ShareRow[] =
    live === null
      ? []
      : live.tools.slice(0, TOP_ROWS).map(function row(s) {
          return shareRow(s, true);
        });

  const latencyRows: ValueRow[] =
    live === null
      ? []
      : live.connectors
          .filter(function timed(s) {
            return s.avg_ms !== null;
          })
          .sort(function slowestFirst(a, b) {
            return (b.avg_ms || 0) - (a.avg_ms || 0);
          })
          .slice(0, TOP_ROWS)
          .map(function row(s) {
            const connector = s.connector_label || s.connector;
            return {
              key: s.connector,
              label: connector,
              mark: <ConnectorMark slug={s.connector} label={connector} size={20} />,
              sub: count(s.calls, "call", "calls"),
              value: s.avg_ms || 0,
              display: formatMs(s.avg_ms === null ? null : Math.round(s.avg_ms)),
            };
          });

  /** Response-time percentiles over the calls that were actually timed. */
  const timing = useMemo(
    function percentiles() {
      if (events === null) {
        return null;
      }
      const timed = events
        .filter(function hasDuration(e) {
          return e.duration_ms !== null;
        })
        .sort(function slowestFirst(a, b) {
          return (b.duration_ms || 0) - (a.duration_ms || 0);
        });
      const values = timed
        .map(function ms(e) {
          return e.duration_ms || 0;
        })
        .sort(function ascending(a, b) {
          return a - b;
        });
      if (values.length === 0) {
        return { count: 0, p50: 0, p95: 0, max: 0, slowest: [] as ActivityEvent[] };
      }
      return {
        count: values.length,
        p50: percentile(values, 50),
        p95: percentile(values, 95),
        max: values[values.length - 1],
        slowest: timed.slice(0, 5),
      };
    },
    [events]
  );

  /**
   * Why calls failed: the failed calls among the latest fetched, grouped by
   * connector and message so the same fault is one row with a count, not a
   * screenful of identical lines.
   */
  const reasons = useMemo(
    function groupFailures() {
      if (events === null) {
        return null;
      }
      const groups = new Map<
        string,
        { key: string; connector: string; label: string; message: string; count: number; last: string; source: string }
      >();
      events.forEach(function add(e) {
        if (e.status === "ok") {
          return;
        }
        const message = e.error_message.trim().length > 0 ? e.error_message.trim() : "No error message was recorded.";
        const key = e.connector + "\u0000" + message;
        const known = groups.get(key);
        if (known === undefined) {
          groups.set(key, {
            key: key,
            connector: e.connector,
            label: e.connector_label || e.connector,
            message: message,
            count: 1,
            // Events arrive newest first, so the first one seen is the latest.
            last: e.created_at,
            source: eventSource(e),
          });
        } else {
          known.count += 1;
        }
      });
      return Array.from(groups.values()).sort(function mostFirst(a, b) {
        return b.count - a.count;
      });
    },
    [events]
  );

  const failedCount = events === null
    ? 0
    : events.filter(function failed(e) {
        return e.status !== "ok";
      }).length;

  const logRows =
    events === null
      ? []
      : events
          .filter(function keep(e) {
            return logFilter === "all" || e.status !== "ok";
          })
          .slice(0, LOG_ROWS);

  /* ---------------- getting started ---------------- */

  const hasConnections = connections !== null && connections.length > 0;
  const activityKnown = events !== null || history !== null;
  const activitySettled = activityKnown || eventsError !== null;
  const hasActivity =
    (events !== null && events.length > 0) || (history !== null && history.total > 0);
  const stepConnected = hasConnections;
  const stepKeyed =
    connections !== null &&
    connections.some(function keyed(row) {
      return row.key_count > 0;
    });
  const stepCalled = hasActivity;
  // Three real steps, three real facts. It leaves entirely once they are all
  // true, rather than becoming a permanent row of ticks nobody needs again.
  const showStart =
    connections !== null && activitySettled && !(stepConnected && stepKeyed && stepCalled);
  const keyTarget =
    connections !== null && connections.length > 0
      ? "/dashboard/connectors/" + connections[0].connector
      : undefined;

  const greetName = firstName(session.user.full_name);

  /* ---------------- tab keyboard support ---------------- */

  function onTabKey(event: KeyboardEvent<HTMLDivElement>): void {
    const at = TABS.findIndex(function current(t) {
      return t.key === tab;
    });
    let next = -1;
    if (event.key === "ArrowRight") {
      next = (at + 1) % TABS.length;
    } else if (event.key === "ArrowLeft") {
      next = (at - 1 + TABS.length) % TABS.length;
    } else if (event.key === "Home") {
      next = 0;
    } else if (event.key === "End") {
      next = TABS.length - 1;
    }
    if (next >= 0) {
      event.preventDefault();
      choose(TABS[next].key);
      const button = document.getElementById("ov-tab-" + TABS[next].key);
      if (button !== null) {
        button.focus();
      }
    }
  }

  const tabCounts: Record<TabKey, number | null> = {
    overview: null,
    activity: events === null ? null : failedCount,
    endpoints: connections === null ? null : connections.length,
    performance: null,
    trends: null,
  };

  const liveLoading = live === null && !liveError;

  return (
    <div className="panel panel-wide">
      <PanelCover
        title={greetName.length > 0 ? "Welcome, " + greetName : "Welcome"}
        lede={"Here is what is happening in " + session.tenant.name + "."}
      />

      <div className="panel-body ov-stack">
        {connectionsError !== null ? (
          <p className="error" role="alert">
            {connectionsError}
          </p>
        ) : null}

        {/* ---- The counts: true on every tab, so above them ---- */}
        {totals !== null && totals.connections > 0 ? (
          <ul className="data-stats ov-kpis">
            {live !== null ? (
              <li className="data-stat">
                <span className="data-stat-value">{live.calls.toLocaleString()}</span>
                <span className="data-stat-label">Calls · 24h</span>
              </li>
            ) : null}
            {live !== null ? (
              <li className={live.errors > 0 ? "data-stat data-stat-bad" : "data-stat"}>
                <span className="data-stat-value">
                  {live.calls > 0
                    ? String(Math.round(((live.calls - live.errors) / live.calls) * 100)) + "%"
                    : "—"}
                </span>
                <span className="data-stat-label">Success rate · 24h</span>
              </li>
            ) : null}
            {live !== null ? (
              <li className="data-stat">
                <span className="data-stat-value">
                  {formatMs(live.avg_ms === null ? null : Math.round(live.avg_ms))}
                </span>
                <span className="data-stat-label">Avg response · 24h</span>
              </li>
            ) : null}
            {monthTotals !== null ? (
              <li className="data-stat">
                <span className="data-stat-value">{monthTotals.calls.toLocaleString()}</span>
                <span className="data-stat-label">{"Calls · " + String(SPARK_DAYS) + "d"}</span>
              </li>
            ) : null}
            {monthTotals !== null ? (
              <li className={monthTotals.failed > 0 ? "data-stat data-stat-bad" : "data-stat"}>
                <span className="data-stat-value">{String(monthTotals.failRate) + "%"}</span>
                <span className="data-stat-label">{"Failure rate · " + String(SPARK_DAYS) + "d"}</span>
              </li>
            ) : null}
            <li className="data-stat">
              <span className="data-stat-value">{totals.connections}</span>
              <span className="data-stat-label">
                {totals.connections === 1 ? "Connection" : "Connections"}
              </span>
            </li>
            <li className="data-stat">
              <span className="data-stat-value">{totals.tools}</span>
              <span className="data-stat-label">Tools exposed</span>
            </li>
            <li className="data-stat">
              <span className="data-stat-value">{totals.keys}</span>
              <span className="data-stat-label">{totals.keys === 1 ? "Key" : "Keys"}</span>
            </li>
            <li className={totals.errors > 0 ? "data-stat data-stat-bad" : "data-stat"}>
              <span className="data-stat-value">{totals.errors}</span>
              <span className="data-stat-label">
                {totals.errors === 1 ? "Connection down" : "Connections down"}
              </span>
            </li>
          </ul>
        ) : null}

        {/* ---- Needs attention: urgent, so never hidden behind a tab ---- */}
        {failing !== null && failing.length > 0 ? (
          <section className="ov-section">
            <div className="ov-section-head">
              <h2 className="ov-section-title">
                <TriangleAlert size={15} strokeWidth={2} aria-hidden="true" />
                <span>Needs attention</span>
              </h2>
              <span className="mkt-count">
                {count(failing.length, "connection", "connections")}
              </span>
            </div>
            <ul className="conn-list">
              {failing.map(function renderFailure(row: Connection) {
                return (
                  <li className="conn-row ov-attention-row" key={row.id}>
                    <ConnectorMark slug={row.connector} label={row.connector_label || row.connector} />
                    <div className="conn-row-body">
                      <p className="conn-row-title">
                        <Link className="data-row-link" href={"/dashboard/connectors/" + row.connector}>
                          {connectionTitle(row)}
                        </Link>
                        <span className="conn-status conn-status-error">
                          <span className="conn-status-dot" aria-hidden="true" />
                          <span>Error</span>
                        </span>
                      </p>
                      {row.last_error.length > 0 ? (
                        <p className="conn-row-error">{row.last_error}</p>
                      ) : (
                        <p className="conn-row-meta">
                          {row.connector_label} stopped working. Open it to re-enter its credentials.
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}

        {/* ---- Getting started: only while the workspace is new ---- */}
        {showStart ? (
          <section className="ov-section ov-start">
            <h2 className="ov-section-title">Getting started</h2>
            <p className="conn-note">Three steps between here and an AI client calling your data.</p>
            <ol className="ov-steps">
              <StartStep
                index={1}
                done={stepConnected}
                title="Connect a source"
                text="Pick a connector and give it credentials. Honeycomb turns it into an MCP server."
                href="/dashboard/connectors"
                linkLabel="Browse MCPs"
              />
              <StartStep
                index={2}
                done={stepKeyed}
                title="Mint a key"
                text="Each connection needs a key before anything can call it. Open a connection and use its Access tab."
                href={keyTarget}
                linkLabel="Open the connection"
              />
              <StartStep
                index={3}
                done={stepCalled}
                title="Paste the URL into an AI client"
                text="Add the MCP URL and the key to Claude, Cursor or any MCP client. The first call it makes appears here."
                href={hasConnections ? "/dashboard/data" : undefined}
                linkLabel="Get the URL"
              />
            </ol>
          </section>
        ) : null}

        {/* ---- The tabs ---- */}
        <div className="ov-tabs-bar">
          <div
            className="act-tabs"
            role="tablist"
            aria-label="Overview views"
            onKeyDown={onTabKey}
          >
            {TABS.map(function renderTab(def: TabDef) {
              const Icon = def.icon;
              const n = tabCounts[def.key];
              const on = tab === def.key;
              return (
                <button
                  key={def.key}
                  type="button"
                  role="tab"
                  id={"ov-tab-" + def.key}
                  aria-selected={on}
                  aria-controls={"ov-panel-" + def.key}
                  tabIndex={on ? 0 : -1}
                  className={on ? "act-tab act-tab-on" : "act-tab"}
                  onClick={function pick() {
                    choose(def.key);
                  }}
                >
                  <Icon size={15} strokeWidth={2.2} aria-hidden="true" />
                  {def.label}
                  {n !== null && n > 0 ? <span className="mkt-shelf-count">{n}</span> : null}
                </button>
              );
            })}
          </div>
        </div>

        {/* ================= Overview ================= */}
        <div
          role="tabpanel"
          id="ov-panel-overview"
          aria-labelledby="ov-tab-overview"
          hidden={tab !== "overview"}
          className="ov-tabpanel"
          tabIndex={0}
        >
          <div className="ov-cards ov-cards-2">
            <section className="ov-card">
              <div className="ov-card-head">
                <div>
                  <h2 className="ov-card-title">Calls, last 24 hours</h2>
                  <p className="ov-card-meta">One column per hour, in your time zone.</p>
                </div>
                <CallsLegend />
              </div>
              {live !== null ? (
                <StackedColumns
                  points={hourPoints}
                  height={220}
                  caption="Calls per hour over the last 24 hours"
                  firstColumn="Hour"
                />
              ) : (
                <p className="ov-card-empty">
                  {liveLoading ? "Loading the last 24 hours…" : "The last 24 hours could not be loaded."}
                </p>
              )}
            </section>

            <section className="ov-card">
              <div className="ov-card-head">
                <div>
                  <h2 className="ov-card-title">{"Calls, last " + String(SPARK_DAYS) + " days"}</h2>
                  <p className="ov-card-meta">One column per day.</p>
                </div>
                <CallsLegend />
              </div>
              {month !== null ? (
                <StackedColumns
                  points={dayPoints}
                  height={220}
                  caption={"Calls per day over the last " + String(SPARK_DAYS) + " days"}
                  firstColumn="Day"
                />
              ) : (
                <p className="ov-card-empty">Loading the last {SPARK_DAYS} days…</p>
              )}
            </section>

            <section className="ov-card">
              <div className="ov-card-head">
                <div>
                  <h2 className="ov-card-title">Busiest connectors</h2>
                  <p className="ov-card-meta">Calls in the last 24 hours.</p>
                </div>
                {connectorRows.length > 0 ? <CallsLegend /> : null}
              </div>
              {live === null ? (
                <p className="ov-card-empty">{liveLoading ? "Loading…" : "Could not be loaded."}</p>
              ) : connectorRows.length === 0 ? (
                <p className="ov-card-empty">No calls in the last 24 hours.</p>
              ) : (
                <ShareBars rows={connectorRows} caption="Calls per connector, last 24 hours" />
              )}
            </section>

            <section className="ov-card">
              <div className="ov-card-head">
                <div>
                  <h2 className="ov-card-title">Most-used tools</h2>
                  <p className="ov-card-meta">Calls in the last 24 hours.</p>
                </div>
                {toolRows.length > 0 ? <CallsLegend /> : null}
              </div>
              {live === null ? (
                <p className="ov-card-empty">{liveLoading ? "Loading…" : "Could not be loaded."}</p>
              ) : toolRows.length === 0 ? (
                <p className="ov-card-empty">No calls in the last 24 hours.</p>
              ) : (
                <ShareBars rows={toolRows} caption="Calls per tool, last 24 hours" />
              )}
            </section>
          </div>
        </div>

        {/* ================= Activity ================= */}
        <div
          role="tabpanel"
          id="ov-panel-activity"
          aria-labelledby="ov-tab-activity"
          hidden={tab !== "activity"}
          className="ov-tabpanel"
          tabIndex={0}
        >
          <div className="ov-cards ov-cards-2">
            <section className="ov-card">
              <div className="ov-card-head">
                <div>
                  <h2 className="ov-card-title">Recent calls</h2>
                  <p className="ov-card-meta">
                    {"The newest " + String(LOG_ROWS) + ". "}
                    <Link className="ov-section-link" href="/dashboard/activity">
                      Open the full live log
                    </Link>
                  </p>
                </div>
                {failedCount > 0 ? (
                  <div className="mkt-chips act-filter" role="group" aria-label="Filter calls">
                    <button
                      type="button"
                      className="mkt-chip"
                      aria-pressed={logFilter === "all"}
                      onClick={function all() {
                        setLogFilter("all");
                      }}
                    >
                      All
                    </button>
                    <button
                      type="button"
                      className="mkt-chip"
                      aria-pressed={logFilter === "failed"}
                      onClick={function failedOnly() {
                        setLogFilter("failed");
                      }}
                    >
                      <TriangleAlert size={14} strokeWidth={2.2} aria-hidden="true" />
                      Failed
                      <span className="mkt-shelf-count">{failedCount}</span>
                    </button>
                  </div>
                ) : null}
              </div>
              {eventsError !== null ? (
                <p className="error" role="alert">
                  {eventsError}
                </p>
              ) : events === null ? (
                <p className="ov-card-empty">Loading recent calls…</p>
              ) : logRows.length === 0 ? (
                <EmptyState
                  icon={Activity}
                  title={logFilter === "failed" ? "Nothing has failed" : "No calls yet"}
                  description={
                    logFilter === "failed"
                      ? "None of the latest calls failed."
                      : "Paste an MCP URL into Claude or another AI client, and every tool call it makes shows up here."
                  }
                />
              ) : (
                <ul className="conn-list ov-events">
                  {logRows.map(function renderEvent(row) {
                    return <EventRow row={row} key={row.id} />;
                  })}
                </ul>
              )}
            </section>

            <section className="ov-card">
              <div className="ov-card-head">
                <div>
                  <h2 className="ov-card-title">Why calls failed</h2>
                  <p className="ov-card-meta">
                    {events === null
                      ? "Grouped by connector and error."
                      : "Across the latest " + count(events.length, "call", "calls") + ", grouped by connector and error."}
                  </p>
                </div>
                {failedCount > 0 && events !== null ? (
                  <span className="ov-card-figure">
                    <strong>{failedCount}</strong>
                    {" of " + String(events.length) + " failed"}
                  </span>
                ) : null}
              </div>
              {reasons === null ? (
                <p className="ov-card-empty">{eventsError !== null ? "Could not be loaded." : "Loading…"}</p>
              ) : reasons.length === 0 ? (
                <EmptyState
                  icon={Check}
                  title="No failures"
                  description="Every one of the latest calls succeeded."
                />
              ) : (
                <ul className="ov-reasons">
                  {reasons.map(function renderReason(r) {
                    return (
                      <li className="ov-reason" key={r.key}>
                        <ConnectorMark slug={r.connector} label={r.label} size={24} />
                        <div className="ov-reason-body">
                          <p className="ov-reason-title">
                            <span className="ov-reason-count">{count(r.count, "failure", "failures")}</span>
                            <span>{r.label}</span>
                          </p>
                          <p className="ov-reason-msg">{r.message}</p>
                          <p className="ov-reason-meta">
                            {"Latest via " + r.source + " · "}
                            <time dateTime={r.last} title={absoluteTime(r.last)}>
                              {relativeTime(r.last)}
                            </time>
                          </p>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          </div>
        </div>

        {/* ================= Endpoints ================= */}
        <div
          role="tabpanel"
          id="ov-panel-endpoints"
          aria-labelledby="ov-tab-endpoints"
          hidden={tab !== "endpoints"}
          className="ov-tabpanel"
          tabIndex={0}
        >
          <section className="ov-card">
            <div className="ov-card-head">
              <div>
                <h2 className="ov-card-title">Your MCP endpoints</h2>
                <p className="ov-card-meta">
                  Every URL this workspace serves. Paste one, with a key, into any MCP client.
                </p>
              </div>
              {totals !== null ? (
                <span className="ov-card-figure">
                  <strong>{totals.connections}</strong>
                  {" endpoints · "}
                  <strong>{totals.tools}</strong>
                  {" tools · "}
                  <strong>{totals.keys}</strong>
                  {totals.keys === 1 ? " key" : " keys"}
                </span>
              ) : null}
            </div>
            {connections === null ? (
              <p className="ov-card-empty">
                {connectionsError !== null ? "Could not be loaded." : "Loading your endpoints…"}
              </p>
            ) : connections.length === 0 ? (
              <EmptyState
                icon={Database}
                title="No endpoints yet"
                description="Connect a source and Honeycomb gives it an MCP URL."
              />
            ) : (
              <div className="ov-scroll-x">
                <table className="ov-endpoint-table">
                  <thead>
                    <tr>
                      <th scope="col">Endpoint</th>
                      <th scope="col">Status</th>
                      <th scope="col" className="num">Tools on</th>
                      <th scope="col" className="num">Keys</th>
                      <th scope="col" className="num">Added</th>
                    </tr>
                  </thead>
                  <tbody>
                    {connections.map(function renderEndpoint(row) {
                      const title = connectionTitle(row);
                      const on = row.tool_count - row.disabled_tools.length;
                      const down = row.status === "error";
                      return (
                        <tr key={row.id}>
                          <td>
                            <div className="ov-endpoint-name">
                              <ConnectorMark slug={row.connector} label={row.connector_label || row.connector} />
                              <div>
                                <Link className="data-row-link" href={"/dashboard/connectors/" + row.connector}>
                                  {title}
                                </Link>
                                <p className="conn-row-meta">{row.connector_label}</p>
                                <div className="ov-endpoint-url">
                                  <McpUrl url={row.mcp_url} label={"Copy the MCP URL for " + title} />
                                </div>
                              </div>
                            </div>
                          </td>
                          <td>
                            <span className={down ? "conn-status conn-status-error" : "conn-status"}>
                              <span className="conn-status-dot" aria-hidden="true" />
                              <span>{down ? "Error" : "Active"}</span>
                            </span>
                            {down && row.last_error.length > 0 ? (
                              <p className="conn-row-error">{row.last_error}</p>
                            ) : null}
                          </td>
                          <td className="num">{String(on) + " / " + String(row.tool_count)}</td>
                          <td className="num">
                            {row.key_count > 0 ? (
                              row.key_count
                            ) : (
                              <span title="Nothing can call this endpoint until it has a key.">None</span>
                            )}
                          </td>
                          <td className="num">
                            <time dateTime={row.created_at} title={absoluteTime(row.created_at)}>
                              {new Date(row.created_at).toLocaleDateString()}
                            </time>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>

        {/* ================= Performance ================= */}
        <div
          role="tabpanel"
          id="ov-panel-performance"
          aria-labelledby="ov-tab-performance"
          hidden={tab !== "performance"}
          className="ov-tabpanel"
          tabIndex={0}
        >
          <div className="ov-cards ov-cards-2">
            <section className="ov-card">
              <div className="ov-card-head">
                <div>
                  <h2 className="ov-card-title">Response time</h2>
                  <p className="ov-card-meta">
                    {timing === null || timing.count === 0
                      ? "Over the latest timed calls."
                      : "Over the latest " + count(timing.count, "timed call", "timed calls") + "."}
                  </p>
                </div>
              </div>
              {timing === null ? (
                <p className="ov-card-empty">{eventsError !== null ? "Could not be loaded." : "Loading…"}</p>
              ) : timing.count === 0 ? (
                <p className="ov-card-empty">No timed calls yet.</p>
              ) : (
                <>
                  <ul className="ov-figures">
                    <li>
                      <span className="ov-figure-value">{formatMs(timing.p50)}</span>
                      <span className="ov-figure-label">Median</span>
                    </li>
                    <li>
                      <span className="ov-figure-value">{formatMs(timing.p95)}</span>
                      <span className="ov-figure-label">95th percentile</span>
                    </li>
                    <li>
                      <span className="ov-figure-value">{formatMs(timing.max)}</span>
                      <span className="ov-figure-label">Slowest</span>
                    </li>
                    {live !== null && live.avg_ms !== null ? (
                      <li>
                        <span className="ov-figure-value">{formatMs(Math.round(live.avg_ms))}</span>
                        <span className="ov-figure-label">Average · 24h</span>
                      </li>
                    ) : null}
                  </ul>
                  <h3 className="act-section-title">Slowest recent calls</h3>
                  <ul className="conn-list ov-events">
                    {timing.slowest.map(function renderSlow(row) {
                      return <EventRow row={row} key={row.id} />;
                    })}
                  </ul>
                </>
              )}
            </section>

            <section className="ov-card">
              <div className="ov-card-head">
                <div>
                  <h2 className="ov-card-title">Average response by connector</h2>
                  <p className="ov-card-meta">Last 24 hours, slowest first.</p>
                </div>
              </div>
              {live === null ? (
                <p className="ov-card-empty">{liveLoading ? "Loading…" : "Could not be loaded."}</p>
              ) : latencyRows.length === 0 ? (
                <p className="ov-card-empty">No timed calls in the last 24 hours.</p>
              ) : (
                <ValueBars
                  rows={latencyRows}
                  caption="Average response time per connector, last 24 hours"
                  measure="Average response"
                />
              )}
            </section>
          </div>
        </div>

        {/* ================= Trends ================= */}
        <div
          role="tabpanel"
          id="ov-panel-trends"
          aria-labelledby="ov-tab-trends"
          hidden={tab !== "trends"}
          className="ov-tabpanel"
          tabIndex={0}
        >
          {history !== null ? (
            <div className="ov-trend">
              <ActivityCalendar summary={history} />
              <ActivityTrend summary={history} live={live} />
            </div>
          ) : (
            <p className="ov-card-empty">Loading the year…</p>
          )}
        </div>

        {/* Nothing loaded yet: the page has no honest content, so it shows the
            wait rather than a frame of zeros. */}
        {connections === null && connectionsError === null && events === null ? (
          <p className="conn-loading">Loading&hellip;</p>
        ) : null}
      </div>
    </div>
  );
}
