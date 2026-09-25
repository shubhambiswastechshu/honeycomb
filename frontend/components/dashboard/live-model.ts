/**
 * Pure helpers behind the live panel and the charts: no React, no fetching.
 *
 * Everything here is derived from what the API returned. Nothing invents a
 * row, and nothing renders a figure for data that has not arrived.
 */

import type {
  ActivityEvent,
  ActivityLive,
  ActivitySummary,
  Connection,
} from "@/lib/api";

/**
 * One line in the Problems list. `href` is null when the thing it describes
 * can no longer be opened -- the server nulls a call's connection when that
 * connection is deleted, and a link to something that is gone is worse than
 * no link.
 */
export interface ProblemRow {
  key: string;
  kind: "connection" | "call";
  title: string;
  detail: string;
  at: string | null;
  href: string | null;
  connector: string;
  connectorLabel: string;
}

/**
 * What is wrong right now: connections sitting in "error", then failed calls
 * inside the live window.
 *
 * A failure from last week is history, not a problem, so old failures are not
 * counted here even though the feed can still show them. Deriving this rather
 * than storing it is deliberate: there is no read/unread mark to get out of
 * step with the thing it describes, and the badge falls on its own the moment
 * a connection is fixed.
 */
export function buildProblems(
  connections: Connection[] | null,
  live: ActivityLive | null
): ProblemRow[] {
  const rows: ProblemRow[] = [];

  (connections === null ? [] : connections).forEach(function down(row) {
    if (row.status !== "error") {
      return;
    }
    rows.push({
      key: "connection:" + String(row.id),
      kind: "connection",
      title: row.name.trim() || row.connector_label,
      detail:
        row.last_error.trim() ||
        "This connection reported an error and has stopped working.",
      at: row.updated_at,
      href: "/dashboard/connectors/" + row.connector,
      connector: row.connector,
      connectorLabel: row.connector_label,
    });
  });

  if (live !== null) {
    const since = new Date(live.since).getTime();
    live.recent.forEach(function failed(event: ActivityEvent) {
      if (event.status === "ok") {
        return;
      }
      if (new Date(event.created_at).getTime() < since) {
        return;
      }
      rows.push({
        key: "call:" + String(event.id),
        kind: "call",
        title: event.tool_name,
        detail: event.error_message.trim() || "The call failed without a message.",
        at: event.created_at,
        href:
          event.connection === null
            ? null
            : "/dashboard/connectors/" + event.connector,
        connector: event.connector,
        connectorLabel: event.connector_label || event.connector,
      });
    });
  }

  return rows;
}

/** "4 s ago", "3 min ago", "5 hr ago", then the date. `nowMs` is passed in so a ticking clock can re-render it. */
export function relativeTime(iso: string | null, nowMs: number): string {
  if (iso === null) {
    return "";
  }
  const when = new Date(iso).getTime();
  if (Number.isNaN(when)) {
    return "";
  }
  const secs = Math.max(0, Math.round((nowMs - when) / 1000));
  if (secs < 5) {
    return "just now";
  }
  if (secs < 60) {
    return String(secs) + " s ago";
  }
  const mins = Math.round(secs / 60);
  if (mins < 60) {
    return String(mins) + " min ago";
  }
  const hours = Math.round(mins / 60);
  if (hours < 24) {
    return String(hours) + " hr ago";
  }
  return new Date(when).toLocaleDateString();
}

/** A duration a person can read: "182 ms", "1.4 s", or a dash when untimed. */
export function formatMs(ms: number | null): string {
  if (ms === null) {
    return "—";
  }
  if (ms < 1000) {
    return String(ms) + " ms";
  }
  return (ms / 1000).toFixed(1) + " s";
}

/**
 * The last `days` days of a longer summary, with the totals recomputed from
 * the days that remain -- so the number over a chart still equals the sum of
 * its bars whichever window is showing.
 */
export function tailSummary(summary: ActivitySummary, days: number): ActivitySummary {
  const slice = summary.days.slice(-days);
  let total = 0;
  let errors = 0;
  slice.forEach(function add(day) {
    total += day.ok + day.error;
    errors += day.error;
  });
  return { days: slice, total: total, errors: errors };
}

/** "Mon, 14 Sep" from an ISO "YYYY-MM-DD", read as the UTC day the server counted. */
export function dayLabel(iso: string, withYear: boolean): string {
  const when = new Date(iso + "T00:00:00Z");
  if (Number.isNaN(when.getTime())) {
    return iso;
  }
  return when.toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: withYear ? "numeric" : undefined,
    timeZone: "UTC",
  });
}

/** "14:00" in the viewer's own timezone, from an hour's ISO instant. */
export function hourLabel(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) {
    return iso;
  }
  return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
