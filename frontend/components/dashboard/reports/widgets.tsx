"use client";

/**
 * The visuals a report widget can be: a number, a table, or a bar chart.
 *
 * Every widget reads the same two things -- a run's outcome (ok/loading/
 * error) and, when it has one, a comparison run -- and turns whatever JSON the
 * provider sent back into something a marketer did not have to configure by
 * hand. `shape()` (lib/reports/shape.ts, shared with the connector page's
 * live-data preview) finds the array of records or the scalar stats; the
 * widget then picks a sensible field to show unless one is set in
 * `widget.fields`.
 *
 * No chart library: a bar chart is not worth one, and the repo carries none.
 * The table reuses DataTable from the Google Ads report (sort, search, CSV)
 * rather than a second table implementation -- it is already generic over any
 * row shape, wrapped in `.ga` for the card styling it was built against.
 */

import { useMemo } from "react";
import { DataTable } from "@/components/reports/google-ads/ads-ui";
// The table widget wraps itself in .ga to get this card system's table
// styling (sortable header buttons, the CSV/"show more" bar) -- see the
// TableWidget body below. Nothing else from the Ads report is used here.
import "@/components/reports/google-ads/google-ads-report.css";
import type { SavedReportRun, SavedReportWidget } from "@/lib/api";
import { cell, columnsOf, inferFieldTypes, isNumericColumn, label, shape } from "@/lib/reports/shape";
import type { Json } from "@/lib/reports/shape";

/** What a widget body needs, independent of how the report route answered. */
export interface WidgetRun {
  status: "loading" | "ready" | "error";
  data: Json;
  error: string | null;
}

export interface WidgetRunPair {
  main: WidgetRun;
  /** Present only when the report is comparing and this widget opted in. */
  previous: WidgetRun | null;
}

export function runOf(result: SavedReportRun | undefined): WidgetRun {
  if (result === undefined) {
    return { status: "error", data: null, error: "This widget did not come back." };
  }
  if (result.ok) {
    return { status: "ready", data: result.data, error: null };
  }
  return { status: "error", data: null, error: result.error };
}

type Stat = { key: string; value: string; raw: Json };

function firstNumericKey(stats: Stat[], rows: Array<Record<string, Json>> | null): string | null {
  if (rows !== null && rows.length > 0) {
    for (const key of columnsOf(rows, 64)) {
      if (isNumericColumn(rows, key)) {
        return key;
      }
    }
  }
  // A stat's OWN value must actually be a number, not merely something that
  // parses as one -- a Google Ads customer id ("1234567890") is a string
  // that Number() happily accepts, and would otherwise be picked as the KPI
  // ahead of an id-shaped field with no metric anywhere in sight.
  const numeric = stats.find((s) => typeof s.raw === "number");
  return numeric ? numeric.key : null;
}

function numberAt(rows: Array<Record<string, Json>> | null, stats: Stat[], key: string): number | null {
  if (rows !== null && rows.length > 0) {
    let total = 0;
    let any = false;
    for (const row of rows) {
      const value = row[key];
      if (typeof value === "number") {
        total += value;
        any = true;
      }
    }
    return any ? total : null;
  }
  const found = stats.find((s) => s.key === key);
  return found !== undefined && typeof found.raw === "number" ? found.raw : null;
}

/** Generous headroom above every connector's default row cap (checked: all
    well under 500), so a KPI's total is never silently short -- and if a
    future tool ever does return more, rowsTotal below still says so instead
    of staying quiet about it. */
const WIDGET_MAX_ROWS = 2000;

/* ------------------------------------------------------------------ */
/* Frame: the card every widget renders inside                         */
/* ------------------------------------------------------------------ */

export function WidgetFrame({
  title,
  onRemove,
  children,
}: {
  title: string;
  onRemove?: () => void;
  children: React.ReactNode;
}) {
  return (
    <section className="rpt-widget">
      <header className="rpt-widget-head">
        <h3 className="rpt-widget-title">{title}</h3>
        {onRemove !== undefined ? (
          <button type="button" className="rpt-widget-remove" aria-label={"Remove " + title} onClick={onRemove}>
            {"✕"}
          </button>
        ) : null}
      </header>
      <div className="rpt-widget-body">{children}</div>
    </section>
  );
}

function StatusBody({ run }: { run: WidgetRun }) {
  if (run.status === "loading") {
    return <div className="rpt-widget-skeleton" aria-hidden="true" />;
  }
  return (
    <p className="rpt-widget-error" role="alert">
      {run.error !== null && run.error.length > 0 ? run.error : "This widget could not be loaded."}
    </p>
  );
}

/* ------------------------------------------------------------------ */
/* KPI                                                                  */
/* ------------------------------------------------------------------ */

export function KpiWidget({ widget, run }: { widget: SavedReportWidget; run: WidgetRunPair }) {
  const shaped = useMemo(() => (run.main.status === "ready" ? shape(run.main.data, WIDGET_MAX_ROWS) : null), [run.main]);
  const prevShaped = useMemo(
    () => (run.previous !== null && run.previous.status === "ready" ? shape(run.previous.data, WIDGET_MAX_ROWS) : null),
    [run.previous]
  );

  if (run.main.status !== "ready" || shaped === null) {
    return <StatusBody run={run.main} />;
  }

  const configured = typeof widget.fields?.["metric"] === "string" ? (widget.fields["metric"] as string) : null;
  const key = configured ?? firstNumericKey(shaped.stats, shaped.rows);
  if (key === null) {
    return <p className="rpt-widget-empty">Nothing numeric to show.</p>;
  }

  const value = numberAt(shaped.rows, shaped.stats, key);
  const previous = prevShaped !== null ? numberAt(prevShaped.rows, prevShaped.stats, key) : null;
  const delta = value !== null && previous !== null && previous !== 0 ? ((value - previous) / Math.abs(previous)) * 100 : null;

  return (
    <div className="rpt-kpi">
      <p className="rpt-kpi-value">{value !== null ? cell(value) : "—"}</p>
      <p className="rpt-kpi-label">{label(key)}</p>
      {delta !== null ? (
        <p className={"rpt-kpi-delta" + (delta >= 0 ? " is-up" : " is-down")}>
          <span aria-hidden="true">{delta >= 0 ? "▲" : "▼"}</span>
          {/* The arrow is aria-hidden and colour alone does not reach a
              screen reader, so the direction is also a plain word -- without
              it, an increase and a decrease of the same size read identically. */}
          <span className="dash-visually-hidden">{delta >= 0 ? "Up " : "Down "}</span>
          {Math.abs(delta).toFixed(1)}%
          <span className="rpt-kpi-delta-note"> vs previous period</span>
        </p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Table                                                               */
/* ------------------------------------------------------------------ */

export function TableWidget({ run }: { widget: SavedReportWidget; run: WidgetRunPair }) {
  const shaped = useMemo(() => (run.main.status === "ready" ? shape(run.main.data, WIDGET_MAX_ROWS) : null), [run.main]);

  if (run.main.status !== "ready" || shaped === null) {
    return <StatusBody run={run.main} />;
  }
  if (shaped.rows === null || shaped.rows.length === 0) {
    if (shaped.stats.length > 0) {
      return (
        <dl className="rpt-stats">
          {shaped.stats.map((stat) => (
            <div className="rpt-stat" key={stat.key}>
              <dt>{label(stat.key)}</dt>
              <dd>{stat.value}</dd>
            </div>
          ))}
        </dl>
      );
    }
    return <p className="rpt-widget-empty">No rows came back.</p>;
  }

  const rows = shaped.rows;
  const columns = columnsOf(rows, 10);
  const fieldTypes = inferFieldTypes(rows);

  return (
    <div className="ga">
      <DataTable
        rows={rows}
        rowKey={(row) => JSON.stringify(row)}
        caption={shaped.rowsKey ?? "Results"}
        columns={columns.map((key) => ({
          id: key,
          label: label(key),
          align: fieldTypes[key] === "number" ? ("right" as const) : undefined,
          sort: (row: Record<string, Json>) => {
            const value = row[key];
            return typeof value === "number" || typeof value === "string" ? value : null;
          },
          cell: (row: Record<string, Json>) => cell(row[key]),
          csv: (row: Record<string, Json>) => {
            const value = row[key];
            return typeof value === "number" || typeof value === "string" ? value : null;
          },
        }))}
        pageSize={10}
      />
      {shaped.rowsTotal > rows.length ? (
        <p className="rpt-widget-truncated">
          Showing the first {rows.length.toLocaleString("en-GB")} of {shaped.rowsTotal.toLocaleString("en-GB")} rows.
        </p>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Bar                                                                  */
/* ------------------------------------------------------------------ */

const BAR_MAX_ITEMS = 8;
const BAR_HEIGHT = 26;
const BAR_GAP = 10;
const BAR_LABEL_W = 120;

export function BarWidget({ widget, run }: { widget: SavedReportWidget; run: WidgetRunPair }) {
  const shaped = useMemo(() => (run.main.status === "ready" ? shape(run.main.data, WIDGET_MAX_ROWS) : null), [run.main]);

  if (run.main.status !== "ready" || shaped === null) {
    return <StatusBody run={run.main} />;
  }
  const rows = shaped.rows;
  if (rows === null || rows.length === 0) {
    return <p className="rpt-widget-empty">No rows came back.</p>;
  }

  const types = inferFieldTypes(rows);
  const columns = columnsOf(rows, 64);
  const configuredCategory = typeof widget.fields?.["category"] === "string" ? (widget.fields["category"] as string) : null;
  const configuredValue = typeof widget.fields?.["value"] === "string" ? (widget.fields["value"] as string) : null;
  const category = configuredCategory ?? columns.find((k) => types[k] !== "number") ?? columns[0];
  const value = configuredValue ?? columns.find((k) => types[k] === "number");

  if (category === undefined || value === undefined) {
    return <p className="rpt-widget-empty">This result has no field to chart.</p>;
  }

  const points = rows
    .map((row) => ({ label: cell(row[category]), value: typeof row[value] === "number" ? (row[value] as number) : 0 }))
    .sort((a, b) => b.value - a.value)
    .slice(0, BAR_MAX_ITEMS);
  const max = Math.max(1, ...points.map((p) => p.value));
  const chartWidth = 100;
  const height = points.length * (BAR_HEIGHT + BAR_GAP);

  return (
    <>
      <svg
        className="rpt-bar-chart"
        viewBox={"0 0 " + String(BAR_LABEL_W + chartWidth + 40) + " " + String(height)}
        role="img"
        aria-label={label(value) + " by " + label(category)}
      >
        {points.map((point, index) => {
          const y = index * (BAR_HEIGHT + BAR_GAP);
          const w = (point.value / max) * chartWidth;
          return (
            <g key={point.label + index}>
              <text x={BAR_LABEL_W - 8} y={y + BAR_HEIGHT / 2} textAnchor="end" dominantBaseline="middle" className="rpt-bar-label">
                {point.label}
              </text>
              <rect x={BAR_LABEL_W} y={y} width={Math.max(w, 1)} height={BAR_HEIGHT} rx={3} className="rpt-bar-rect" />
              <text x={BAR_LABEL_W + w + 6} y={y + BAR_HEIGHT / 2} dominantBaseline="middle" className="rpt-bar-value">
                {cell(point.value)}
              </text>
            </g>
          );
        })}
      </svg>
      {/* The SVG is role="img" with one summary label, so none of the
          per-bar numbers a sighted reader gets directly off the chart reach
          assistive tech. This table carries the exact same data, visually
          hidden but always present -- the product's own rule ("every visual
          has a table alternative") applied literally, not just for a chart
          type that already had DataTable to lean on. */}
      <table className="dash-visually-hidden">
        <caption>{label(value) + " by " + label(category)}</caption>
        <thead>
          <tr>
            <th scope="col">{label(category)}</th>
            <th scope="col">{label(value)}</th>
          </tr>
        </thead>
        <tbody>
          {points.map((point, index) => (
            <tr key={point.label + index}>
              <td>{point.label}</td>
              <td>{cell(point.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
