"use client";

/**
 * Fetches one "pack" of report tools -- the handful a single sub-tab needs --
 * in one request, and hands each section its own slice of the answer.
 *
 * Why a pack per view rather than everything up front: every tool is a real
 * call against Google's API, so a page that fetches the whole report to draw
 * its first screen spends quota on screens nobody opened. Each sub-tab asks for
 * its own tools when it is first shown.
 *
 * Why the cache: the server already caches provider results for five minutes,
 * so a repeat request is cheap for Google but still a round trip for the
 * person. Remembering the answer here for the same five minutes makes flipping
 * between sub-tabs instant, and "Refresh" is the deliberate way past it.
 *
 * Every section fails on its own. A pack-level failure (throttled, offline)
 * gives every section the same error; a single tool failing gives only its own
 * section one.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { runReport } from "@/lib/api";
import type { ReportResult, ReportRun } from "@/lib/api";

/** A run plus the name a section uses to ask for its result. */
export interface PackRun extends ReportRun {
  id: string;
}

export type PackStatus = "idle" | "loading" | "ready" | "error";

export interface PackState {
  status: PackStatus;
  results: Record<string, ReportResult>;
  error: string | null;
  /** Epoch ms of the answer on screen. */
  loadedAt: number | null;
}

/** What one section needs: loading, an error to show, or its data. */
export interface Slice {
  status: "loading" | "ready" | "error";
  data: unknown;
  error: string | null;
  /** The data is the last answer for this same view, shown dimmed while a refresh runs. */
  stale?: boolean;
}

/** Matches the server's TTL_MEDIUM: a fresher answer than this would be a cache miss there too. */
const FRESH_MS = 5 * 60 * 1000;

const CACHE = new Map<string, { at: number; results: Record<string, ReportResult> }>();

const IDLE: PackState = { status: "idle", results: {}, error: null, loadedAt: null };

export function sliceOf(state: PackState, id: string): Slice {
  if (state.status === "loading") {
    const last = state.results[id];
    if (last !== undefined && last.ok) {
      return { status: "ready", data: last.data, error: null, stale: true };
    }
    return { status: "loading", data: null, error: null };
  }
  if (state.status === "idle") {
    return { status: "loading", data: null, error: null };
  }
  if (state.status === "error") {
    return { status: "error", data: null, error: state.error };
  }
  const result = state.results[id];
  if (result === undefined) {
    return { status: "error", data: null, error: "This report did not come back." };
  }
  if (!result.ok) {
    return { status: "error", data: null, error: result.error };
  }
  return { status: "ready", data: result.data, error: null };
}

export function usePack(
  connectionId: number,
  /** Null means "nothing to ask yet" -- no account has been chosen. */
  key: string | null,
  runs: PackRun[]
): { state: PackState; reload: () => void } {
  const [state, setState] = useState<PackState>(IDLE);
  const [nonce, setNonce] = useState<number>(0);

  // The runs are rebuilt on every render; the key is what says they changed.
  const runsRef = useRef<PackRun[]>(runs);
  runsRef.current = runs;
  const forceRef = useRef<boolean>(false);
  const viewRef = useRef<string>("");

  useEffect(
    function fetchPack() {
      if (key === null) {
        setState(IDLE);
        return;
      }
      const cacheKey = String(connectionId) + "|" + key;
      const cached = CACHE.get(cacheKey);
      if (!forceRef.current && cached !== undefined && Date.now() - cached.at < FRESH_MS) {
        viewRef.current = cacheKey;
        setState({ status: "ready", results: cached.results, error: null, loadedAt: cached.at });
        return;
      }
      forceRef.current = false;

      let alive = true;
      const sameView = viewRef.current === cacheKey;
      viewRef.current = cacheKey;
      setState(function loading(previous) {
        // A refresh of the SAME view keeps what was on screen, dimmed: an
        // empty page that refills is a flash, not progress. A different
        // account or range starts clean -- showing another view's numbers
        // under the new heading, even dimmed, would be a wrong answer.
        return {
          status: "loading",
          results: sameView ? previous.results : {},
          error: null,
          loadedAt: sameView ? previous.loadedAt : null,
        };
      });

      const sent = runsRef.current;
      runReport(
        connectionId,
        sent.map(function strip(run) {
          return { tool: run.tool, args: run.args };
        })
      )
        .then(function received(response) {
          if (!alive) {
            return;
          }
          const results: Record<string, ReportResult> = {};
          sent.forEach(function place(run, index) {
            const result = response.results[index];
            if (result !== undefined) {
              results[run.id] = result;
            }
          });
          const at = Date.now();
          CACHE.set(cacheKey, { at: at, results: results });
          setState({ status: "ready", results: results, error: null, loadedAt: at });
        })
        .catch(function failed(caught: unknown) {
          if (!alive) {
            return;
          }
          setState({
            status: "error",
            results: {},
            error:
              caught instanceof Error && caught.message.length > 0
                ? caught.message
                : "The report could not be loaded.",
            loadedAt: null,
          });
        });

      return function stop() {
        alive = false;
      };
    },
    [connectionId, key, nonce]
  );

  const reload = useCallback(function reload(): void {
    forceRef.current = true;
    setNonce(function bump(n) {
      return n + 1;
    });
  }, []);

  return { state: state, reload: reload };
}
