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
import EmptyState from "@/components/dashboard/EmptyState";
import { Database } from "lucide-react";
import DataInventory from "@/components/dashboard/DataInventory";
import PanelCover from "@/components/dashboard/PanelCover";
import { listConnections } from "@/lib/api";
import type { Connection } from "@/lib/api";

const LOAD_ERROR = "Could not load your connected MCPs.";

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
          <DataInventory rows={rows} />
        )}
      </div>
    </div>
  );
}
