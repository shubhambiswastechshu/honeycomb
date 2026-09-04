"use client";

/**
 * Overview: what is wrong, what is happening, what you are serving.
 *
 * The order down the page is the order a person needs it in. Anything failing
 * comes first, because a broken connection is the only thing here that is
 * urgent. Then the calls that actually happened, then the totals behind them,
 * then the URLs, then -- only while the workspace is still new -- the three
 * steps that get the first call to land.
 *
 * Every number on this page is counted from a response that has arrived. There
 * is no health score, no quota bar and no sample row, and nothing renders a
 * figure while its own request is still in flight: a tile reading 0 mid-request
 * is a wrong answer wearing the costume of a loading state. Each of the three
 * requests owns its own slice of the page, so one endpoint being down costs the
 * section that needed it and nothing else.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Activity, Check, Database, TriangleAlert } from "lucide-react";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import EmptyState from "@/components/dashboard/EmptyState";
import ActivityMatrix from "@/components/dashboard/ActivityMatrix";
import McpUrl from "@/components/dashboard/McpUrl";
import PanelCover from "@/components/dashboard/PanelCover";
import { useSession } from "@/components/dashboard/SessionProvider";
import { activitySummary, listActivity, listConnections } from "@/lib/api";
import type { ActivityEvent, ActivitySummary, Connection } from "@/lib/api";

/** How many calls the activity list shows. Also what is asked of the server. */
const EVENT_LIMIT = 12;

/** The activity field's window, in days. 30 is the summary endpoint's cap. */
const SPARK_DAYS = 90;

/**
 * How many MCP URLs the Overview lists before handing off to /dashboard/data.
 * The Overview is a summary; a workspace with twenty connections should not
 * turn this page into the Data page.
 */
const ENDPOINT_CAP = 4;

const CONNECTIONS_ERROR = "Could not load your connections.";
const ACTIVITY_ERROR = "Could not load recent activity.";

/** "1 tool" / "3 tools" -- never a bare number with no noun. */
function count(n: number, one: string, many: string): string {
  return String(n) + " " + (n === 1 ? one : many);
}

/** The name a connection shows when its owner left the field blank. */
function connectionTitle(row: Connection): string {
  const name = row.name.trim();
  return name.length > 0 ? name : row.connector_label;
}

/**
 * "4 mins ago" for a timestamp.
 *
 * Floored at every step, never rounded: rounding turns 59 minutes into
 * "60 mins ago", which is a unit the sentence has already left behind. Past a
 * week the relative form stops meaning anything, so it falls back to the date,
 * and an unparseable value is returned untouched rather than rendered as
 * "NaN days ago".
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
  // which is the most likely row on a live dashboard -- the call you just
  // watched happen.
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
  if (trimmed.length === 0) {
    return "";
  }
  return trimmed.split(/\s+/)[0];
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

/* ------------------------------------------------------------------ */
/* The page                                                            */
/* ------------------------------------------------------------------ */

export default function OverviewPage() {
  const { session } = useSession();

  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [summary, setSummary] = useState<ActivitySummary | null>(null);

  // A dashboard is a tab people leave open. Fetched once and never again, the
  // sparkline silently mislabels which day is "today" after midnight and the
  // relative timestamps drift by hours -- wrong numbers that look exactly like
  // right ones. Refetching when the tab is looked at again costs two requests
  // nobody sees and keeps the page honest. Not a poll: a tab nobody is looking
  // at has nothing to be stale for.
  const load = useCallback(function load(alive: () => boolean): Promise<void> {
    // Three requests, in parallel, each with its own catch. Promise.all over
    // bare promises would reject as a whole the moment one endpoint answered
    // 500 and blank a page whose other two thirds loaded fine, so every
    // promise settles into its own slice of state and the page renders
    // whatever arrived.
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
      activitySummary(SPARK_DAYS)
        .then(function apply(result: ActivitySummary) {
          if (alive()) {
            setSummary(result);
          }
        })
        .catch(function fail() {
          // No message for this one. The sparkline is a decoration on a list
          // that stands perfectly well without it, and a second red line
          // above the same section would just say the same outage twice.
        }),
    ]).then(function done() {
      return undefined;
    });
  }, []);

  useEffect(
    function loadAndRefresh() {
      let live = true;
      function alive(): boolean {
        return live;
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
        live = false;
        document.removeEventListener("visibilitychange", onWake);
        window.removeEventListener("focus", onWake);
      };
    },
    [load]
  );

  const failing =
    connections === null
      ? null
      : connections.filter(function isDown(row) {
          return row.status === "error";
        });

  /** Four counts, every one of them summed from the connections response. */
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

  const hasConnections = connections !== null && connections.length > 0;

  /**
   * Activity is known once either call answered: the list proves calls exist,
   * and so does a non-zero total from the summary. Either is enough to say the
   * last step of Getting started is done.
   */
  const activityKnown = events !== null || summary !== null;
  // Both activity calls failing is the only way activityKnown stays false with
  // the requests finished: either one succeeding sets its own state.
  const activityFailed = eventsError !== null;
  // Whether the activity requests have finished, succeeded or not. Getting
  // started must not vanish because /api/activity/ 500d: its first two steps
  // are answered entirely by /connections/, and a brand-new workspace losing
  // its only instructions to an unrelated outage is the worst case of the
  // per-section isolation this page promises.
  const activitySettled = activityKnown || activityFailed;
  const hasActivity =
    (events !== null && events.length > 0) ||
    (summary !== null && summary.total > 0);

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
    connections !== null &&
    activitySettled &&
    !(stepConnected && stepKeyed && stepCalled);

  // "No calls yet" is only news to someone who has something connected. With
  // an empty workspace the answer is Getting started, not an empty list.
  const showActivity =
    eventsError !== null ||
    (events !== null && events.length > 0) ||
    (events !== null && connections !== null && connections.length > 0) ||
    (events !== null && connectionsError !== null);

  const endpoints =
    connections === null ? [] : connections.slice(0, ENDPOINT_CAP);
  const keyTarget =
    connections !== null && connections.length > 0
      ? "/dashboard/connectors/" + connections[0].connector
      : undefined;

  const greetName = firstName(session.user.full_name);

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

        {/* ---- Needs attention ---- */}
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
                    <ConnectorMark
                      slug={row.connector}
                      label={row.connector_label || row.connector}
                    />
                    <div className="conn-row-body">
                      <p className="conn-row-title">
                        <Link
                          className="data-row-link"
                          href={"/dashboard/connectors/" + row.connector}
                        >
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
                          {row.connector_label} stopped working. Open it to
                          re-enter its credentials.
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}

        {/* ---- Recent activity ---- */}
        {showActivity ? (
          <section className="ov-section">
            <div className="ov-section-head">
              <h2 className="ov-section-title">
                <Activity size={15} strokeWidth={2} aria-hidden="true" />
                <span>Recent activity</span>
              </h2>
            </div>

            {summary !== null ? <ActivityMatrix summary={summary} /> : null}

            {eventsError !== null ? (
              <p className="error" role="alert">
                {eventsError}
              </p>
            ) : events !== null && events.length === 0 ? (
              <EmptyState
                icon={Activity}
                title="No calls yet"
                description="Paste an MCP URL into Claude or another AI client, and every tool call it makes shows up here."
              />
            ) : events !== null ? (
              <ul className="conn-list ov-events">
                {events.map(function renderEvent(row: ActivityEvent) {
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
                            <span
                              className="conn-status-dot"
                              aria-hidden="true"
                            />
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
            ) : null}
          </section>
        ) : null}

        {/* ---- Status strip ---- */}
        {totals !== null && totals.connections > 0 ? (
          <section className="ov-section">
            <h2 className="ov-section-title">At a glance</h2>
            <ul className="data-stats">
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
                <span className="data-stat-label">
                  {totals.keys === 1 ? "Key" : "Keys"}
                </span>
              </li>
              <li
                className={
                  totals.errors > 0 ? "data-stat data-stat-bad" : "data-stat"
                }
              >
                <span className="data-stat-value">{totals.errors}</span>
                <span className="data-stat-label">
                  {totals.errors === 1 ? "Error" : "Errors"}
                </span>
              </li>
            </ul>
          </section>
        ) : null}

        {/* ---- Endpoints ---- */}
        {connections !== null && connections.length > 0 ? (
          <section className="ov-section">
            <div className="ov-section-head">
              <h2 className="ov-section-title">
                <Database size={15} strokeWidth={2} aria-hidden="true" />
                <span>Your MCP endpoints</span>
              </h2>
              {connections.length > ENDPOINT_CAP ? (
                <Link className="ov-section-link" href="/dashboard/data">
                  {"All " + String(connections.length)}
                </Link>
              ) : null}
            </div>
            <ul className="conn-list ov-endpoints">
              {endpoints.map(function renderEndpoint(row: Connection) {
                const title = connectionTitle(row);
                return (
                  <li className="conn-row ov-endpoint" key={row.id}>
                    <ConnectorMark
                      slug={row.connector}
                      label={row.connector_label || row.connector}
                    />
                    <div className="conn-row-body">
                      <p className="conn-row-title">
                        <Link
                          className="data-row-link"
                          href={"/dashboard/connectors/" + row.connector}
                        >
                          {title}
                        </Link>
                      </p>
                      <p className="conn-row-meta">
                        {row.connector_label}
                        {" · " +
                          count(
                            row.tool_count - row.disabled_tools.length,
                            "tool",
                            "tools"
                          )}
                        {" · " + count(row.key_count, "key", "keys")}
                      </p>
                      <McpUrl
                        url={row.mcp_url}
                        label={"Copy the MCP URL for " + title}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}

        {/* ---- Getting started ---- */}
        {showStart ? (
          <section className="ov-section ov-start">
            <h2 className="ov-section-title">Getting started</h2>
            <p className="conn-note">
              Three steps between here and an AI client calling your data.
            </p>
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
                text="Add the MCP URL and the key to Claude, Cursor or any MCP client. The first call it makes appears above."
                href={hasConnections ? "/dashboard/data" : undefined}
                linkLabel="Get the URL"
              />
            </ol>
          </section>
        ) : null}

        {/* Nothing connected, nothing called and nothing loaded yet: the page
            has no honest content, so it shows the wait rather than a frame of
            zeros. */}
        {connections === null && connectionsError === null && events === null ? (
          <p className="conn-loading">Loading&hellip;</p>
        ) : null}
      </div>
    </div>
  );
}
