"use client";

/**
 * Overview. Two jobs: say who you are signed in as, and be the way into the
 * four things this product does.
 *
 * The counts come from one GET /workspaces/ -- the serializer annotates each
 * workspace with what it holds, so the whole grid costs a single request
 * rather than four. Until that lands the tiles render without their `meta`,
 * which is deliberate: a tile reading "0 sources" while the request is still
 * in flight is a wrong answer, not a loading state.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Database,
  GitBranch,
  LayoutGrid,
  Plug,
  SquareTerminal,
  Terminal,
} from "lucide-react";
import TileGrid from "@/components/dashboard/TileGrid";
import type { TileSpec } from "@/components/dashboard/TileGrid";
import { useSession } from "@/components/dashboard/SessionProvider";
import { listWorkspaces } from "@/lib/api";
import type { Workspace } from "@/lib/api";

/** "1 workspace" / "3 workspaces" -- never a bare number with no noun. */
function count(n: number, one: string, many: string): string {
  return n + " " + (n === 1 ? one : many);
}

export default function OverviewPage() {
  const { session } = useSession();

  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);

  const load = useCallback(async function load(): Promise<void> {
    try {
      setWorkspaces(await listWorkspaces());
    } catch (caught) {
      // The tiles are navigation first; they still work without their counts,
      // so a failed count is not worth an error banner on the landing page.
      setWorkspaces(null);
    }
  }, []);

  useEffect(
    function loadOnMount() {
      void load();
    },
    [load]
  );

  const totals =
    workspaces === null
      ? null
      : workspaces.reduce(
          function add(sum, workspace) {
            return {
              sources: sum.sources + workspace.counts.data_sources,
              queries: sum.queries + workspace.counts.queries,
              pipelines: sum.pipelines + workspace.counts.pipelines,
            };
          },
          { sources: 0, queries: 0, pipelines: 0 }
        );

  const tiles: TileSpec[] = [
    {
      // ?new opens the create form -- the tile says "New workspace", so
      // landing on a list would be one click short of what it promises.
      href: "/dashboard/workspaces?new",
      label: "New workspace",
      description:
        "Group sources, queries and pipelines so two teams can work apart.",
      icon: LayoutGrid,
      meta:
        workspaces === null
          ? undefined
          : count(workspaces.length, "workspace", "workspaces"),
    },
    {
      href: "/dashboard/sql",
      label: "SQL",
      description:
        "Write a query, run it against a source, and keep it for the team.",
      icon: Terminal,
      meta: totals === null ? undefined : count(totals.queries, "query", "queries"),
    },
    {
      href: "/dashboard/python",
      label: "Python",
      description:
        "A scratchpad that runs in your browser — no server, no setup.",
      icon: SquareTerminal,
      // No count: scripts are not part of the workspace serializer's
      // annotation, and a second request for one number on a landing page is
      // not worth the latency.
    },
    {
      href: "/dashboard/pipelines",
      label: "Data pipelines",
      description: "Schedule a move from a source into somewhere useful.",
      icon: GitBranch,
      meta:
        totals === null
          ? undefined
          : count(totals.pipelines, "pipeline", "pipelines"),
    },
    {
      href: "/dashboard/sources?new",
      label: "Connect data",
      description: "Point this workspace at a database, warehouse or bucket.",
      icon: Plug,
      meta: totals === null ? undefined : count(totals.sources, "source", "sources"),
    },
  ];

  return (
    <div className="panel">
      <h1 className="panel-title">Welcome, {session.user.full_name}</h1>
      <p className="panel-lede">You are signed in to {session.tenant.name}.</p>

      <div className="panel-body">
        <TileGrid tiles={tiles} />

        {workspaces !== null && workspaces.length === 0 ? (
          <p className="tile-hint">
            <Database size={14} strokeWidth={1.75} aria-hidden="true" />
            Start with a workspace &mdash; sources, queries and pipelines all
            live inside one.
          </p>
        ) : null}
      </div>
    </div>
  );
}
