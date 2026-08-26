"use client";

/**
 * The SQL console.
 *
 * Four things share the screen because a query tool that hides any of them
 * makes you keep it in your head instead: the schema you are writing against,
 * the statement, the rows it produced, and what you ran before.
 *
 * Two decisions worth stating, because both look like bugs from outside:
 *
 * A failed query is not an error state. `POST /runs/` answers 200 with
 * `state: "FAILED"` and PostgreSQL's own message, and that message goes in the
 * results pane where the rows would be. "relation \"acounts\" does not exist"
 * is the useful thing; a red toast saying "Something went wrong" is not.
 * `runError` is reserved for the request itself failing -- offline, 429, 500.
 *
 * The draft is kept in localStorage per workspace. Losing half an hour of SQL
 * to a refresh is the fastest way to make someone stop trusting a tool, and a
 * draft is exactly the kind of per-browser convenience that storage is for. It
 * is a convenience and not a save: the Save button, and the server, are what
 * make a query exist for anyone else.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Check,
  Clock,
  Database,
  FileCode2,
  Info,
  Loader2,
  Play,
  Plus,
  Save,
  SquareTerminal,
  Table2,
  Trash2,
  X,
} from "lucide-react";
import CodeEditor from "@/components/ide/CodeEditor";
import type { CodeEditorHandle, SqlSchema } from "@/components/ide/CodeEditor";
import ResultTable from "@/components/ide/ResultTable";
import SchemaTree from "@/components/ide/SchemaTree";
import SplitPane from "@/components/ide/SplitPane";
import {
  createQuery,
  deleteQuery,
  fetchSchema,
  getRun,
  listDataSources,
  listQueries,
  listRuns,
  listWorkspaces,
  runQuery,
  updateQuery,
} from "@/lib/api";
import type {
  DataSource,
  QueryRun,
  QueryRunSummary,
  SavedQuery,
  SchemaTable,
  Workspace,
} from "@/lib/api";

const STARTER = "select 1;\n";
const DRAFT_KEY = "honeycomb.sql.draft.";
const ROW_LIMITS = [100, 1000, 5000];

function message(caught: unknown): string {
  return caught instanceof Error
    ? caught.message
    : "Something went wrong. Please try again.";
}

/** localStorage throws in a few real browsers; a lost draft is not worth a crash. */
function readDraft(workspace: number): string | null {
  try {
    return window.localStorage.getItem(DRAFT_KEY + workspace);
  } catch (caught) {
    return null;
  }
}

function writeDraft(workspace: number, sql: string): void {
  try {
    window.localStorage.setItem(DRAFT_KEY + workspace, sql);
  } catch (caught) {
    /* Private mode, or site data blocked. The editor still works. */
  }
}

/** "1,204" -- digits are what a row count is read for. */
function thousands(value: number): string {
  return value.toLocaleString("en-US");
}

/**
 * A sentence to add under a failed run, when the database's own message is
 * accurate but answers a question the person did not mean to ask.
 *
 * This is not a guard and it does not inspect a statement before running it --
 * the connector deliberately never does that. It runs only after the server
 * has already refused, and only to translate a refusal that reads as
 * "you lack permission" into what actually happened.
 */
function hintFor(sql: string, database: string | undefined): string | null {
  const head = sql.trim().toLowerCase();
  if (head.indexOf("use ") === 0 || head === "use") {
    return (
      "Every run opens its own connection, so USE has nothing to carry over to" +
      (database ? ' — the source is already connected to "' + database + '".' : ".") +
      " Query the tables directly instead."
    );
  }
  return null;
}

function elapsed(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) {
    return seconds + "s ago";
  }
  if (seconds < 3600) {
    return Math.round(seconds / 60) + "m ago";
  }
  if (seconds < 86400) {
    return Math.round(seconds / 3600) + "h ago";
  }
  return new Date(iso).toLocaleDateString();
}

export default function SqlConsolePage() {
  const editorRef = useRef<CodeEditorHandle | null>(null);

  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  const [workspaceId, setWorkspaceId] = useState<number | null>(null);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [sourceId, setSourceId] = useState<number | null>(null);

  const [sql, setSql] = useState(STARTER);
  const [limit, setLimit] = useState(ROW_LIMITS[1]);

  const [schema, setSchema] = useState<SchemaTable[] | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  const [run, setRun] = useState<QueryRun | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const [history, setHistory] = useState<QueryRunSummary[]>([]);
  const [saved, setSaved] = useState<SavedQuery[]>([]);
  const [openQuery, setOpenQuery] = useState<SavedQuery | null>(null);

  const [sidebar, setSidebar] = useState<"schema" | "saved">("schema");
  const [pane, setPane] = useState<"results" | "history">("results");

  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState(false);

  /* ---- loading ---- */

  useEffect(function loadWorkspaces() {
    let cancelled = false;
    listWorkspaces()
      .then(function keep(list) {
        if (cancelled) {
          return;
        }
        setWorkspaces(list);
        if (list.length > 0) {
          setWorkspaceId(list[0].id);
        }
      })
      .catch(function empty() {
        if (!cancelled) {
          setWorkspaces([]);
        }
      });
    return function cleanup() {
      cancelled = true;
    };
  }, []);

  useEffect(
    function loadWorkspaceContents() {
      if (workspaceId === null) {
        return;
      }
      let cancelled = false;

      const draft = readDraft(workspaceId);
      setSql(draft === null ? STARTER : draft);
      setOpenQuery(null);
      setRun(null);
      setSchema(null);
      setSchemaError(null);

      listDataSources(workspaceId)
        .then(function keep(list) {
          if (cancelled) {
            return;
          }
          setSources(list);
          // Prefer a source that has actually connected: an untested one is
          // the likeliest to fail, and starting on it makes the console look
          // broken when it is the source that is not set up.
          const connected = list.find(function ready(source) {
            return source.status === "CONNECTED";
          });
          setSourceId(connected ? connected.id : list.length > 0 ? list[0].id : null);
        })
        .catch(function empty() {
          if (!cancelled) {
            setSources([]);
            setSourceId(null);
          }
        });

      listQueries(workspaceId)
        .then(function keep(list) {
          if (!cancelled) {
            setSaved(list);
          }
        })
        .catch(function empty() {
          if (!cancelled) {
            setSaved([]);
          }
        });

      listRuns(workspaceId)
        .then(function keep(list) {
          if (!cancelled) {
            setHistory(list);
          }
        })
        .catch(function empty() {
          if (!cancelled) {
            setHistory([]);
          }
        });

      return function cleanup() {
        cancelled = true;
      };
    },
    [workspaceId]
  );

  useEffect(
    function persistDraft() {
      if (workspaceId === null) {
        return;
      }
      // Debounced: writing localStorage on every keystroke is synchronous work
      // on the typing path, and nobody needs a draft saved per character.
      const timer = window.setTimeout(function save() {
        writeDraft(workspaceId, sql);
      }, 400);
      return function cleanup() {
        window.clearTimeout(timer);
      };
    },
    [workspaceId, sql]
  );

  const loadSchema = useCallback(
    function loadSchema(): void {
      if (sourceId === null) {
        setSchema(null);
        return;
      }
      setSchemaLoading(true);
      setSchemaError(null);
      fetchSchema(sourceId)
        .then(function keep(tables) {
          setSchema(tables);
        })
        .catch(function fail(caught) {
          setSchema(null);
          setSchemaError(message(caught));
        })
        .finally(function done() {
          setSchemaLoading(false);
        });
    },
    [sourceId]
  );

  useEffect(
    function loadSchemaOnSource() {
      loadSchema();
    },
    [loadSchema]
  );

  /* ---- derived ---- */

  const source = useMemo(
    function findSource() {
      return sources.find(function match(entry) {
        return entry.id === sourceId;
      });
    },
    [sources, sourceId]
  );

  const completionSchema = useMemo<SqlSchema>(
    function buildCompletionSchema() {
      const map: SqlSchema = {};
      if (schema === null) {
        return map;
      }
      for (let index = 0; index < schema.length; index += 1) {
        const table = schema[index];
        map[table.qualified] = table.columns.map(function name(column) {
          return column.name;
        });
      }
      return map;
    },
    [schema]
  );

  const canRun =
    workspaceId !== null && sourceId !== null && sql.trim() !== "" && !running;

  /* ---- actions ---- */

  const execute = useCallback(
    async function execute(): Promise<void> {
      if (workspaceId === null || sourceId === null) {
        return;
      }
      // A selection means "run this bit". It is how anyone works through a
      // scratch buffer holding half a dozen statements.
      const selected = editorRef.current ? editorRef.current.getSelection() : "";
      const statement = (selected || sql).trim();
      if (statement === "") {
        return;
      }

      setRunning(true);
      setRunError(null);
      setPane("results");
      try {
        const result = await runQuery({
          workspace: workspaceId,
          data_source: sourceId,
          sql: statement,
          saved_query: openQuery === null ? null : openQuery.id,
          limit: limit,
        });
        setRun(result);
        const runs = await listRuns(workspaceId);
        setHistory(runs);
      } catch (caught) {
        setRunError(message(caught));
      } finally {
        setRunning(false);
      }
    },
    [workspaceId, sourceId, sql, limit, openQuery]
  );

  useEffect(
    function bindGlobalShortcut() {
      // The editor binds Mod-Enter itself; this covers the case where focus is
      // in the source picker or the schema filter, which is where it often is
      // right before someone wants to run something.
      function onKey(event: KeyboardEvent): void {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
          event.preventDefault();
          void execute();
        }
      }
      window.addEventListener("keydown", onKey);
      return function cleanup() {
        window.removeEventListener("keydown", onKey);
      };
    },
    [execute]
  );

  function openSaved(query: SavedQuery): void {
    setOpenQuery(query);
    setSql(query.sql);
    setRun(null);
    if (query.data_source !== null) {
      setSourceId(query.data_source);
    }
    setPane("results");
  }

  function startNew(): void {
    setOpenQuery(null);
    setSql(STARTER);
    setRun(null);
    if (editorRef.current !== null) {
      editorRef.current.focus();
    }
  }

  async function openRun(entry: QueryRunSummary): Promise<void> {
    try {
      const full = await getRun(entry.id);
      setRun(full);
      setSql(full.sql);
      if (full.data_source !== null) {
        setSourceId(full.data_source);
      }
      setPane("results");
    } catch (caught) {
      setRunError(message(caught));
    }
  }

  async function save(): Promise<void> {
    if (workspaceId === null) {
      return;
    }
    setSaveError(null);

    // An open query saves over itself. Asking for a name again would make
    // every edit a new row, and a saved-query list full of "Revenue (2)" is
    // how a shared workspace stops being useful.
    if (openQuery !== null) {
      try {
        const updated = await updateQuery(openQuery.id, { sql: sql });
        setOpenQuery(updated);
        setSaved(await listQueries(workspaceId));
        flashSaved();
      } catch (caught) {
        setSaveError(message(caught));
      }
      return;
    }

    if (!saveOpen) {
      setSaveName("");
      setSaveOpen(true);
      return;
    }

    const name = saveName.trim();
    if (name === "") {
      setSaveError("Give the query a name.");
      return;
    }
    try {
      const created = await createQuery({
        workspace: workspaceId,
        data_source: sourceId,
        name: name,
        sql: sql,
      });
      setOpenQuery(created);
      setSaved(await listQueries(workspaceId));
      setSaveOpen(false);
      flashSaved();
    } catch (caught) {
      setSaveError(message(caught));
    }
  }

  function flashSaved(): void {
    setSavedFlash(true);
    window.setTimeout(function reset() {
      setSavedFlash(false);
    }, 1600);
  }

  async function removeSaved(query: SavedQuery): Promise<void> {
    if (workspaceId === null) {
      return;
    }
    try {
      await deleteQuery(query.id);
      if (openQuery !== null && openQuery.id === query.id) {
        setOpenQuery(null);
      }
      setSaved(await listQueries(workspaceId));
    } catch (caught) {
      setSaveError(message(caught));
    }
  }

  /* ---- gates ---- */

  if (workspaces !== null && workspaces.length === 0) {
    return (
      <div className="ide ide-gate">
        <div className="ide-gate-card">
          <Table2 size={22} strokeWidth={1.5} aria-hidden="true" />
          <h1>The console needs a workspace</h1>
          <p>
            Sources, queries and results all live inside one.{" "}
            <Link href="/dashboard/workspaces?new">Create a workspace</Link> and
            come back.
          </p>
        </div>
      </div>
    );
  }

  /* ---- render ---- */

  const sourceMissing = sources.length === 0 && workspaceId !== null;

  return (
    <div className="ide">
      <header className="ide-bar">
        <div className="ide-bar-left">
          <label className="dash-visually-hidden" htmlFor="ide-workspace">
            Workspace
          </label>
          <select
            id="ide-workspace"
            className="ide-select"
            value={workspaceId === null ? "" : String(workspaceId)}
            onChange={function change(event) {
              setWorkspaceId(Number(event.target.value));
            }}
          >
            {(workspaces || []).map(function option(workspace) {
              return (
                <option key={workspace.id} value={workspace.id}>
                  {workspace.name}
                </option>
              );
            })}
          </select>

          <span className="ide-bar-sep" aria-hidden="true" />

          <label className="dash-visually-hidden" htmlFor="ide-source">
            Data source
          </label>
          <select
            id="ide-source"
            className="ide-select"
            value={sourceId === null ? "" : String(sourceId)}
            disabled={sources.length === 0}
            onChange={function change(event) {
              setSourceId(Number(event.target.value));
            }}
          >
            {sources.length === 0 ? <option value="">No sources</option> : null}
            {sources.map(function option(entry) {
              return (
                <option key={entry.id} value={entry.id}>
                  {entry.name}
                </option>
              );
            })}
          </select>

          {source !== undefined ? (
            <span
              className={"ide-dot ide-dot-" + source.status.toLowerCase()}
              title={
                source.status === "CONNECTED"
                  ? "Connected"
                  : source.status === "FAILED"
                  ? source.last_error || "The last connection failed"
                  : "Not tested yet"
              }
              aria-hidden="true"
            />
          ) : null}
        </div>

        <div className="ide-bar-right">
          <label className="dash-visually-hidden" htmlFor="ide-limit">
            Row limit
          </label>
          <select
            id="ide-limit"
            className="ide-select ide-select-narrow"
            value={String(limit)}
            onChange={function change(event) {
              setLimit(Number(event.target.value));
            }}
            title="Rows to fetch"
          >
            {ROW_LIMITS.map(function option(value) {
              return (
                <option key={value} value={value}>
                  {thousands(value)} rows
                </option>
              );
            })}
          </select>

          <button
            type="button"
            className="ide-button"
            onClick={function onSave() {
              void save();
            }}
            disabled={sql.trim() === "" || workspaceId === null}
            title={openQuery === null ? "Save as a new query" : "Save over " + openQuery.name}
          >
            {savedFlash ? (
              <Check size={15} strokeWidth={2} aria-hidden="true" />
            ) : (
              <Save size={15} strokeWidth={1.75} aria-hidden="true" />
            )}
            {savedFlash ? "Saved" : "Save"}
          </button>

          <button
            type="button"
            className="ide-button ide-button-primary"
            onClick={function onRun() {
              void execute();
            }}
            disabled={!canRun}
          >
            {running ? (
              <Loader2 size={15} className="ide-spin" aria-hidden="true" />
            ) : (
              <Play size={15} strokeWidth={2} aria-hidden="true" />
            )}
            {running ? "Running" : "Run"}
            <kbd className="ide-kbd">⌘↵</kbd>
          </button>
        </div>
      </header>

      {saveOpen && openQuery === null ? (
        <div className="ide-savebar">
          <input
            className="ide-saveinput"
            value={saveName}
            autoFocus
            placeholder="Name this query"
            aria-label="Name this query"
            onChange={function change(event) {
              setSaveName(event.target.value);
            }}
            onKeyDown={function key(event) {
              if (event.key === "Enter") {
                void save();
              } else if (event.key === "Escape") {
                setSaveOpen(false);
              }
            }}
          />
          <button type="button" className="ide-button" onClick={function go() { void save(); }}>
            Save
          </button>
          <button
            type="button"
            className="ide-icon-button"
            aria-label="Cancel"
            onClick={function cancel() {
              setSaveOpen(false);
              setSaveError(null);
            }}
          >
            <X size={14} strokeWidth={1.75} aria-hidden="true" />
          </button>
          {saveError !== null ? <span className="ide-saveerror">{saveError}</span> : null}
        </div>
      ) : null}

      <div className="ide-body">
        <div className="ide-side">
          <div className="ide-side-tabs" role="tablist" aria-label="Sidebar">
            <button
              type="button"
              role="tab"
              aria-selected={sidebar === "schema"}
              className={sidebar === "schema" ? "ide-tab ide-tab-on" : "ide-tab"}
              onClick={function pick() {
                setSidebar("schema");
              }}
            >
              Schema
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={sidebar === "saved"}
              className={sidebar === "saved" ? "ide-tab ide-tab-on" : "ide-tab"}
              onClick={function pick() {
                setSidebar("saved");
              }}
            >
              Saved
              {saved.length > 0 ? <span className="ide-tab-count">{saved.length}</span> : null}
            </button>
          </div>

          {sidebar === "schema" ? (
            <SchemaTree
              tables={schema}
              loading={schemaLoading}
              error={schemaError}
              database={source === undefined ? undefined : source.database}
              onRefresh={loadSchema}
              onPick={function pick(text) {
                if (editorRef.current !== null) {
                  editorRef.current.insert(text);
                }
              }}
            />
          ) : (
            <div className="ide-saved">
              <button type="button" className="ide-newquery" onClick={startNew}>
                <Plus size={13} strokeWidth={2} aria-hidden="true" />
                New query
              </button>
              {saved.length === 0 ? (
                <p className="ide-schema-note">
                  Nothing saved yet. Write something and press Save.
                </p>
              ) : (
                <ul className="ide-saved-list">
                  {saved.map(function renderSaved(query) {
                    const open = openQuery !== null && openQuery.id === query.id;
                    return (
                      <li key={query.id} className={open ? "ide-saved-on" : undefined}>
                        <button
                          type="button"
                          className="ide-saved-item"
                          onClick={function pick() {
                            openSaved(query);
                          }}
                        >
                          <FileCode2 size={13} strokeWidth={1.75} aria-hidden="true" />
                          <span className="ide-saved-name">{query.name}</span>
                        </button>
                        <button
                          type="button"
                          className="ide-icon-button"
                          aria-label={"Delete " + query.name}
                          title="Delete"
                          onClick={function drop() {
                            void removeSaved(query);
                          }}
                        >
                          <Trash2 size={13} strokeWidth={1.75} aria-hidden="true" />
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="ide-main">
          {sourceMissing ? (
            <p className="ide-warn">
              <AlertTriangle size={14} strokeWidth={1.75} aria-hidden="true" />
              This workspace has no data source.{" "}
              <Link href="/dashboard/sources?new">Connect one</Link> before running
              anything.
            </p>
          ) : null}

          {source !== undefined && !source.secret_configured ? (
            <p className="ide-warn">
              <AlertTriangle size={14} strokeWidth={1.75} aria-hidden="true" />
              No password is stored for <strong>{source.name}</strong>. Set{" "}
              <code>{source.secret_env_var || "its secret name"}</code> in the
              backend environment and restart it.
            </p>
          ) : null}

          <SplitPane
            initialTop={320}
            minTop={140}
            minBottom={160}
            label="Resize the editor"
            top={
              <div className="ide-editor-frame">
                {openQuery !== null ? (
                  <div className="ide-openfile">
                    <FileCode2 size={12} strokeWidth={1.75} aria-hidden="true" />
                    {openQuery.name}
                    <button
                      type="button"
                      className="ide-openfile-close"
                      aria-label="Close this query"
                      onClick={startNew}
                    >
                      <X size={12} strokeWidth={2} aria-hidden="true" />
                    </button>
                  </div>
                ) : null}
                <CodeEditor
                  ref={editorRef}
                  value={sql}
                  onChange={setSql}
                  language="sql"
                  schema={completionSchema}
                  onRun={execute}
                  ariaLabel="SQL editor"
                  placeholder={"select *\nfrom accounts\norder by mrr desc\nlimit 50"}
                />
              </div>
            }
            bottom={
              <div className="ide-output">
                <div className="ide-output-tabs" role="tablist" aria-label="Output">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={pane === "results"}
                    className={pane === "results" ? "ide-tab ide-tab-on" : "ide-tab"}
                    onClick={function pick() {
                      setPane("results");
                    }}
                  >
                    Results
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={pane === "history"}
                    className={pane === "history" ? "ide-tab ide-tab-on" : "ide-tab"}
                    onClick={function pick() {
                      setPane("history");
                    }}
                  >
                    History
                    {history.length > 0 ? (
                      <span className="ide-tab-count">{history.length}</span>
                    ) : null}
                  </button>

                  {run !== null && pane === "results" ? (
                    <span className="ide-status">
                      {run.state === "SUCCEEDED" ? (
                        <>
                          <Check size={12} strokeWidth={2.5} aria-hidden="true" />
                          {thousands(run.row_count)}
                          {run.truncated ? "+" : ""} row
                          {run.row_count === 1 ? "" : "s"}
                          <span className="ide-status-dim">·</span>
                          {run.duration_ms} ms
                        </>
                      ) : (
                        <>
                          <AlertTriangle size={12} strokeWidth={2} aria-hidden="true" />
                          Failed
                        </>
                      )}
                    </span>
                  ) : null}
                </div>

                <div className="ide-output-body">
                  {pane === "results" ? (
                    <>
                      {runError !== null ? (
                        <p className="ide-error">{runError}</p>
                      ) : null}

                      {run === null && runError === null ? (
                        <div className="ide-blank">
                          <Database size={20} strokeWidth={1.5} aria-hidden="true" />
                          <p>
                            Press Run to execute the statement, or select part of it
                            to run just that.
                          </p>
                        </div>
                      ) : null}

                      {run !== null && run.state === "FAILED" ? (
                        <div className="ide-fail">
                          <span className="ide-fail-head">
                            <AlertTriangle size={14} strokeWidth={2} aria-hidden="true" />
                            The database refused this statement
                          </span>
                          <pre className="ide-fail-body">{run.error}</pre>
                          {(function renderHint() {
                            const hint = hintFor(
                              run.sql,
                              source === undefined ? undefined : source.database
                            );
                            return hint === null ? null : (
                              <p className="ide-fail-hint">
                                <Info size={13} strokeWidth={1.75} aria-hidden="true" />
                                {hint}
                              </p>
                            );
                          })()}
                        </div>
                      ) : null}

                      {run !== null && run.state === "SUCCEEDED" ? (
                        <>
                          {run.notice ? (
                            <p className="ide-notice">{run.notice}</p>
                          ) : null}
                          {run.row_count > 0 ? (
                            // Carries the run id, not the rows: the Python
                            // console re-reads them from the API, so a link
                            // someone bookmarks or pastes still works and the
                            // rows never take a detour through the URL.
                            <p className="ide-handoff">
                              <Link href={"/dashboard/python?run=" + run.id}>
                                <SquareTerminal
                                  size={13}
                                  strokeWidth={1.75}
                                  aria-hidden="true"
                                />
                                Open these rows in Python
                              </Link>
                            </p>
                          ) : null}
                          <ResultTable
                            columns={run.result_columns}
                            rows={run.result_rows as unknown[][]}
                            name={openQuery === null ? "result" : openQuery.name}
                          />
                        </>
                      ) : null}
                    </>
                  ) : (
                    <ul className="ide-history">
                      {history.length === 0 ? (
                        <li className="ide-schema-note">Nothing has run here yet.</li>
                      ) : null}
                      {history.map(function renderRun(entry) {
                        return (
                          <li key={entry.id}>
                            <button
                              type="button"
                              className="ide-history-item"
                              onClick={function open() {
                                void openRun(entry);
                              }}
                            >
                              <span
                                className={
                                  entry.state === "SUCCEEDED"
                                    ? "ide-dot ide-dot-connected"
                                    : "ide-dot ide-dot-failed"
                                }
                                aria-hidden="true"
                              />
                              <code className="ide-history-sql">{entry.preview}</code>
                              <span className="ide-history-meta">
                                {entry.state === "SUCCEEDED"
                                  ? thousands(entry.row_count) + " rows · " + entry.duration_ms + " ms"
                                  : "failed"}
                                <span className="ide-status-dim">·</span>
                                <Clock size={11} strokeWidth={1.75} aria-hidden="true" />
                                {elapsed(entry.created_at)}
                              </span>
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              </div>
            }
          />
        </div>
      </div>
    </div>
  );
}
