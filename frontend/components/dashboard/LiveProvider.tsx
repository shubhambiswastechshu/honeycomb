"use client";

/**
 * The dashboard's live data, fetched once and shared.
 *
 * The live panel, the Overview charts and the Activity page all want the same
 * three things -- the last 24 hours, a year of daily counts, and the state of
 * every connection. Held here, they are fetched once per tick instead of once
 * per component, and the panel and the charts can never show two different
 * answers to the same question.
 *
 * Cadence. While the panel is open AND the tab is visible, the 24-hour
 * snapshot refreshes every LIVE_EVERY_MS and the slower data (the year of
 * days, the connections) every SLOW_EVERY ticks. With the panel closed there
 * is no timer at all: the data refreshes on load and whenever the tab is
 * looked at again, which is what the dashboard did before. A tab in the
 * background pauses too -- nothing is watching it, so nothing is stale.
 *
 * The snapshot is one request that answers the whole panel, so the cost of
 * "live" is about twelve small reads a minute, well inside the read throttle.
 *
 * Every slice fails on its own. A connections outage must not blank the feed,
 * and a feed outage must not hide a connection that is actually broken; each
 * keeps its last good value and reports its own error flag.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { buildProblems } from "@/components/dashboard/live-model";
import { activityLive, activitySummary, listConnections } from "@/lib/api";
import type { ActivityLive, ActivitySummary, Connection } from "@/lib/api";

/** Days of history the calendar draws: 53 whole weeks. Matches the server's ceiling. */
export const HISTORY_DAYS = 371;

/** How often the open panel refreshes the 24-hour snapshot. */
export const LIVE_EVERY_MS = 5000;

/** The slow data refreshes every this-many live ticks (30 s). */
const SLOW_EVERY = 6;

/** A refetch on wake inside this window is skipped: focus and visibility both fire. */
const WAKE_THROTTLE_MS = 4000;

/** Wide enough to dock the panel beside the page instead of over it. */
const DOCK_QUERY = "(min-width: 1100px)";

const STORAGE_KEY = "hc.live-panel.open";

/**
 * Whether the panel starts open: only if it was left open AND there is room
 * to dock it. Restoring an overlay on a phone would cover the page the person
 * just navigated to.
 *
 * localStorage is a per-viewer convenience here and nothing more, so every
 * access is guarded -- it can throw or come back empty in a private window.
 */
function readOpen(): boolean {
  try {
    if (typeof window === "undefined") {
      return false;
    }
    return (
      window.localStorage.getItem(STORAGE_KEY) === "1" &&
      window.matchMedia(DOCK_QUERY).matches
    );
  } catch (unavailable) {
    return false;
  }
}

function writeOpen(open: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, open ? "1" : "0");
  } catch (unavailable) {
    // Not remembering is fine; the panel still works.
  }
}

export interface LiveContextValue {
  open: boolean;
  setOpen: (next: boolean) => void;
  toggle: () => void;

  /** Null until the first snapshot lands -- which is not the same as "no calls". */
  live: ActivityLive | null;
  liveError: boolean;
  /** A year of per-day counts. Null until it lands. */
  summary: ActivitySummary | null;
  summaryError: boolean;
  connections: Connection[] | null;
  connectionsError: boolean;

  /** True while the panel is open on a visible tab, i.e. while it is polling. */
  polling: boolean;
  /** Epoch ms of the last successful snapshot, for "updated 3 s ago". */
  updatedAt: number | null;
  /** Problems that exist right now; the number on the bell. */
  problemCount: number;
  refresh: () => void;
}

const LiveContext = createContext<LiveContextValue | null>(null);

export function useLive(): LiveContextValue {
  const value = useContext(LiveContext);
  if (value === null) {
    throw new Error("useLive must be used inside <LiveProvider>.");
  }
  return value;
}

export default function LiveProvider({ children }: { children: ReactNode }) {
  const [open, setOpenState] = useState<boolean>(readOpen);
  const [visible, setVisible] = useState<boolean>(true);

  const [live, setLive] = useState<ActivityLive | null>(null);
  const [liveError, setLiveError] = useState<boolean>(false);
  const [summary, setSummary] = useState<ActivitySummary | null>(null);
  const [summaryError, setSummaryError] = useState<boolean>(false);
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [connectionsError, setConnectionsError] = useState<boolean>(false);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);

  const aliveRef = useRef<boolean>(true);
  const wokeAtRef = useRef<number>(0);

  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  const loadLive = useCallback(function loadLive(): void {
    activityLive()
      .then(function received(snapshot: ActivityLive) {
        if (aliveRef.current) {
          setLive(snapshot);
          setLiveError(false);
          setUpdatedAt(Date.now());
        }
      })
      .catch(function failed() {
        // Keep the last snapshot on screen: stale-and-flagged beats blank.
        if (aliveRef.current) {
          setLiveError(true);
        }
      });
  }, []);

  const loadSlow = useCallback(function loadSlow(): void {
    // Separate catches, for the reason in the header.
    activitySummary(HISTORY_DAYS)
      .then(function received(result: ActivitySummary) {
        if (aliveRef.current) {
          setSummary(result);
          setSummaryError(false);
        }
      })
      .catch(function failed() {
        // The charts stand down without it (and keep the last good year if
        // they have one); the panel does not need it.
        if (aliveRef.current) {
          setSummaryError(true);
        }
      });
    listConnections()
      .then(function received(rows: Connection[]) {
        if (aliveRef.current) {
          setConnections(rows);
          setConnectionsError(false);
        }
      })
      .catch(function failed() {
        if (aliveRef.current) {
          setConnectionsError(true);
        }
      });
  }, []);

  const refresh = useCallback(
    function refresh(): void {
      loadLive();
      loadSlow();
    },
    [loadLive, loadSlow]
  );

  // First load, and again whenever the tab is looked at.
  useEffect(
    function loadOnMountAndWake() {
      refresh();

      function onWake(): void {
        const shown = document.visibilityState !== "hidden";
        setVisible(shown);
        if (!shown) {
          return;
        }
        const now = Date.now();
        if (now - wokeAtRef.current < WAKE_THROTTLE_MS) {
          return;
        }
        wokeAtRef.current = now;
        refresh();
      }

      setVisible(document.visibilityState !== "hidden");
      document.addEventListener("visibilitychange", onWake);
      window.addEventListener("focus", onWake);
      return function unbind() {
        document.removeEventListener("visibilitychange", onWake);
        window.removeEventListener("focus", onWake);
      };
    },
    [refresh]
  );

  const polling = open && visible;

  // The timer exists only while there is a watched, open panel.
  useEffect(
    function poll() {
      if (!polling) {
        return;
      }
      let tick = 0;
      const timer = window.setInterval(function step() {
        tick += 1;
        loadLive();
        if (tick % SLOW_EVERY === 0) {
          loadSlow();
        }
      }, LIVE_EVERY_MS);
      // Opening the panel is a request to see the present, so catch up at
      // once instead of showing data up to a whole tick old.
      loadLive();
      return function stop() {
        window.clearInterval(timer);
      };
    },
    [polling, loadLive, loadSlow]
  );

  const setOpen = useCallback(function setOpen(next: boolean): void {
    setOpenState(next);
    writeOpen(next);
  }, []);

  const toggle = useCallback(
    function toggle(): void {
      setOpen(!open);
    },
    [open, setOpen]
  );

  const problemCount = useMemo(
    function count(): number {
      return buildProblems(connections, live).length;
    },
    [connections, live]
  );

  const value = useMemo<LiveContextValue>(
    function build() {
      return {
        open: open,
        setOpen: setOpen,
        toggle: toggle,
        live: live,
        liveError: liveError,
        summary: summary,
        summaryError: summaryError,
        connections: connections,
        connectionsError: connectionsError,
        polling: polling,
        updatedAt: updatedAt,
        problemCount: problemCount,
        refresh: refresh,
      };
    },
    [
      open,
      setOpen,
      toggle,
      live,
      liveError,
      summary,
      summaryError,
      connections,
      connectionsError,
      polling,
      updatedAt,
      problemCount,
      refresh,
    ]
  );

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}
