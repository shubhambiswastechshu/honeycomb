"use client";

import { LayoutGrid } from "lucide-react";
import ResourceBoard from "@/components/dashboard/ResourceBoard";
import type { FieldSpec } from "@/components/dashboard/ResourceBoard";
import {
  createWorkspace,
  deleteWorkspace,
  listWorkspaces,
} from "@/lib/api";
import type { Workspace } from "@/lib/api";

// Module scope on purpose: ResourceBoard keys its load effect on this
// reference, so a function rebuilt on every render would refetch forever.
const FIELDS: FieldSpec[] = [
  {
    name: "name",
    label: "Name",
    kind: "text",
    placeholder: "Analytics",
    required: true,
  },
  {
    name: "description",
    label: "Description",
    kind: "textarea",
    placeholder: "What this workspace is for.",
    help: "Shown on the workspace list. Optional.",
  },
];

function load(): Promise<Workspace[]> {
  return listWorkspaces();
}

function create(values: Record<string, unknown>): Promise<Workspace> {
  return createWorkspace({
    name: String(values.name || ""),
    description: values.description === undefined ? "" : String(values.description),
  });
}

function remove(workspace: Workspace): Promise<void> {
  return deleteWorkspace(workspace.id);
}

export default function WorkspacesPage() {
  return (
    <ResourceBoard<Workspace>
      icon={LayoutGrid}
      title="Workspaces"
      lede="Each one keeps its own sources, queries and pipelines."
      addLabel="New workspace"
      emptyTitle="No workspaces yet"
      emptyText="Everything else in this product lives inside a workspace, so this is the first thing to make."
      fields={FIELDS}
      load={load}
      create={create}
      remove={remove}
      rowKey={function key(workspace) {
        return workspace.id;
      }}
      renderRow={function row(workspace) {
        return (
          <>
            <span className="board-row-title">{workspace.name}</span>
            {workspace.description ? (
              <span className="board-row-text">{workspace.description}</span>
            ) : null}
            <span className="board-row-meta">
              {workspace.counts.data_sources} sources ·{" "}
              {workspace.counts.queries} queries ·{" "}
              {workspace.counts.pipelines} pipelines
            </span>
          </>
        );
      }}
    />
  );
}
