"use client";

/**
 * The top bar's bell: the switch for the live panel, and the count of what is
 * wrong right now.
 *
 * It used to open a popover that closed on the first click outside. It now
 * toggles the docked live panel (LivePanel), which stays open while you move
 * around the dashboard -- so this component owns no data and no list, only
 * the button and its badge.
 *
 * The badge is exactly the length of the panel's Problems list, so the number
 * on the bell and the rows behind it cannot disagree. There is deliberately
 * no read/unread state: marking something read needs somewhere to keep the
 * mark, and that would be a second source of truth about whether something is
 * wrong. The number counts problems that exist RIGHT NOW and falls on its own
 * the moment a connection is fixed.
 */

import { Bell } from "lucide-react";
import { useLive } from "@/components/dashboard/LiveProvider";
import {
  LIVE_PANEL_ID,
  LIVE_TOGGLE_ID,
} from "@/components/dashboard/LivePanel";

export default function NotificationBell() {
  const { open, toggle, problemCount } = useLive();

  return (
    <div className="dash-notif">
      <button
        type="button"
        id={LIVE_TOGGLE_ID}
        className="dash-notif-button"
        aria-expanded={open}
        aria-controls={LIVE_PANEL_ID}
        aria-label={
          problemCount > 0
            ? "Live activity, " +
              String(problemCount) +
              (problemCount === 1 ? " problem" : " problems")
            : "Live activity"
        }
        title={open ? "Hide live activity" : "Show live activity"}
        onClick={toggle}
      >
        <Bell size={18} strokeWidth={1.9} aria-hidden="true" />
        {problemCount > 0 ? (
          <span className="dash-notif-badge" aria-hidden="true">
            {problemCount > 99 ? "99+" : problemCount}
          </span>
        ) : null}
      </button>
    </div>
  );
}
