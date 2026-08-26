"use client";

import { GitBranch } from "lucide-react";
import ResourceBoard from "@/components/dashboard/ResourceBoard";
import type { FieldSpec } from "@/components/dashboard/ResourceBoard";
import { createPipeline, deletePipeline, listPipelines } from "@/lib/api";
import type { Pipeline } from "@/lib/api";

const FIELDS: FieldSpec[] = [
  {
    name: "workspace",
    label: "Workspace",
    kind: "workspace",
    required: true,
    placeholder: "Choose a workspace",
  },
  {
    name: "source",
    label: "Reads from",
    kind: "source",
    placeholder: "Choose a source",
    help: "Required before a pipeline can be made active.",
  },
  {
    name: "name",
    label: "Name",
    kind: "text",
    placeholder: "Nightly account load",
    required: true,
  },
  {
    name: "destination",
    label: "Writes to",
    kind: "text",
    placeholder: "warehouse.accounts",
  },
  {
    name: "schedule",
    label: "Schedule",
    kind: "text",
    placeholder: "0 3 * * *",
    help: "A cron expression. Leave blank to run it by hand.",
  },
  {
    name: "status",
    label: "Status",
    kind: "select",
    placeholder: "Draft",
    options: [
      { value: "DRAFT", label: "Draft" },
      { value: "ACTIVE", label: "Active" },
      { value: "PAUSED", label: "Paused" },
    ],
  },
  { name: "description", label: "Description", kind: "textarea" },
];

function load(): Promise<Pipeline[]> {
  return listPipelines();
}

function create(values: Record<string, unknown>): Promise<Pipeline> {
  return createPipeline(values);
}

function remove(pipeline: Pipeline): Promise<void> {
  return deletePipeline(pipeline.id);
}

export default function PipelinesPage() {
  return (
    <ResourceBoard<Pipeline>
      icon={GitBranch}
      title="Data pipelines"
      lede="Scheduled moves from a source into somewhere useful."
      addLabel="New pipeline"
      emptyTitle="No pipelines yet"
      emptyText="Define a move from one of your sources, and it will be listed here with its schedule."
      fields={FIELDS}
      load={load}
      create={create}
      remove={remove}
      rowKey={function key(pipeline) {
        return pipeline.id;
      }}
      renderRow={function row(pipeline) {
        return (
          <>
            <span className="board-row-title">{pipeline.name}</span>
            <span className="board-row-text">
              {pipeline.source_name || "No source"}
              {pipeline.destination ? " → " + pipeline.destination : ""}
              {pipeline.schedule ? " · " + pipeline.schedule : " · manual"}
            </span>
            <span className="board-row-meta">
              {pipeline.workspace_name} · {pipeline.status_display} ·{" "}
              {pipeline.last_outcome_display}
            </span>
          </>
        );
      }}
    />
  );
}
