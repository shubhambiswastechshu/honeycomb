"use client";

/**
 * The Python console.
 *
 * The code runs in this tab, on Pyodide, and never on the server -- see
 * lib/pyodide.ts for why that is the design rather than a stopgap, and
 * notebooks/models.py for the same rule stated where the data lives.
 *
 * Two consequences the interface has to be honest about, because a console
 * that quietly does less than it looks like is worse than one that says so:
 *
 *   The first Run downloads about 10MB. The button says so before it is
 *   pressed, and shows what stage the load is at while it happens.
 *
 *   The tab holds no database credentials and never will. It reads data by
 *   asking the server to run the statement -- `await honeycomb.query(...)`,
 *   see lib/pybridge.ts -- which is the same endpoint the SQL console uses,
 *   with the same read-only role, row cap, timeout and rate limit. So Python
 *   can read a warehouse without anything in this tab being able to reach one.
 *
 * The interpreter is kept alive between runs, so names defined in one run are
 * there in the next, the way a REPL works. "Reset" is what drops it.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Check,
  Eraser,
  FileCode2,
  Info,
  Loader2,
  Play,
  Plus,
  RotateCcw,
  Save,
  Table2,
  Trash2,
  X,
} from "lucide-react";
import CodeEditor from "@/components/ide/CodeEditor";
import type { CodeEditorHandle } from "@/components/ide/CodeEditor";
import SplitPane from "@/components/ide/SplitPane";
import {
  createScript,
  deleteScript,
  getRun,
  listDataSources,
  listScripts,
  listWorkspaces,
  updateScript,
} from "@/lib/api";
import type { DataSource, PythonScript, QueryRun, Workspace } from "@/lib/api";
import { getPyodide, PYODIDE_VERSION } from "@/lib/pyodide";
import { BOOTSTRAP, payloadFor, runForPython } from "@/lib/pybridge";

const STARTER = [
  "# Runs in your browser, on Pyodide. Nothing is sent to the server.",
  "# The standard library is here, and `import numpy` or `import pandas`",
  "# fetches the package on first use.",
  "",
  'print("hello from", __import__("sys").version.split()[0])',
  "",
].join("\n");

const DRAFT_KEY = "honeycomb.python.draft.";

type Stream = "out" | "err" | "meta";

interface Line {
  id: number;
  stream: Stream;
  text: string;
}

function message(caught: unknown): string {
  return caught instanceof Error
    ? caught.message
    : "Something went wrong. Please try again.";
}

function readDraft(workspace: number): string | null {
  try {
    return window.localStorage.getItem(DRAFT_KEY + workspace);
  } catch (caught) {
    return null;
  }
}

function writeDraft(workspace: number, code: string): void {
  try {
    window.localStorage.setItem(DRAFT_KEY + workspace, code);
  } catch (caught) {
    /* Private mode, or site data blocked. */
  }
}

export default function PythonConsolePage() {
  const editorRef = useRef<CodeEditorHandle | null>(null);
  const outputRef = useRef<HTMLDivElement | null>(null);
  // Line ids come from a counter rather than from the array length: output is
  // appended in batches while an earlier batch may still be in flight, and
  // length-based keys collide the moment two batches land in one tick.
  const nextId = useRef(0);

  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  const [workspaceId, setWorkspaceId] = useState<number | null>(null);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [sourceId, setSourceId] = useState<number | null>(null);
  // A run handed over from the SQL console via ?run=. Its rows are waiting in
  // `honeycomb.rows` so the first thing anyone writes is about the data rather
  // than about fetching it.
  const [attached, setAttached] = useState<QueryRun | null>(null);
  const [scripts, setScripts] = useState<PythonScript[]>([]);
  const [openScript, setOpenScript] = useState<PythonScript | null>(null);

  const [code, setCode] = useState(STARTER);
  const [lines, setLines] = useState<Line[]>([]);
  const [running, setRunning] = useState(false);
  const [booting, setBooting] = useState(false);
  const [ready, setReady] = useState(false);
  // The interpreter version, read from the runtime once it is up. Pyodide's
  // own version is not Python's -- 0.26.4 ships CPython 3.12 -- and a pill
  // reading "Python 0.26.4" is simply a wrong fact on the screen.
  const [pythonVersion, setPythonVersion] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState(false);

  const contextRef = useRef({
    workspaceId: null as number | null,
    sourceId: null as number | null,
  });
  contextRef.current = { workspaceId: workspaceId, sourceId: sourceId };
  const bridgeReady = useRef(false);
  // The source a handed-over run came from. Setting `sourceId` directly is not
  // enough: choosing the workspace kicks off the source fetch, which finishes
  // later and picks a default, silently replacing it. Then `honeycomb.query()`
  // would run against a different database than the rows on screen came from.
  const wantedSource = useRef<number | null>(null);

  const append = useCallback(function append(stream: Stream, text: string): void {
    setLines(function add(current) {
      const id = nextId.current;
      nextId.current += 1;
      return current.concat([{ id: id, stream: stream, text: text }]);
    });
  }, []);

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
    function loadScriptsForWorkspace() {
      if (workspaceId === null) {
        return;
      }
      let cancelled = false;
      const draft = readDraft(workspaceId);
      setCode(draft === null ? STARTER : draft);
      setOpenScript(null);

      listScripts(workspaceId)
        .then(function keep(list) {
          if (!cancelled) {
            setScripts(list);
          }
        })
        .catch(function empty() {
          if (!cancelled) {
            setScripts([]);
          }
        });

      listDataSources(workspaceId)
        .then(function keep(list) {
          if (cancelled) {
            return;
          }
          setSources(list);
          const wanted = wantedSource.current;
          const asked =
            wanted === null
              ? undefined
              : list.find(function match(source) {
                  return source.id === wanted;
                });
          if (asked !== undefined) {
            wantedSource.current = null;
            setSourceId(asked.id);
            return;
          }
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

      return function cleanup() {
        cancelled = true;
      };
    },
    [workspaceId]
  );

  useEffect(function attachRunFromUrl() {
    // Read from window rather than useSearchParams: the hook forces the page
    // into a Suspense boundary for static rendering, and one query parameter
    // is not worth that.
    if (typeof window === "undefined") {
      return;
    }
    const id = new URLSearchParams(window.location.search).get("run");
    if (id === null) {
      return;
    }
    let cancelled = false;
    getRun(Number(id))
      .then(function keep(run) {
        if (cancelled || run.state !== "SUCCEEDED") {
          return;
        }
        setAttached(run);
        wantedSource.current = run.data_source;
        setWorkspaceId(run.workspace);
        if (run.data_source !== null) {
          setSourceId(run.data_source);
        }
      })
      .catch(function ignore() {
        /* A stale or foreign run id. The console still works without it. */
      });
    return function cleanup() {
      cancelled = true;
    };
  }, []);

  useEffect(
    function persistDraft() {
      if (workspaceId === null) {
        return;
      }
      const timer = window.setTimeout(function save() {
        writeDraft(workspaceId, code);
      }, 400);
      return function cleanup() {
        window.clearTimeout(timer);
      };
    },
    [workspaceId, code]
  );

  useEffect(
    function followOutput() {
      const host = outputRef.current;
      if (host !== null) {
        host.scrollTop = host.scrollHeight;
      }
    },
    [lines]
  );

  const workspaceName = (workspaces || []).find(function match(entry) {
    return entry.id === workspaceId;
  })?.name;
  const currentSource = sources.find(function match(entry) {
    return entry.id === sourceId;
  });
  const sourceName = currentSource?.name;

  /* ---- running ---- */

  const execute = useCallback(
    async function execute(): Promise<void> {
      const selected = editorRef.current ? editorRef.current.getSelection() : "";
      const program = (selected || code).trim();
      if (program === "" || running) {
        return;
      }

      setRunning(true);
      setLoadError(null);

      let runtime;
      try {
        if (!ready) {
          setBooting(true);
          append("meta", "Starting Pyodide " + PYODIDE_VERSION + "…");
        }
        runtime = await getPyodide();
        if (!bridgeReady.current) {
          // Once per runtime. The module object closes over contextRef, so a
          // later change to the pickers is seen without re-registering --
          // registering twice is an error in Pyodide.
          runtime.registerJsModule("_honeycomb_bridge", {
            run_query: function run(sql: string, limit: number | null) {
              return runForPython(contextRef.current, sql, limit);
            },
          });
          await runtime.runPythonAsync(BOOTSTRAP);
          bridgeReady.current = true;
        }
        if (!ready) {
          const version = String(
            await runtime.runPythonAsync("__import__('sys').version.split()[0]")
          );
          setPythonVersion(version);
          setReady(true);
          setBooting(false);
          append("meta", "Python " + version + " ready.");
        }
      } catch (caught) {
        setBooting(false);
        setRunning(false);
        setLoadError(message(caught));
        return;
      }

      // Rebound before every run: setStdout replaces the handler, and after a
      // Reset the previous closure would be writing into a state setter whose
      // component has already moved on.
      runtime.setStdout({
        batched: function onOut(text: string) {
          append("out", text);
        },
      });
      runtime.setStderr({
        batched: function onErr(text: string) {
          append("err", text);
        },
      });

      try {
        // Refreshed before every run rather than at startup, so `honeycomb`
        // describes the pickers as they are now. JSON.stringify because the
        // bootstrap parses it -- see lib/pybridge.ts on why a string rather
        // than an object crosses this boundary.
        const state = JSON.stringify({
          workspace: workspaceName,
          source: sourceName,
          database: currentSource?.database || null,
          attached: attached === null ? null : payloadFor(attached),
        });
        await runtime.runPythonAsync(
          "import honeycomb\nhoneycomb._set_context(" +
            JSON.stringify(state) +
            ")"
        );

        // Reads the imports and fetches numpy, pandas and friends from the CDN
        // before the program runs, so `import pandas` works without anyone
        // having to know micropip exists.
        await runtime.loadPackagesFromImports(program);
        const value = await runtime.runPythonAsync(program);
        if (value !== undefined && value !== null) {
          // The value of the last expression, the way a REPL echoes it. A
          // script ending in a call that returns None prints nothing, which is
          // also what a REPL does.
          append("out", String(value));
        }
      } catch (caught) {
        // Pyodide puts the whole Python traceback in the message, and the
        // traceback is the useful part -- it is shown verbatim rather than
        // reduced to "an error occurred".
        append("err", message(caught));
      } finally {
        setRunning(false);
      }
    },
    [code, running, ready, append, attached, workspaceName, sourceName, currentSource]
  );

  useEffect(
    function bindShortcut() {
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

  async function resetInterpreter(): Promise<void> {
    if (!ready) {
      return;
    }
    try {
      const runtime = await getPyodide();
      if (runtime.globals.clear !== undefined) {
        runtime.globals.clear();
      }
      append("meta", "Interpreter reset — every name is gone.");
    } catch (caught) {
      append("err", message(caught));
    }
  }

  /* ---- scripts ---- */

  function openSaved(script: PythonScript): void {
    setOpenScript(script);
    setCode(script.code);
  }

  function startNew(): void {
    setOpenScript(null);
    setCode(STARTER);
    if (editorRef.current !== null) {
      editorRef.current.focus();
    }
  }

  async function save(): Promise<void> {
    if (workspaceId === null) {
      return;
    }
    setSaveError(null);

    if (openScript !== null) {
      try {
        const updated = await updateScript(openScript.id, { code: code });
        setOpenScript(updated);
        setScripts(await listScripts(workspaceId));
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
      setSaveError("Give the script a name.");
      return;
    }
    try {
      const created = await createScript({
        workspace: workspaceId,
        name: name,
        code: code,
      });
      setOpenScript(created);
      setScripts(await listScripts(workspaceId));
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

  async function removeScript(script: PythonScript): Promise<void> {
    if (workspaceId === null) {
      return;
    }
    try {
      await deleteScript(script.id);
      if (openScript !== null && openScript.id === script.id) {
        setOpenScript(null);
      }
      setScripts(await listScripts(workspaceId));
    } catch (caught) {
      setSaveError(message(caught));
    }
  }

  /* ---- gates ---- */

  if (workspaces !== null && workspaces.length === 0) {
    return (
      <div className="ide ide-gate">
        <div className="ide-gate-card">
          <FileCode2 size={22} strokeWidth={1.5} aria-hidden="true" />
          <h1>The console needs a workspace</h1>
          <p>
            Scripts are saved inside one.{" "}
            <Link href="/dashboard/workspaces?new">Create a workspace</Link> and
            come back.
          </p>
        </div>
      </div>
    );
  }

  /* ---- render ---- */

  return (
    <div className="ide">
      <header className="ide-bar">
        <div className="ide-bar-left">
          <label className="dash-visually-hidden" htmlFor="py-workspace">
            Workspace
          </label>
          <select
            id="py-workspace"
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

          <label className="dash-visually-hidden" htmlFor="py-source">
            Data source
          </label>
          <select
            id="py-source"
            className="ide-select"
            value={sourceId === null ? "" : String(sourceId)}
            disabled={sources.length === 0}
            title="Where honeycomb.query() runs"
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

          {currentSource !== undefined && currentSource.database ? (
            <span className="ide-dbname" title="honeycomb.query() runs here">
              {currentSource.database}
            </span>
          ) : null}

          <span className="ide-bar-sep" aria-hidden="true" />

          <span
            className={ready ? "ide-pill ide-pill-on" : "ide-pill"}
            title={"Pyodide " + PYODIDE_VERSION}
          >
            {booting ? (
              <Loader2 size={12} className="ide-spin" aria-hidden="true" />
            ) : null}
            {ready
              ? "Python " + (pythonVersion || "")
              : booting
              ? "Starting…"
              : "Not started"}
          </span>
        </div>

        <div className="ide-bar-right">
          <button
            type="button"
            className="ide-button"
            onClick={function clear() {
              setLines([]);
            }}
            disabled={lines.length === 0}
            title="Clear the output"
          >
            <Eraser size={15} strokeWidth={1.75} aria-hidden="true" />
            Clear
          </button>

          <button
            type="button"
            className="ide-button"
            onClick={function reset() {
              void resetInterpreter();
            }}
            disabled={!ready || running}
            title="Forget every variable and start the interpreter fresh"
          >
            <RotateCcw size={15} strokeWidth={1.75} aria-hidden="true" />
            Reset
          </button>

          <button
            type="button"
            className="ide-button"
            onClick={function onSave() {
              void save();
            }}
            disabled={code.trim() === "" || workspaceId === null}
            title={openScript === null ? "Save as a new script" : "Save over " + openScript.name}
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
            disabled={running || code.trim() === ""}
            title={ready ? "Run" : "First run downloads the Python runtime (~10MB)"}
          >
            {running ? (
              <Loader2 size={15} className="ide-spin" aria-hidden="true" />
            ) : (
              <Play size={15} strokeWidth={2} aria-hidden="true" />
            )}
            {running ? (booting ? "Starting" : "Running") : "Run"}
            <kbd className="ide-kbd">⌘↵</kbd>
          </button>
        </div>
      </header>

      {saveOpen && openScript === null ? (
        <div className="ide-savebar">
          <input
            className="ide-saveinput"
            value={saveName}
            autoFocus
            placeholder="Name this script"
            aria-label="Name this script"
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
          <div className="ide-side-tabs">
            <span className="ide-tab ide-tab-on">Scripts</span>
          </div>
          <div className="ide-saved">
            <button type="button" className="ide-newquery" onClick={startNew}>
              <Plus size={13} strokeWidth={2} aria-hidden="true" />
              New script
            </button>
            {scripts.length === 0 ? (
              <p className="ide-schema-note">
                Nothing saved yet. Write something and press Save.
              </p>
            ) : (
              <ul className="ide-saved-list">
                {scripts.map(function renderScript(script) {
                  const open = openScript !== null && openScript.id === script.id;
                  return (
                    <li key={script.id} className={open ? "ide-saved-on" : undefined}>
                      <button
                        type="button"
                        className="ide-saved-item"
                        onClick={function pick() {
                          openSaved(script);
                        }}
                      >
                        <FileCode2 size={13} strokeWidth={1.75} aria-hidden="true" />
                        <span className="ide-saved-name">{script.name}</span>
                      </button>
                      <button
                        type="button"
                        className="ide-icon-button"
                        aria-label={"Delete " + script.name}
                        title="Delete"
                        onClick={function drop() {
                          void removeScript(script);
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
        </div>

        <div className="ide-main">
          <p className="ide-warn ide-warn-quiet">
            <Info size={14} strokeWidth={1.75} aria-hidden="true" />
            {/* One <span>, not loose inline elements: .ide-warn is a flex row,
                so every direct child becomes a flex item and a sentence
                containing <code> and a link gets spread across the bar with
                gaps between the words. */}
            <span>
              Your code runs in this tab, so it holds no database password. To
              read data, have the server run the query for you:{" "}
              <code>await honeycomb.query(&quot;select ...&quot;)</code> — the
              same read-only connection the{" "}
              <Link href="/dashboard/sql">SQL console</Link> uses.
            </span>
          </p>

          {attached !== null ? (
            <p className="ide-warn ide-warn-quiet">
              <Table2 size={14} strokeWidth={1.75} aria-hidden="true" />
              <span>
                {attached.row_count} row{attached.row_count === 1 ? "" : "s"} from{" "}
                <code>{attached.preview}</code> are already in{" "}
                <code>honeycomb.rows</code>.
              </span>
              <button
                type="button"
                className="ide-openfile-close"
                aria-label="Detach these rows"
                title="Detach"
                onClick={function detach() {
                  setAttached(null);
                }}
              >
                <X size={12} strokeWidth={2} aria-hidden="true" />
              </button>
            </p>
          ) : null}

          {loadError !== null ? (
            <p className="ide-warn">
              <AlertTriangle size={14} strokeWidth={1.75} aria-hidden="true" />
              {loadError}
            </p>
          ) : null}

          <SplitPane
            initialTop={340}
            minTop={140}
            minBottom={140}
            label="Resize the editor"
            top={
              <div className="ide-editor-frame">
                {openScript !== null ? (
                  <div className="ide-openfile">
                    <FileCode2 size={12} strokeWidth={1.75} aria-hidden="true" />
                    {openScript.name}
                    <button
                      type="button"
                      className="ide-openfile-close"
                      aria-label="Close this script"
                      onClick={startNew}
                    >
                      <X size={12} strokeWidth={2} aria-hidden="true" />
                    </button>
                  </div>
                ) : null}
                <CodeEditor
                  ref={editorRef}
                  value={code}
                  onChange={setCode}
                  language="python"
                  onRun={execute}
                  ariaLabel="Python editor"
                  placeholder={"for row in range(3):\n    print(row)"}
                />
              </div>
            }
            bottom={
              <div className="ide-output">
                <div className="ide-output-tabs">
                  <span className="ide-tab ide-tab-on">Output</span>
                  {running ? (
                    <span className="ide-status">
                      <Loader2 size={12} className="ide-spin" aria-hidden="true" />
                      {booting ? "downloading the runtime" : "running"}
                    </span>
                  ) : null}
                </div>
                <div className="ide-output-body ide-console" ref={outputRef}>
                  {lines.length === 0 ? (
                    <div className="ide-blank">
                      <Play size={20} strokeWidth={1.5} aria-hidden="true" />
                      <p>
                        Press Run to execute the script, or select part of it to run
                        just that. The first run downloads the runtime.
                      </p>
                    </div>
                  ) : (
                    <pre className="ide-console-body">
                      {lines.map(function renderLine(line) {
                        return (
                          <span key={line.id} className={"ide-line ide-line-" + line.stream}>
                            {line.text}
                          </span>
                        );
                      })}
                    </pre>
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
