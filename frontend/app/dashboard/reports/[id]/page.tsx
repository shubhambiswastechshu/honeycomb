"use client";

/**
 * One saved report: its own viewer and its own editor, at once.
 *
 * There is no separate edit mode. Everyone in the workspace may already
 * change any report (reports/access.py only restricts DELETE), so a toggle
 * between "viewing" and "editing" would be a lock this page never enforces --
 * worse, a false one. Renaming it, adding a widget, changing the date range:
 * every change autosaves, the same way a shared document does.
 *
 * Widgets stack full-width in the order they were added. A real drag-and-drop
 * 12-column canvas is coming; this ships the part a marketer needs to get a
 * real report live today without it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ChevronLeft, Plus } from "lucide-react";
import {
  BarWidget,
  KpiWidget,
  TableWidget,
  WidgetFrame,
  runOf,
} from "@/components/dashboard/reports/widgets";
import type { WidgetRunPair } from "@/components/dashboard/reports/widgets";
import {
  ApiError,
  SavedReportConflictError,
  getSavedReport,
  listConnections,
  listConnectionTools,
  listSavedReports,
  runSavedReport,
  updateSavedReport,
} from "@/lib/api";
import type {
  ConnectorTool,
  Connection,
  SavedReport,
  SavedReportDateFilter,
  SavedReportRangeId,
  SavedReportRunResponse,
  SavedReportWidget,
  WidgetType,
} from "@/lib/api";
import { label } from "@/lib/reports/shape";

const RANGES: Array<{ id: SavedReportRangeId; label: string }> = [
  { id: "LAST_7_DAYS", label: "Last 7 days" },
  { id: "LAST_14_DAYS", label: "Last 14 days" },
  { id: "LAST_30_DAYS", label: "Last 30 days" },
  { id: "LAST_90_DAYS", label: "Last 90 days" },
  { id: "THIS_MONTH", label: "This month" },
  { id: "LAST_MONTH", label: "Last month" },
];

const WIDGET_TYPES: Array<{ id: WidgetType; label: string; height: number }> = [
  { id: "kpi", label: "Number", height: 3 },
  { id: "table", label: "Table", height: 7 },
  { id: "bar", label: "Bar chart", height: 6 },
];

/** Param names this project's connectors use for an explicit date window. */
const START_DATE_KEYS = ["start_date"];
const END_DATE_KEYS = ["end_date"];

function messageOf(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message.length > 0 ? caught.message : fallback;
}

function newWidgetId(): string {
  return "w" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function nextY(layout: SavedReportWidget[]): number {
  return layout.reduce((max, w) => Math.max(max, w.y + w.h), 0);
}

/** Args for a new widget: date-shaped params get the report's own tokens, everything else the value the marketer typed. */
function buildArgs(tool: ConnectorTool, given: Record<string, string>): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  const keys = Object.keys(tool.params || {});
  for (const key of keys) {
    if (START_DATE_KEYS.includes(key)) {
      args[key] = "$date.start";
    } else if (END_DATE_KEYS.includes(key)) {
      args[key] = "$date.end";
    }
  }
  for (const key of Object.keys(given)) {
    const value = given[key].trim();
    if (value.length > 0) {
      args[key] = value;
    }
  }
  return args;
}

function widgetRuns(
  run: SavedReportRunResponse | null,
  widgetId: string
): WidgetRunPair {
  if (run === null) {
    return { main: { status: "loading", data: null, error: null }, previous: null };
  }
  const ref = run.widgets[widgetId];
  if (ref === undefined) {
    return { main: { status: "error", data: null, error: "This widget did not come back." }, previous: null };
  }
  return {
    main: runOf(run.runs[ref.run]),
    previous: ref.prev_run !== null ? runOf(run.runs[ref.prev_run]) : null,
  };
}

/* ------------------------------------------------------------------ */
/* Add-widget panel                                                    */
/* ------------------------------------------------------------------ */

function AddWidgetPanel({
  onAdd,
  onCancel,
}: {
  onAdd: (widget: SavedReportWidget) => void;
  onCancel: () => void;
}) {
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [connectionId, setConnectionId] = useState<number | null>(null);
  const [tools, setTools] = useState<ConnectorTool[] | null>(null);
  const [toolName, setToolName] = useState<string>("");
  const [type, setType] = useState<WidgetType>("table");
  const [title, setTitle] = useState<string>("");
  const [argValues, setArgValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [loadingTools, setLoadingTools] = useState<boolean>(false);

  useEffect(function loadConnections() {
    listConnections()
      .then(function got(rows) {
        setConnections(rows);
        if (rows.length > 0) {
          setConnectionId(rows[0].id);
        }
      })
      .catch(function failed(caught) {
        setError(messageOf(caught, "Could not load your connections."));
      });
  }, []);

  /** The connection this effect's own fetch was for -- so a slow, superseded
      response can never overwrite what a faster, later one already set. */
  const toolsForRef = useRef<number | null>(null);
  useEffect(
    function loadTools() {
      toolsForRef.current = connectionId;
      setArgValues({});
      if (connectionId === null) {
        setTools(null);
        setToolName("");
        return;
      }
      setLoadingTools(true);
      setTools(null);
      setToolName("");
      listConnectionTools(connectionId)
        .then(function got(rows) {
          if (toolsForRef.current !== connectionId) return; // superseded by a later selection
          const readable = rows.filter((t) => !t.write && t.enabled !== false);
          setTools(readable);
          if (readable.length > 0) {
            setToolName(readable[0].name);
          }
        })
        .catch(function failed(caught) {
          if (toolsForRef.current !== connectionId) return;
          setError(messageOf(caught, "Could not load this connection's tools."));
        })
        .finally(function done() {
          if (toolsForRef.current === connectionId) setLoadingTools(false);
        });
    },
    [connectionId]
  );

  /** Args typed for a since-abandoned tool must never leak into the next one. */
  function onToolChange(next: string): void {
    setToolName(next);
    setArgValues({});
  }

  const currentTool = useMemo(
    function find() {
      return (tools || []).find((t) => t.name === toolName);
    },
    [tools, toolName]
  );

  const extraRequired = useMemo(
    function required() {
      const need = currentTool?.required || [];
      return need.filter((key) => !START_DATE_KEYS.includes(key) && !END_DATE_KEYS.includes(key));
    },
    [currentTool]
  );

  const missing = extraRequired.filter((key) => (argValues[key] || "").trim().length === 0);

  function submit(): void {
    if (connectionId === null || currentTool === undefined || missing.length > 0) {
      return;
    }
    const kind = WIDGET_TYPES.find((k) => k.id === type) || WIDGET_TYPES[0];
    onAdd({
      id: newWidgetId(),
      type: type,
      x: 0,
      y: 0, // the caller places it at the bottom of the stack
      w: 12,
      h: kind.height,
      title: title.trim().length > 0 ? title.trim() : label(currentTool.name),
      source: { connection_id: connectionId, tool: currentTool.name, args: buildArgs(currentTool, argValues) },
      options: { compare: true },
    });
  }

  return (
    <section className="rpt-add">
      <h3 className="rpt-add-title">Add a widget</h3>
      {error !== null ? (
        <p className="error" role="alert">
          {error}
        </p>
      ) : null}

      {connections === null ? (
        <p className="conn-loading">Loading your connections&hellip;</p>
      ) : connections.length === 0 ? (
        <p className="rpt-widget-empty">
          Connect a data source first. <Link href="/dashboard/connectors">Browse connections</Link>.
        </p>
      ) : (
        <>
          <div className="rpt-add-row">
            <div className="field">
              <label className="label" htmlFor="rpt_add_connection">
                Connection
              </label>
              <select
                className="input"
                id="rpt_add_connection"
                value={connectionId ?? ""}
                onChange={(e) => setConnectionId(Number(e.target.value))}
              >
                {connections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name.trim().length > 0 ? c.name : c.connector_label}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label className="label" htmlFor="rpt_add_tool">
                What to show
              </label>
              <select
                className="input"
                id="rpt_add_tool"
                value={toolName}
                disabled={loadingTools || (tools || []).length === 0}
                onChange={(e) => onToolChange(e.target.value)}
              >
                {(tools || []).map((t) => (
                  <option key={t.name} value={t.name}>
                    {label(t.name)}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label className="label" htmlFor="rpt_add_type">
                Shown as
              </label>
              <select className="input" id="rpt_add_type" value={type} onChange={(e) => setType(e.target.value as WidgetType)}>
                {WIDGET_TYPES.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {loadingTools ? <p className="conn-loading">Loading tools&hellip;</p> : null}

          {currentTool !== undefined && currentTool.description.length > 0 ? (
            <p className="rpt-add-hint">{currentTool.description}</p>
          ) : null}

          <div className="field">
            <label className="label" htmlFor="rpt_add_title">
              Title (optional)
            </label>
            <input
              className="input"
              id="rpt_add_title"
              type="text"
              value={title}
              placeholder={currentTool !== undefined ? label(currentTool.name) : ""}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>

          {extraRequired.length > 0 ? (
            <div className="rpt-add-row">
              {extraRequired.map((key) => (
                <div className="field" key={key}>
                  <label className="label" htmlFor={"rpt_add_arg_" + key}>
                    {label(key)}
                  </label>
                  <input
                    className="input"
                    id={"rpt_add_arg_" + key}
                    type="text"
                    value={argValues[key] || ""}
                    placeholder={currentTool?.params?.[key]?.description || ""}
                    onChange={(e) => {
                      const next = e.target.value;
                      setArgValues((prev) => ({ ...prev, [key]: next }));
                    }}
                  />
                </div>
              ))}
            </div>
          ) : null}
        </>
      )}

      <div className="rpt-add-actions">
        <button type="button" className="conn-action" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="conn-action conn-action-primary"
          disabled={connectionId === null || currentTool === undefined || missing.length > 0}
          onClick={submit}
        >
          Add widget
        </button>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                 */
/* ------------------------------------------------------------------ */

export default function SavedReportPage() {
  const params = useParams<{ id: string }>();
  const reportId = Number(params.id);

  const [report, setReport] = useState<SavedReport | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState<boolean>(false);

  const [name, setName] = useState<string>("");
  const [client, setClient] = useState<string>("");

  const [run, setRun] = useState<SavedReportRunResponse | null>(null);
  const [running, setRunning] = useState<boolean>(false);
  const [runError, setRunError] = useState<string | null>(null);

  const [saveNote, setSaveNote] = useState<string | null>(null);
  const [adding, setAdding] = useState<boolean>(false);
  const [existingClients, setExistingClients] = useState<string[]>([]);

  const aliveRef = useRef(true);
  const reportRef = useRef<SavedReport | null>(null);
  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  const runReport = useCallback(async function runReport(): Promise<void> {
    setRunning(true);
    setRunError(null);
    try {
      const next = await runSavedReport(reportId);
      if (aliveRef.current) setRun(next);
    } catch (caught) {
      if (aliveRef.current) setRunError(messageOf(caught, "This report could not be run."));
    } finally {
      if (aliveRef.current) setRunning(false);
    }
  }, [reportId]);

  const load = useCallback(async function load(): Promise<void> {
    try {
      const next = await getSavedReport(reportId);
      if (!aliveRef.current) return;
      setReport(next);
      reportRef.current = next;
      setName(next.name);
      setClient(next.client);
      setNotFound(false);
      setLoadError(null);
      void runReport();
    } catch (caught) {
      if (!aliveRef.current) return;
      if (caught instanceof ApiError && caught.status === 404) {
        setNotFound(true);
      } else {
        setLoadError(messageOf(caught, "This report could not be loaded."));
      }
    }
  }, [reportId, runReport]);

  useEffect(
    function loadOnMount() {
      void load();
    },
    [load]
  );

  /** Every client name already in use, so typing one is picking from a list
      instead of hoping to match an earlier report's spelling exactly. */
  useEffect(function loadClientNames() {
    listSavedReports()
      .then(function got(rows) {
        if (!aliveRef.current) return;
        const seen = new Set<string>();
        for (const row of rows) {
          const trimmed = row.client.trim();
          if (trimmed.length > 0) seen.add(trimmed);
        }
        setExistingClients(Array.from(seen).sort((a, b) => a.localeCompare(b)));
      })
      .catch(function ignore() {
        // Purely a convenience; a failed fetch just means no suggestions.
      });
  }, []);

  type Patch = Partial<Pick<SavedReport, "name" | "client" | "layout" | "filters">>;
  /**
   * Every autosave path funnels through here, and every save is chained onto
   * the one before it: `patcher` reads the report fresh at the moment ITS OWN
   * turn in the queue arrives, not when it was scheduled. Two edits fired back
   * to back (add a widget, then immediately toggle Compare) used to both read
   * the same stale `version` and race -- the second always lost to a 409 that
   * looked like someone else's edit, and was silently dropped. Reading late
   * instead of early means the second one now sees the first one's result and
   * simply saves on top of it, in order, like typing into the same document.
   */
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const enqueueSave = useCallback(
    function enqueueSave(patcher: (current: SavedReport) => Patch | null, rerun: boolean): Promise<void> {
      const run = async function run(): Promise<void> {
        const current = reportRef.current;
        if (current === null) return;
        const patch = patcher(current);
        if (patch === null) return;
        try {
          const saved = await updateSavedReport(reportId, { ...patch, version: current.version });
          if (!aliveRef.current) return;
          setReport(saved);
          reportRef.current = saved;
          setSaveNote(null);
          if (rerun) void runReport();
        } catch (caught) {
          if (!aliveRef.current) return;
          if (caught instanceof SavedReportConflictError) {
            setSaveNote("Someone else changed this report. Reloading the latest version…");
            await load();
            return;
          }
          setSaveNote(messageOf(caught, "That change could not be saved."));
        }
      };
      // Chained with .then(run, run): a queued save's own catch already
      // swallows everything, but this guarantees the queue can never wedge
      // itself even if that ever stops being true.
      const next = saveQueueRef.current.then(run, run);
      saveQueueRef.current = next;
      return next;
    },
    [reportId, runReport, load]
  );

  /* Name and client: saved a moment after typing stops, not on every
     keystroke -- and on their OWN timers, so editing one never cancels the
     other's pending save (they used to share one ref and silently drop
     whichever field's save lost the race to be cancelled). */
  const nameTimer = useRef<number | null>(null);
  const clientTimer = useRef<number | null>(null);
  function onNameChange(value: string): void {
    setName(value);
    if (nameTimer.current !== null) window.clearTimeout(nameTimer.current);
    nameTimer.current = window.setTimeout(() => {
      void enqueueSave((current) => (value.trim().length > 0 && value !== current.name ? { name: value } : null), false);
    }, 600);
  }
  function onClientChange(value: string): void {
    setClient(value);
    if (clientTimer.current !== null) window.clearTimeout(clientTimer.current);
    clientTimer.current = window.setTimeout(() => {
      void enqueueSave((current) => (value !== current.client ? { client: value } : null), false);
    }, 600);
  }

  function onDateRangeChange(range: SavedReportRangeId): void {
    const date: SavedReportDateFilter = { range: range };
    void enqueueSave((current) => ({ filters: { ...current.filters, date: date } }), true);
  }

  function onCompareChange(compare: boolean): void {
    void enqueueSave((current) => ({ filters: { ...current.filters, compare: compare } }), true);
  }

  function addWidget(widget: SavedReportWidget): void {
    void enqueueSave((current) => ({ layout: [...current.layout, { ...widget, y: nextY(current.layout) }] }), true);
    setAdding(false);
  }

  function removeWidget(widgetId: string): void {
    void enqueueSave((current) => ({ layout: current.layout.filter((w) => w.id !== widgetId) }), true);
  }

  if (notFound) {
    return (
      <div className="panel">
        <h1 className="panel-title">Report not found</h1>
        <p className="panel-lede">
          It may have been deleted. <Link href="/dashboard/reports">Back to reports</Link>.
        </p>
      </div>
    );
  }

  if (loadError !== null) {
    return (
      <div className="panel">
        <p className="error" role="alert">
          {loadError}
        </p>
      </div>
    );
  }

  if (report === null) {
    return (
      <div className="panel">
        <p className="conn-loading">Loading&hellip;</p>
      </div>
    );
  }

  const currentRange = report.filters.date?.range ?? "LAST_30_DAYS";
  const compareOn = report.filters.compare === true;

  return (
    <div className="panel panel-wide">
      <div className="rpt-editor-top">
        <Link href="/dashboard/reports" className="rpt-back">
          <ChevronLeft size={16} strokeWidth={2} aria-hidden="true" />
          All reports
        </Link>
      </div>

      <div className="rpt-editor-head">
        <div className="rpt-editor-titles">
          <input
            className="rpt-name-input"
            value={name}
            aria-label="Report name"
            onChange={(e) => onNameChange(e.target.value)}
          />
          <input
            className="rpt-client-input"
            value={client}
            placeholder="Client (optional)"
            aria-label="Client"
            list="rpt_client_options"
            onChange={(e) => onClientChange(e.target.value)}
          />
          {existingClients.length > 0 ? (
            <datalist id="rpt_client_options">
              {existingClients.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          ) : null}
        </div>

        <div className="rpt-editor-filters">
          <select
            className="input rpt-range-select"
            value={currentRange === "CUSTOM" ? "LAST_30_DAYS" : currentRange}
            aria-label="Date range"
            onChange={(e) => onDateRangeChange(e.target.value as SavedReportRangeId)}
          >
            {RANGES.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label}
              </option>
            ))}
          </select>
          <label className="rpt-compare">
            <input type="checkbox" checked={compareOn} onChange={(e) => onCompareChange(e.target.checked)} />
            Compare to previous period
          </label>
        </div>
      </div>

      {saveNote !== null ? (
        <p className="conn-note" role="status">
          {saveNote}
        </p>
      ) : running && run !== null ? (
        <p className="conn-note" role="status">
          {"Refreshing…"}
        </p>
      ) : null}
      {runError !== null ? (
        <p className="error" role="alert">
          {runError}
        </p>
      ) : null}

      <div className="panel-body">
        {report.layout.length === 0 && !adding ? (
          <div className="rpt-widget-empty rpt-empty-canvas">
            <p>This report has no widgets yet.</p>
            <button type="button" className="conn-action conn-action-primary" onClick={() => setAdding(true)}>
              <Plus size={14} strokeWidth={2} aria-hidden="true" /> Add a widget
            </button>
          </div>
        ) : (
          <>
            <div className="rpt-canvas">
              {report.layout.map((widget) => {
                // Keep showing the LAST run's data while a new one is in
                // flight: swapping every widget to its loading skeleton on
                // every edit (a rename, a date-range change, a removed
                // widget elsewhere on the canvas) was a full-canvas flash for
                // widgets nothing about the change actually touched.
                const runs = widgetRuns(run, widget.id);
                return (
                  <WidgetFrame key={widget.id} title={widget.title || label(widget.source.tool)} onRemove={() => removeWidget(widget.id)}>
                    {widget.type === "kpi" ? (
                      <KpiWidget widget={widget} run={runs} />
                    ) : widget.type === "bar" ? (
                      <BarWidget widget={widget} run={runs} />
                    ) : (
                      <TableWidget widget={widget} run={runs} />
                    )}
                  </WidgetFrame>
                );
              })}
            </div>

            {!adding ? (
              <button type="button" className="conn-action rpt-add-trigger" onClick={() => setAdding(true)}>
                <Plus size={14} strokeWidth={2} aria-hidden="true" /> Add a widget
              </button>
            ) : null}
          </>
        )}

        {adding ? <AddWidgetPanel onAdd={addWidget} onCancel={() => setAdding(false)} /> : null}
      </div>
    </div>
  );
}
