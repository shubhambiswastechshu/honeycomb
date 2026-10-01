"use client";

/**
 * The desktop-alert toggle, and the thing that actually fires the alerts.
 *
 * Given the error rows the page already polls, it notifies once per row it has
 * not notified about before. The bookkeeping is by row id, not by count: counts
 * go up and down as the server's window slides, and a count-based trigger either
 * misses failures or repeats them.
 *
 * The first load never notifies. Opening the page on a workspace with a hundred
 * old failures must not fire a hundred notifications, so the first batch only
 * establishes the high-water mark; everything after it is genuinely new.
 *
 * The choice is remembered per browser in localStorage. Permission itself lives
 * with the browser, so a person who allowed notifications and then switched the
 * toggle off is off -- the toggle is the intent, the permission is only the
 * capability.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Bell, BellOff, BellRing } from "lucide-react";
import type { ActivityEvent } from "@/lib/api";
import {
  notificationPermission,
  notifyFailure,
  requestNotifications,
} from "@/lib/notify";

const STORAGE_KEY = "hc.activity.alerts";

/** Beyond this many new failures at once, one summary beats a burst of pings. */
const BURST = 3;

interface Props {
  /** Every failure the page currently knows about, newest first. */
  failures: ActivityEvent[];
}

function readStoredChoice(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "on";
  } catch {
    // Private mode, or storage disabled. Default off; the toggle still works
    // for this tab, it simply will not be remembered.
    return false;
  }
}

function storeChoice(on: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, on ? "on" : "off");
  } catch {
    /* Not remembering the choice is not worth failing over. */
  }
}

function describe(row: ActivityEvent): string {
  const where =
    row.connection_name !== null && row.connection_name.length > 0
      ? row.connection_name
      : row.connector_label || row.connector;
  const why =
    row.error_message.length > 0 ? row.error_message : "The call failed.";
  return where + " · " + row.tool_name + "\n" + why;
}

export default function ActivityAlerts({ failures }: Props) {
  const [on, setOn] = useState<boolean>(false);
  const [permission, setPermission] = useState<
    NotificationPermission | "unsupported"
  >("default");

  /* Ids already notified about. A ref, not state: changing it must not cause a
     render, and the effect below reads it in the same tick it writes it. */
  const seen = useRef<Set<number>>(new Set<number>());
  const primed = useRef<boolean>(false);

  useEffect(function restore() {
    setPermission(notificationPermission());
    setOn(readStoredChoice());
  }, []);

  const toggle = useCallback(
    async function toggle(): Promise<void> {
      if (on) {
        setOn(false);
        storeChoice(false);
        return;
      }
      // Asked from the click, which is the only context a browser accepts.
      const granted = await requestNotifications();
      setPermission(granted);
      const allowed = granted === "granted";
      setOn(allowed);
      storeChoice(allowed);
    },
    [on]
  );

  useEffect(
    function announce(): void {
      // The first batch is the baseline, whether the toggle is on or not --
      // so switching it on later does not then announce the backlog.
      if (!primed.current) {
        failures.forEach(function remember(row: ActivityEvent) {
          seen.current.add(row.id);
        });
        primed.current = true;
        return;
      }
      if (!on || permission !== "granted") {
        // Still track them, so turning the toggle on does not replay history.
        failures.forEach(function remember(row: ActivityEvent) {
          seen.current.add(row.id);
        });
        return;
      }

      const fresh = failures.filter(function unseen(row: ActivityEvent) {
        return !seen.current.has(row.id);
      });
      if (fresh.length === 0) {
        return;
      }
      fresh.forEach(function remember(row: ActivityEvent) {
        seen.current.add(row.id);
      });

      if (fresh.length > BURST) {
        notifyFailure({
          title: String(fresh.length) + " MCP calls failed",
          body: "Open the Activity page to see which connectors are affected.",
          tag: "hc-activity-burst",
        });
        return;
      }
      fresh.forEach(function tell(row: ActivityEvent) {
        notifyFailure({
          title: "MCP call failed",
          body: describe(row),
          // Per row, so two different failures both appear, but a re-render
          // of the same row can never double-notify.
          tag: "hc-activity-" + String(row.id),
        });
      });
    },
    [failures, on, permission]
  );

  const blocked = permission === "denied";
  const unsupported = permission === "unsupported";
  const Icon = on ? BellRing : blocked || unsupported ? BellOff : Bell;

  return (
    <div className="act-alerts">
      <button
        type="button"
        className="mkt-chip act-alert-toggle"
        aria-pressed={on}
        disabled={blocked || unsupported}
        onClick={function clicked() {
          void toggle();
        }}
      >
        <Icon size={15} strokeWidth={2.2} aria-hidden="true" />
        {on ? "Alerts on" : "Alert me on failures"}
      </button>
      <p className="act-alert-hint">
        {unsupported
          ? "This browser does not support desktop notifications."
          : blocked
            ? "Notifications are blocked for this site. Allow them in your browser's site settings to turn alerts on."
            : on
              ? "You will get a desktop notification when a tool call fails, as long as a tab with Honeycomb is still open."
              : "Get a desktop notification the moment a tool call fails."}
      </p>
    </div>
  );
}
