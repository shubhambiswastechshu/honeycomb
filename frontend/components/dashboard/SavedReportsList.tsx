"use client";

/**
 * The Reports page's inventory: every dashboard this workspace has built.
 *
 * Modelled on DataInventory -- a compact list of rows a person filters and
 * acts on, not a card grid to browse. Reports are grouped by CLIENT because
 * that is how an agency actually thinks about its own dashboards ("show me
 * Acme's reports"), so the client filter is a first-class control here, not a
 * column you have to scan.
 *
 * Search and the client filter both run over the array already in memory: the
 * list arrived in one request (GET /api/reports/, at most 500 rows), so a
 * keystroke round-tripping to Django would add latency and buy nothing.
 */

import { Trash2 } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import type { SavedReportSummary } from "@/lib/api";

/** Below this many rows a filter bar is clutter; above it, it is the point. */
const FILTER_FROM = 6;

export interface SavedReportsListProps {
  rows: SavedReportSummary[];
  onDelete: (report: SavedReportSummary) => void;
}

function relativeDate(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) {
    return iso;
  }
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) {
    return "just now";
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return minutes + (minutes === 1 ? " minute ago" : " minutes ago");
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return hours + (hours === 1 ? " hour ago" : " hours ago");
  }
  const days = Math.round(hours / 24);
  if (days < 30) {
    return days + (days === 1 ? " day ago" : " days ago");
  }
  return new Date(iso).toLocaleDateString();
}

function byName(client: string) {
  return client.trim().length > 0 ? client.trim() : null;
}

function Row({ row, onDelete }: { row: SavedReportSummary; onDelete: (row: SavedReportSummary) => void }) {
  const editedBy = row.updated_by_name.trim().length > 0 ? row.updated_by_name.trim() : row.created_by_name;
  return (
    <li className="rpt-row">
      <div className="rpt-id">
        <Link className="rpt-name" href={"/dashboard/reports/" + row.id}>
          {row.name}
        </Link>
        {row.description.trim().length > 0 ? <p className="rpt-desc">{row.description}</p> : null}
      </div>

      <p className="rpt-meta">
        {byName(row.client) !== null ? (
          <>
            <span className="rpt-client">{row.client}</span>
            <span className="inv-dot" aria-hidden="true" />
          </>
        ) : null}
        <span>
          {editedBy.length > 0 ? "Edited by " + editedBy : "Edited"} {relativeDate(row.updated_at)}
        </span>
      </p>

      {row.can_delete ? (
        <button
          type="button"
          className="rpt-delete"
          aria-label={"Delete " + row.name}
          onClick={function onClick() {
            onDelete(row);
          }}
        >
          <Trash2 size={15} strokeWidth={1.8} aria-hidden="true" />
        </button>
      ) : null}
    </li>
  );
}

export default function SavedReportsList({ rows, onDelete }: SavedReportsListProps) {
  const [query, setQuery] = useState<string>("");
  const [client, setClient] = useState<string>("");

  const clients = useMemo(
    function distinctClients() {
      // Deduped case-insensitively so "Acme" and "acme" -- a plain typo, not
      // two different clients -- become one entry rather than two, keeping
      // whichever casing was seen first as the display value.
      const seen = new Map<string, string>();
      for (const row of rows) {
        const name = byName(row.client);
        if (name !== null && !seen.has(name.toLowerCase())) {
          seen.set(name.toLowerCase(), name);
        }
      }
      return Array.from(seen.values()).sort(function alphabetical(a, b) {
        return a.localeCompare(b);
      });
    },
    [rows]
  );

  const matched = useMemo(
    function filter() {
      let list = rows;
      if (client.length > 0) {
        const wanted = client.toLowerCase();
        list = list.filter(function sameClient(row) {
          return byName(row.client)?.toLowerCase() === wanted;
        });
      }
      const q = query.trim().toLowerCase();
      if (q.length > 0) {
        list = list.filter(function hit(row) {
          return (
            row.name.toLowerCase().includes(q) ||
            row.client.toLowerCase().includes(q) ||
            row.description.toLowerCase().includes(q)
          );
        });
      }
      return list;
    },
    [rows, client, query]
  );

  return (
    <div className="rpt-inv">
      {(rows.length >= FILTER_FROM || clients.length > 1) ? (
        <div className="rpt-bar">
          <p className="inv-summary">
            <b>{rows.length}</b> {rows.length === 1 ? "report" : "reports"}
          </p>

          {clients.length > 1 ? (
            <select
              className="input rpt-client-filter"
              value={client}
              aria-label="Filter by client"
              onChange={function onChange(event) {
                setClient(event.target.value);
              }}
            >
              <option value="">All clients</option>
              {clients.map(function option(name) {
                return (
                  <option key={name} value={name}>
                    {name}
                  </option>
                );
              })}
            </select>
          ) : null}

          {rows.length >= FILTER_FROM ? (
            <label className="inv-search rpt-search">
              <input
                type="search"
                value={query}
                placeholder="Search reports"
                aria-label="Search reports"
                onChange={function onChange(event) {
                  setQuery(event.target.value);
                }}
              />
            </label>
          ) : null}
        </div>
      ) : null}

      {matched.length === 0 ? (
        <p className="inv-none">
          Nothing matches{query.trim().length > 0 ? " “" + query + "”" : " that filter"}.
        </p>
      ) : (
        <ul className="rpt-list">
          {matched.map(function each(row) {
            return <Row row={row} onDelete={onDelete} key={row.id} />;
          })}
        </ul>
      )}
    </div>
  );
}
