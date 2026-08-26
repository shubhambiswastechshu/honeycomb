"use client";

import { Plug } from "lucide-react";
import ResourceBoard from "@/components/dashboard/ResourceBoard";
import TestSourceButton from "@/components/dashboard/TestSourceButton";
import type { FieldSpec } from "@/components/dashboard/ResourceBoard";
import {
  createDataSource,
  deleteDataSource,
  listDataSources,
} from "@/lib/api";
import type { DataSource } from "@/lib/api";

const FIELDS: FieldSpec[] = [
  {
    name: "workspace",
    label: "Workspace",
    kind: "workspace",
    required: true,
    placeholder: "Choose a workspace",
  },
  {
    name: "name",
    label: "Name",
    kind: "text",
    placeholder: "Production warehouse",
    required: true,
  },
  {
    name: "kind",
    label: "Kind",
    kind: "select",
    required: true,
    placeholder: "Choose a kind",
    options: [
      { value: "POSTGRES", label: "PostgreSQL" },
      { value: "MYSQL", label: "MySQL" },
      { value: "BIGQUERY", label: "BigQuery" },
      { value: "SNOWFLAKE", label: "Snowflake" },
      { value: "S3", label: "Amazon S3" },
      { value: "HTTP", label: "HTTP endpoint" },
    ],
  },
  { name: "host", label: "Host", kind: "text", placeholder: "db.internal" },
  {
    name: "port",
    label: "Port",
    kind: "text",
    placeholder: "5432",
    help: "Leave blank for the default port for this kind.",
  },
  { name: "database", label: "Database", kind: "text", placeholder: "analytics" },
  { name: "username", label: "Username", kind: "text", placeholder: "readonly" },
  {
    name: "secret_name",
    label: "Secret name",
    kind: "text",
    placeholder: "warehouse/readonly",
    help:
      "The key this connection's password is stored under, never the password " +
      "itself -- no credential is kept on this record. With the default " +
      "backend, \"warehouse_ro\" is read from the environment variable " +
      "HONEYCOMB_SECRET_WAREHOUSE_RO.",
  },
];

function load(): Promise<DataSource[]> {
  return listDataSources();
}

function create(values: Record<string, unknown>): Promise<DataSource> {
  return createDataSource(values);
}

function remove(source: DataSource): Promise<void> {
  return deleteDataSource(source.id);
}

export default function SourcesPage() {
  return (
    <ResourceBoard<DataSource>
      icon={Plug}
      title="Connect data"
      lede="Where this organization reads data from."
      addLabel="Connect a source"
      emptyTitle="Nothing connected yet"
      emptyText="Point a workspace at a database, warehouse or bucket and it becomes available to queries and pipelines."
      fields={FIELDS}
      load={load}
      create={create}
      remove={remove}
      rowKey={function key(source) {
        return source.id;
      }}
      rowActions={function actions(source, refresh) {
        return <TestSourceButton source={source} refresh={refresh} />;
      }}
      renderRow={function row(source) {
        return (
          <>
            <span className="board-row-title">{source.name}</span>
            <span className="board-row-text">
              {source.kind_display}
              {source.host ? " · " + source.host : ""}
              {source.database ? "/" + source.database : ""}
            </span>
            <span className="board-row-meta">
              {source.workspace_name} · {source.status_display}
              {source.secret_name && !source.secret_configured
                ? " · no password stored"
                : ""}
            </span>
          </>
        );
      }}
    />
  );
}
