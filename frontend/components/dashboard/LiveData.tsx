"use client";

import { AlertCircle, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { runConnectionTool } from "@/lib/api";
import type { ConnectorTool } from "@/lib/api";

/**
 * Live data: what this connection actually returns, on the page that owns it.
 *
 * The connector detail page could always say what a connector *could* fetch --
 * a list of tool names and descriptions. It could never show a single row of
 * real data, so "is this connection working?" was a question you answered by
 * leaving for an AI client and asking there.
 *
 * Three decisions shape this:
 *
 * Read-only, enforced server-side. Only tools the catalog does not mark `write`
 * can run here. The refusal lives in ToolRunSerializer rather than in this
 * component, because a guard that only exists in the browser is not a guard.
 *
 * The result is shaped, not dumped. Provider payloads are arbitrary JSON, and a
 * pretty-printed blob answers "did it respond" but not "what did it say". So
 * scalars become a stat row, the first array of records becomes a table, and
 * the raw JSON stays one disclosure away for when the shaped view is wrong.
 *
 * Refresh is opt-in. "Real time" for an ads API is not a socket -- it is a poll
 * against a rate-limited upstream that bills you. So the default is manual, and
 * turning on an interval is a deliberate choice with the cost visible.
 */

/** Poll intervals offered, in seconds. 0 is "off" and is the default. */
const INTERVALS = [0, 15, 60, 300];

/** Rows shown before the table scrolls rather than grows. */
const MAX_ROWS = 50;
/** Columns a record table will show before it starts hiding the tail. */
const MAX_COLS = 7;

export interface LiveDataProps {
  connectionId: number;
  tools: ConnectorTool[];
  /** False while credentials are missing -- nothing here can work yet. */
  ready: boolean;
}

type Json = unknown;

interface Shaped {
  /** Scalar leaves of the top-level object, shown as a stat row. */
  stats: Array<{ key: string; value: string }>;
  /** The first array-of-records found, shown as a table. */
  rows: Array<Record<string, Json>> | null;
  /** Where that array was found, so the table can be labelled honestly. */
  rowsKey: string | null;
  /** Total rows before truncation. */
  rowsTotal: number;
}

function isRecord(value: Json): value is Record<string, Json> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isScalar(value: Json): boolean {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

/** Human label for a snake_case key, without inventing words. */
function label(key: string): string {
  const spaced = key.replace(/_/g, " ").trim();
  return spaced.length === 0 ? key : spaced[0].toUpperCase() + spaced.slice(1);
}

function cell(value: Json): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  if (typeof value === "number") {
    // Provider ids are numbers too, and grouping them reads as a quantity that
    // it is not. Only group values small enough to plausibly be a count.
    return Number.isInteger(value) && Math.abs(value) < 1e15
      ? value.toLocaleString("en-GB")
      : String(value);
  }
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.length + " item" + (value.length === 1 ? "" : "s");
  }
  return "{…}";
}

/**
 * Find the shape worth rendering.
 *
 * Deliberately shallow: one pass over the top level, and one level into it for
 * an array. A recursive walk finds more, but it also finds arrays buried in
 * metadata and puts them on screen as though they were the answer.
 */
function shape(data: Json): Shaped {
  const empty: Shaped = { stats: [], rows: null, rowsKey: null, rowsTotal: 0 };

  if (Array.isArray(data)) {
    const records = data.filter(isRecord);
    if (records.length === data.length && records.length > 0) {
      return { stats: [], rows: records.slice(0, MAX_ROWS), rowsKey: null, rowsTotal: data.length };
    }
    return empty;
  }

  if (!isRecord(data)) {
    return empty;
  }

  const stats: Array<{ key: string; value: string }> = [];
  let rows: Array<Record<string, Json>> | null = null;
  let rowsKey: string | null = null;
  let rowsTotal = 0;

  for (const key of Object.keys(data)) {
    const value = data[key];
    if (isScalar(value)) {
      stats.push({ key: key, value: cell(value) });
      continue;
    }
    if (rows === null && Array.isArray(value)) {
      const records = value.filter(isRecord);
      if (records.length > 0 && records.length === value.length) {
        rows = records.slice(0, MAX_ROWS);
        rowsKey = key;
        rowsTotal = value.length;
      }
    }
  }

  return { stats: stats, rows: rows, rowsKey: rowsKey, rowsTotal: rowsTotal };
}

/** Union of keys across the rows, capped, in first-seen order. */
function columnsOf(rows: Array<Record<string, Json>>): string[] {
  const seen: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (seen.indexOf(key) === -1) {
        seen.push(key);
      }
    }
  }
  return seen.slice(0, MAX_COLS);
}

function ago(at: number | null): string {
  if (at === null) {
    return "";
  }
  const secs = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (secs < 60) {
    return secs + "s ago";
  }
  const mins = Math.round(secs / 60);
  return mins + (mins === 1 ? " min ago" : " mins ago");
}

function Table({
  rows,
  caption,
}: {
  rows: Array<Record<string, Json>>;
  caption: string | null;
}) {
  /* Derived once per render, not once per row: with 50 rows this was walking
     every record 50 times to answer the same question. */
  const columns = useMemo(
    function derive() {
      return columnsOf(rows);
    },
    [rows],
  );

  /* A column is numeric only if every value present in it is a number. Digits
     that do not line up are digits you cannot compare down a column, and the
     tabular figures already set on these cells only pay off once they do. One
     stray string is enough to disqualify a column -- a half-aligned column
     reads worse than an honest left-aligned one. */
  const numeric = useMemo(
    function findNumeric() {
      const out: Record<string, boolean> = {};
      for (const key of columns) {
        let seen = 0;
        let allNumbers = true;
        for (const row of rows) {
          const value = row[key];
          if (value === null || value === undefined) {
            continue;
          }
          seen += 1;
          if (typeof value !== "number") {
            allNumbers = false;
            break;
          }
        }
        out[key] = allNumbers && seen > 0;
      }
      return out;
    },
    [rows, columns],
  );

  return (
    <div className="live-table-wrap">
      <table className="live-table">
        <caption className="live-caption">{caption ? label(caption) : "Results"}</caption>
        <thead>
          <tr>
            {columns.map(function each(key) {
              return (
                <th key={key} className={numeric[key] ? "live-num" : undefined}>
                  {label(key)}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map(function eachRow(row, index) {
            return (
              <tr key={index}>
                {columns.map(function eachCell(key) {
                  const text = cell(row[key]);
                  /* The cell is ellipsised in CSS, so the full value has to
                     survive somewhere -- ids and creative names are routinely
                     wider than any sensible column. */
                  return (
                    <td key={key} className={numeric[key] ? "live-num" : undefined} title={text}>
                      {text}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function LiveData({ connectionId, tools, ready }: LiveDataProps) {
  /* Only read tools that are switched on. A write tool would be refused by the
     server anyway; offering it and then explaining the refusal is worse than
     not offering it. */
  const runnable = useMemo(
    function pickRunnable() {
      return tools.filter(function keep(tool) {
        return !tool.write && tool.enabled !== false;
      });
    },
    [tools],
  );

  const [tool, setTool] = useState<string>("");
  const [args, setArgs] = useState<Record<string, string>>({});
  const [data, setData] = useState<Json>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [at, setAt] = useState<number | null>(null);
  const [took, setTook] = useState<number | null>(null);
  const [every, setEvery] = useState(0);
  const [, setTick] = useState(0);

  const current = useMemo(
    function findCurrent() {
      return runnable.find(function match(t) {
        return t.name === tool;
      });
    },
    [runnable, tool],
  );

  const missing = useMemo(
    function findMissing() {
      const need = current?.required || [];
      return need.filter(function blank(key) {
        return (args[key] || "").trim().length === 0;
      });
    },
    [current, args],
  );

  /* First runnable tool is the default. Connector catalogs are ordered with the
     broadest "what have I got access to" call first, which is the right thing
     to land on. */
  useEffect(
    function chooseDefault() {
      if (tool === "" && runnable.length > 0) {
        setTool(runnable[0].name);
      }
    },
    [runnable, tool],
  );

  const run = useCallback(
    async function run() {
      if (tool === "" || !ready) {
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const sent: Record<string, unknown> = {};
        for (const key of Object.keys(args)) {
          const raw = args[key].trim();
          if (raw.length > 0) {
            sent[key] = raw;
          }
        }
        const result = await runConnectionTool(connectionId, tool, sent);
        setData(result.data);
        setTook(result.duration_ms);
        setAt(Date.now());
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "That call did not come back.");
        setData(null);
      } finally {
        setBusy(false);
      }
    },
    [connectionId, tool, args, ready],
  );

  /* The poll. Cleared and rebuilt whenever the interval or the tool changes, so
     switching tools never leaves the previous one polling in the background. */
  const runRef = useRef(run);
  runRef.current = run;
  useEffect(
    function poll() {
      if (every === 0 || !ready) {
        return;
      }
      const id = window.setInterval(function fire() {
        runRef.current();
      }, every * 1000);
      return function stop() {
        window.clearInterval(id);
      };
    },
    [every, tool, ready],
  );

  /* Re-render once a second only while there is a timestamp to age. */
  useEffect(
    function age() {
      if (at === null) {
        return;
      }
      const id = window.setInterval(function bump() {
        setTick(function next(n) {
          return n + 1;
        });
      }, 1000);
      return function stop() {
        window.clearInterval(id);
      };
    },
    [at],
  );

  const shaped = useMemo(
    function reshape() {
      return data === null ? null : shape(data);
    },
    [data],
  );

  if (runnable.length === 0) {
    return null;
  }

  return (
    <section className="live" aria-label="Live data">
      <div className="live-bar">
        <label className="live-pick">
          <span className="live-pick-label">Fetch</span>
          <select
            value={tool}
            disabled={!ready}
            onChange={function onChange(event) {
              setTool(event.target.value);
              setArgs({});
              setData(null);
              setError(null);
              setAt(null);
            }}
          >
            {runnable.map(function each(t) {
              return (
                <option key={t.name} value={t.name}>
                  {t.name}
                </option>
              );
            })}
          </select>
        </label>

        <div className="live-actions">
          <label className="live-every">
            <span>Refresh</span>
            <select
              value={String(every)}
              disabled={!ready}
              onChange={function onChange(event) {
                setEvery(Number(event.target.value));
              }}
            >
              {INTERVALS.map(function each(secs) {
                return (
                  <option key={secs} value={String(secs)}>
                    {secs === 0 ? "Manual" : secs < 60 ? secs + "s" : secs / 60 + " min"}
                  </option>
                );
              })}
            </select>
          </label>

          <button
            type="button"
            className="conn-button conn-button-primary live-run"
            onClick={run}
            disabled={busy || !ready || missing.length > 0}
          >
            {busy ? (
              <RefreshCw size={14} strokeWidth={2} className="live-spin" aria-hidden="true" />
            ) : (
              <Play size={14} strokeWidth={2} aria-hidden="true" />
            )}
            {at === null ? "Fetch" : "Refresh"}
          </button>
        </div>
      </div>

      {current && current.description.length > 0 ? (
        <p className="live-what">{current.description}</p>
      ) : null}

      {/* Only the arguments this tool actually takes, required ones first. A
          connector's optional filters are a long tail nobody wants on screen
          before they have seen a single row. */}
      {current && current.required && current.required.length > 0 ? (
        <div className="live-args">
          {current.required.map(function each(key) {
            const meta = current.params ? current.params[key] : undefined;
            return (
              <label className="live-arg" key={key}>
                <span>{label(key)}</span>
                <input
                  type="text"
                  value={args[key] || ""}
                  placeholder={meta?.description || ""}
                  onChange={function onChange(event) {
                    const next = event.target.value;
                    setArgs(function update(prev) {
                      return { ...prev, [key]: next };
                    });
                  }}
                />
              </label>
            );
          })}
        </div>
      ) : null}

      {!ready ? (
        <p className="live-idle">Add credentials above and this will start returning data.</p>
      ) : null}

      {error !== null ? (
        <p className="live-error" role="alert">
          <AlertCircle size={14} strokeWidth={2} aria-hidden="true" />
          {error}
        </p>
      ) : null}

      {shaped !== null && error === null ? (
        <div className="live-out">
          <p className="live-when" role="status">
            {shaped.rowsTotal > 0
              ? shaped.rowsTotal.toLocaleString("en-GB") +
                (shaped.rowsTotal === 1 ? " row" : " rows")
              : "Responded"}
            <span className="live-dot" aria-hidden="true" />
            {took !== null ? took.toLocaleString("en-GB") + " ms" : ""}
            <span className="live-dot" aria-hidden="true" />
            {ago(at)}
          </p>

          {shaped.stats.length > 0 ? (
            <dl className="live-stats">
              {shaped.stats.map(function each(stat) {
                return (
                  <div className="live-stat" key={stat.key}>
                    <dt>{label(stat.key)}</dt>
                    <dd>{stat.value}</dd>
                  </div>
                );
              })}
            </dl>
          ) : null}

          {shaped.rows !== null ? (
            <Table rows={shaped.rows} caption={shaped.rowsKey} />
          ) : null}

          {shaped.rowsTotal > MAX_ROWS ? (
            <p className="live-more">
              Showing the first {MAX_ROWS} of{" "}
              {shaped.rowsTotal.toLocaleString("en-GB")}. The full set is in the raw
              response, and in any AI client using this connection.
            </p>
          ) : null}

          {/* The shaped view is a guess about someone else's JSON. When it
              guesses wrong, this is the appeal -- never a dead end. */}
          <details className="live-raw">
            <summary>Raw response</summary>
            <pre>{JSON.stringify(data, null, 2)}</pre>
          </details>
        </div>
      ) : null}
    </section>
  );
}
