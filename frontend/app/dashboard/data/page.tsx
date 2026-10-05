"use client";

/**
 * Data: every MCP this workspace is serving, in one place.
 *
 * The connectors page is the catalogue -- what you *could* connect. This is
 * the inventory: what is actually running, and the URL for each one. They are
 * different questions, which is why the URL appears in both places rather than
 * one linking to the other.
 *
 * Every number here comes from GET /connections/. Nothing is derived, averaged
 * or estimated, and when nothing is connected the page says so rather than
 * rendering an empty frame.
 *
 * DELETING. A delete is a countdown first and a request second. Clicking
 * Delete starts an UNDO_SECONDS timer on the row; Undo stops it, and only when
 * it runs out is DELETE /connections/<id>/ sent. Nothing is removed on the
 * server until then, so Undo is a real undo -- the alternative, deleting at
 * once and re-creating on Undo, would mint a new URL and lose the keys, which
 * is exactly what Undo is meant to save.
 *
 * Leaving the page while a countdown runs sends the delete then: the person
 * asked for it and did not undo it, and going elsewhere is not a change of
 * mind. Closing the tab is different -- a request cannot be relied on to
 * leave a closing page -- so the browser is asked to warn first, and if the
 * tab closes anyway the delete simply never happens. Losing a delete is
 * recoverable; losing a connection is not.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import EmptyState from "@/components/dashboard/EmptyState";
import { Database } from "lucide-react";
import DataInventory from "@/components/dashboard/DataInventory";
import type { Deletion } from "@/components/dashboard/DataInventory";
import PanelCover from "@/components/dashboard/PanelCover";
import { useSession } from "@/components/dashboard/SessionProvider";
import { deleteConnection, listConnections } from "@/lib/api";
import type { Connection } from "@/lib/api";

const LOAD_ERROR = "Could not load your connected MCPs.";

/** How long a delete can be undone before it is sent. */
const UNDO_SECONDS = 8;

/** How often the countdown redraws. Smooth enough for the bar, cheap enough. */
const TICK_MS = 200;

/** The server's own rule (connections/oauth.py CONNECT_ADMIN_ROLES). */
const DELETE_ROLES = ["OWNER", "ADMIN"];

function title(row: Connection): string {
  const name = row.name.trim();
  return name.length > 0 ? name : row.connector_label;
}

function messageOf(caught: unknown): string {
  return caught instanceof Error && caught.message.length > 0
    ? caught.message
    : "The server refused.";
}

function without<T>(map: Record<number, T>, id: number): Record<number, T> {
  const next = { ...map };
  delete next[id];
  return next;
}

export default function DataPage() {
  const { session } = useSession();
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  /** When each pending delete fires, as an epoch-ms deadline, by id. */
  const [deadlines, setDeadlines] = useState<Record<number, number>>({});
  const [sending, setSending] = useState<Record<number, boolean>>({});
  const [refused, setRefused] = useState<Record<number, string>>({});
  const [now, setNow] = useState<number>(Date.now());
  /** One sentence for screen readers about the latest change. */
  const [announce, setAnnounce] = useState<string>("");

  const aliveRef = useRef<boolean>(true);
  // Ids already sent. A tick can fire again before React has re-rendered the
  // countdown away, and a delete must never be sent twice.
  const sentRef = useRef<Set<number>>(new Set<number>());
  // The timers read these, so they see the current values rather than the ones
  // captured when the interval was created.
  const deadlinesRef = useRef<Record<number, number>>({});
  const rowsRef = useRef<Connection[] | null>(null);
  deadlinesRef.current = deadlines;
  rowsRef.current = rows;

  const allowed = DELETE_ROLES.indexOf(session.user.role.toUpperCase()) >= 0;

  const load = useCallback(async function load(): Promise<void> {
    try {
      const fetched = await listConnections();
      if (aliveRef.current) {
        setRows(fetched);
        setError(null);
      }
    } catch (caught) {
      if (aliveRef.current) {
        setError(LOAD_ERROR);
      }
    }
  }, []);

  useEffect(
    function loadOnMount() {
      aliveRef.current = true;
      void load();
      return function unmount() {
        aliveRef.current = false;
      };
    },
    [load]
  );

  /** Send one delete. Called when its countdown runs out, or on leaving. */
  const send = useCallback(function send(id: number): void {
    if (sentRef.current.has(id)) {
      return;
    }
    sentRef.current.add(id);
    const row = (rowsRef.current || []).find(function match(r) {
      return r.id === id;
    });
    const name = row !== undefined ? title(row) : "The connection";
    setDeadlines(function drop(prev) {
      return without(prev, id);
    });
    setSending(function mark(prev) {
      return { ...prev, [id]: true };
    });
    void deleteConnection(id)
      .then(function gone() {
        if (!aliveRef.current) {
          return;
        }
        setRows(function remove(prev) {
          return prev === null
            ? prev
            : prev.filter(function keep(r) {
                return r.id !== id;
              });
        });
        setAnnounce(name + " was deleted.");
      })
      .catch(function failed(caught: unknown) {
        if (!aliveRef.current) {
          return;
        }
        // Not sent after all, so it may be tried again.
        sentRef.current.delete(id);
        setRefused(function record(prev) {
          return { ...prev, [id]: messageOf(caught) };
        });
        setAnnounce(name + " could not be deleted.");
      })
      .then(function settle() {
        if (aliveRef.current) {
          setSending(function clear(prev) {
            return without(prev, id);
          });
        }
      });
  }, []);

  /* The countdown. Runs only while something is pending, and sends each delete
     whose deadline has passed. */
  const pendingCount = Object.keys(deadlines).length;
  useEffect(
    function countdown() {
      if (pendingCount === 0) {
        return;
      }
      const timer = window.setInterval(function tick() {
        const at = Date.now();
        setNow(at);
        const due = deadlinesRef.current;
        Object.keys(due).forEach(function check(key) {
          const id = Number(key);
          if (due[id] <= at) {
            send(id);
          }
        });
      }, TICK_MS);
      return function stop() {
        window.clearInterval(timer);
      };
    },
    [pendingCount, send]
  );

  /* A closing tab cannot be trusted to finish a request, so ask the browser to
     warn while a delete is still counting down. */
  useEffect(
    function warnOnClose() {
      if (pendingCount === 0) {
        return;
      }
      function onBeforeUnload(event: BeforeUnloadEvent): void {
        event.preventDefault();
        // Older browsers need a value set to show the prompt at all.
        event.returnValue = "";
      }
      window.addEventListener("beforeunload", onBeforeUnload);
      return function unbind() {
        window.removeEventListener("beforeunload", onBeforeUnload);
      };
    },
    [pendingCount]
  );

  /* Navigating elsewhere inside the app is not an undo: send what is pending. */
  useEffect(function sendOnLeave() {
    return function leave() {
      Object.keys(deadlinesRef.current).forEach(function flush(key) {
        const id = Number(key);
        if (sentRef.current.has(id)) {
          return;
        }
        sentRef.current.add(id);
        void deleteConnection(id).catch(function ignore() {
          /* The page is gone; there is nowhere to say it failed. The row will
             still be listed next time, which is the honest outcome. */
        });
      });
    };
  }, []);

  function startDelete(row: Connection): void {
    setRefused(function clear(prev) {
      return without(prev, row.id);
    });
    const at = Date.now();
    setNow(at);
    setDeadlines(function add(prev) {
      return { ...prev, [row.id]: at + UNDO_SECONDS * 1000 };
    });
    setAnnounce(
      title(row) + " will be deleted in " + String(UNDO_SECONDS) + " seconds. Press Undo to keep it."
    );
  }

  function undoDelete(row: Connection): void {
    setDeadlines(function drop(prev) {
      return without(prev, row.id);
    });
    setAnnounce("Kept " + title(row) + ".");
  }

  const secondsLeft: Record<number, number> = {};
  const remaining: Record<number, number> = {};
  Object.keys(deadlines).forEach(function measure(key) {
    const id = Number(key);
    const ms = Math.max(0, deadlines[id] - now);
    secondsLeft[id] = Math.max(1, Math.ceil(ms / 1000));
    remaining[id] = ms / (UNDO_SECONDS * 1000);
  });

  const deletion: Deletion = {
    allowed: allowed,
    secondsLeft: secondsLeft,
    remaining: remaining,
    sending: sending,
    errors: refused,
    onDelete: startDelete,
    onUndo: undoDelete,
  };

  return (
    <div className="panel panel-wide">
      <PanelCover
        title="Data"
        lede="Every MCP this workspace serves, and the URL for each one."
      />

      <div className="panel-body">
        <p className="dash-visually-hidden" role="status" aria-live="polite">
          {announce}
        </p>

        {error !== null ? (
          <p className="error" role="alert">
            {error}
          </p>
        ) : rows === null ? (
          <p className="conn-loading">Loading&hellip;</p>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={Database}
            title="Nothing connected yet"
            description="Connect a source and it will appear here with its MCP URL, ready to paste into an AI client."
            action={
              <Link className="conn-action conn-action-primary" href="/dashboard/connectors">
                Browse MCPs
              </Link>
            }
          />
        ) : (
          <DataInventory rows={rows} deletion={deletion} />
        )}
      </div>
    </div>
  );
}
