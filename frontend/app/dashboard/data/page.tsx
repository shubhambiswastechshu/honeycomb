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
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Database, KeyRound, Wrench } from "lucide-react";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import EmptyState from "@/components/dashboard/EmptyState";
import McpUrl from "@/components/dashboard/McpUrl";
import PanelCover from "@/components/dashboard/PanelCover";
import { listConnections } from "@/lib/api";
import type { Connection } from "@/lib/api";

const LOAD_ERROR = "Could not load your connected MCPs.";

/** "1 tool" / "3 tools" -- never a bare number with no noun. */
function count(n: number, one: string, many: string): string {
  return String(n) + " " + (n === 1 ? one : many);
}

export default function DataPage() {
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async function load(): Promise<void> {
    try {
      setRows(await listConnections());
      setError(null);
    } catch (caught) {
      setError(LOAD_ERROR);
    }
  }, []);

  useEffect(
    function loadOnMount() {
      void load();
    },
    [load]
  );

  /**
   * Three numbers, all counted from the rows already loaded. No second
   * request, no estimate, and nothing shown until the real value exists --
   * a stat tile reading 0 while a request is in flight is a wrong answer.
   */
  const totals =
    rows === null
      ? null
      : {
          connections: rows.length,
          failing: rows.filter(function isDown(row) {
            return row.status === "error";
          }).length,
          tools: rows.reduce(function add(sum, row) {
            return sum + (row.tool_count - row.disabled_tools.length);
          }, 0),
          keys: rows.reduce(function add(sum, row) {
            return sum + row.key_count;
          }, 0),
        };

  return (
    <div className="panel panel-wide">
      <PanelCover
        title="Data"
        lede="Every MCP this workspace serves, and the URL for each one."
      />

      <div className="panel-body">
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
          <>
            {totals !== null ? (
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
                {/* Only when there is something wrong. A permanent "0 errors"
                    tile is a reassurance nobody asked for that costs a
                    quarter of the row. */}
                {totals.failing > 0 ? (
                  <li className="data-stat data-stat-bad">
                    <span className="data-stat-value">{totals.failing}</span>
                    <span className="data-stat-label">Need attention</span>
                  </li>
                ) : null}
              </ul>
            ) : null}

            <ul className="conn-list data-list">
              {rows.map(function renderRow(row: Connection) {
                const failing = row.status === "error";
                return (
                  <li className="conn-row data-row" key={row.id}>
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
                          {row.name || row.connector_label}
                        </Link>
                        <span
                          className={
                            failing
                              ? "conn-status conn-status-error"
                              : "conn-status"
                          }
                        >
                          <span className="conn-status-dot" aria-hidden="true" />
                          <span>{failing ? "Error" : "Active"}</span>
                        </span>
                      </p>

                      <p className="conn-row-meta">
                        {row.connector_label}
                        {" · "}
                        <Wrench size={12} strokeWidth={1.9} aria-hidden="true" />
                        {" " +
                          count(
                            row.tool_count - row.disabled_tools.length,
                            "tool",
                            "tools"
                          )}
                        {row.disabled_tools.length > 0
                          ? " (" + String(row.disabled_tools.length) + " off)"
                          : ""}
                        {" · "}
                        <KeyRound size={12} strokeWidth={1.9} aria-hidden="true" />
                        {" " + count(row.key_count, "key", "keys")}
                      </p>

                      {failing && row.last_error.length > 0 ? (
                        <p className="conn-row-error">{row.last_error}</p>
                      ) : null}

                      <McpUrl
                        url={row.mcp_url}
                        label={"Copy the MCP URL for " + (row.name || row.connector_label)}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
