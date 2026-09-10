"use client";

import { Search } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import ConnectorMark from "@/components/dashboard/ConnectorMark";
import McpUrl from "@/components/dashboard/McpUrl";
import type { Connection } from "@/lib/api";

/**
 * The Data page's inventory: every MCP this workspace serves.
 *
 * Split out of the page so it can be rendered against fixtures. A list whose
 * layout only misbehaves at six rows with one of them failing is a list you
 * cannot judge from the two rows a dev database happens to hold.
 *
 * The page exists for one action -- take an endpoint URL to an AI client -- and
 * one question: what is broken. So the URL is the anchor of every row rather
 * than its footnote, and anything failing is lifted into its own group instead
 * of being marked with a dot and left where it was.
 */

/** Below this many rows a filter field is clutter; above it, it is the point. */
const FILTER_FROM = 6;

export interface DataInventoryProps {
  rows: Connection[];
}

function count(n: number, one: string, many: string): string {
  return String(n) + " " + (n === 1 ? one : many);
}

function title(row: Connection): string {
  const name = row.name.trim();
  return name.length > 0 ? name : row.connector_label;
}

function Row({ row }: { row: Connection }) {
  const failing = row.status === "error";
  const live = row.tool_count - row.disabled_tools.length;
  return (
    <li className={failing ? "inv-row is-failing" : "inv-row"}>
      <ConnectorMark slug={row.connector} label={row.connector_label || row.connector} />

      <div className="inv-id">
        <Link className="inv-name" href={"/dashboard/connectors/" + row.connector}>
          {title(row)}
        </Link>
        {/* No "Active" chip. A green badge on every healthy row is noise that
            makes the one red badge harder to find, and the group heading
            already says which state you are looking at. */}
        {failing ? <span className="inv-flag">Error</span> : null}
      </div>

      {/* Icons dropped: at 12px beside the words they label they were texture,
          not information. Zero keys is omitted rather than printed -- on a
          workspace with none it was the same "0 keys" on every row. */}
      <p className="inv-meta">
        <span>{row.connector_label}</span>
        <span className="inv-dot" aria-hidden="true" />
        <span>
          {count(live, "tool", "tools")}
          {row.disabled_tools.length > 0
            ? " · " + String(row.disabled_tools.length) + " off"
            : ""}
        </span>
        {row.key_count > 0 ? (
          <>
            <span className="inv-dot" aria-hidden="true" />
            <span>{count(row.key_count, "key", "keys")}</span>
          </>
        ) : null}
      </p>

      <McpUrl url={row.mcp_url} label={"Copy the MCP URL for " + title(row)} />

      {failing && row.last_error.length > 0 ? (
        <p className="inv-error">{row.last_error}</p>
      ) : null}
    </li>
  );
}

export default function DataInventory({ rows }: DataInventoryProps) {
  const [query, setQuery] = useState("");

  const totals = useMemo(
    function summarise() {
      return {
        tools: rows.reduce(function add(sum, r) {
          return sum + (r.tool_count - r.disabled_tools.length);
        }, 0),
        keys: rows.reduce(function add(sum, r) {
          return sum + r.key_count;
        }, 0),
      };
    },
    [rows],
  );

  const matched = useMemo(
    function filter() {
      const q = query.trim().toLowerCase();
      if (q.length === 0) {
        return rows;
      }
      // Match what a person would type: the name they gave it, the connector's
      // name, or a fragment of the URL they are hunting for.
      return rows.filter(function hit(r) {
        return (
          title(r).toLowerCase().includes(q) ||
          r.connector_label.toLowerCase().includes(q) ||
          r.connector.toLowerCase().includes(q) ||
          r.mcp_url.toLowerCase().includes(q)
        );
      });
    },
    [rows, query],
  );

  const failing = matched.filter(function bad(r) {
    return r.status === "error";
  });
  const healthy = matched.filter(function ok(r) {
    return r.status !== "error";
  });

  return (
    <div className="inv">
      <div className="inv-bar">
        {/* One sentence instead of four tiles. The numbers are context for the
            list below, not the subject of the page, and a tile row of them was
            taking the top of the screen to say so. */}
        <p className="inv-summary">
          <b>{count(rows.length, "connection", "connections")}</b>
          <span className="inv-dot" aria-hidden="true" />
          {count(totals.tools, "tool", "tools")}
          {totals.keys > 0 ? (
            <>
              <span className="inv-dot" aria-hidden="true" />
              {count(totals.keys, "key", "keys")}
            </>
          ) : null}
        </p>

        {rows.length >= FILTER_FROM ? (
          <label className="inv-search">
            <Search size={14} strokeWidth={2} aria-hidden="true" />
            <input
              type="search"
              value={query}
              placeholder="Filter by name, source or URL"
              aria-label="Filter connections"
              onChange={function onChange(event) {
                setQuery(event.target.value);
              }}
            />
          </label>
        ) : null}
      </div>

      {matched.length === 0 ? (
        <p className="inv-none">
          Nothing matches &ldquo;{query}&rdquo;. Try the source name, or part of the URL.
        </p>
      ) : null}

      {/* Failing first, and only when something is failing. The group is the
          status: a reader scanning for trouble finds it at the top or not at
          all, rather than reading every row's dot. */}
      {failing.length > 0 ? (
        <section className="inv-group inv-group-bad">
          <h2 className="inv-group-title">
            Needs attention
            <span className="inv-group-count">{failing.length}</span>
          </h2>
          <ul className="inv-list">
            {failing.map(function each(row) {
              return <Row row={row} key={row.id} />;
            })}
          </ul>
        </section>
      ) : null}

      {healthy.length > 0 ? (
        <section className="inv-group">
          {failing.length > 0 ? (
            <h2 className="inv-group-title">
              Working
              <span className="inv-group-count">{healthy.length}</span>
            </h2>
          ) : null}
          <ul className="inv-list">
            {healthy.map(function each(row) {
              return <Row row={row} key={row.id} />;
            })}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
