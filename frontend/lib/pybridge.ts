/**
 * The bridge that lets browser-side Python read a database it cannot reach.
 *
 * The Python console runs on Pyodide inside the tab. It has no warehouse
 * credentials and no way to open a database socket, and both of those are the
 * point rather than a limitation to work around -- see lib/pyodide.ts.
 *
 * So the bridge does not give Python a connection. It gives Python a way to
 * *ask the server* to run a statement, which is the same request the SQL
 * console makes, through the same endpoint, as the same signed-in user. Every
 * guard that endpoint has still applies and none of them is duplicated here:
 * the tenant filter, the read-only role, the statement timeout, the row cap,
 * the rate limit. A loop calling `query()` a hundred times gets 429s from the
 * same throttle a person clicking Run would.
 *
 * What crosses the boundary is a JSON string, deliberately. Handing Pyodide a
 * JS object means a JsProxy on the Python side whose lifetime the caller has
 * to manage, and whose nested nulls and dates arrive as whatever the automatic
 * conversion decides. A string parsed by `json` on the far side has one
 * meaning.
 */

import { runQuery } from "@/lib/api";
import type { QueryRun } from "@/lib/api";

/** What the Python module is handed for a query, already flattened. */
export interface BridgePayload {
  ok: boolean;
  error: string | null;
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
  notice: string;
  duration_ms: number;
}

/** Rows as dicts keyed by column name -- what `pd.DataFrame(rows)` wants. */
export function toRecords(run: QueryRun): Record<string, unknown>[] {
  const names = run.result_columns.map(function name(column) {
    return column.name;
  });
  return run.result_rows.map(function toRecord(row) {
    const record: Record<string, unknown> = {};
    for (let index = 0; index < names.length; index += 1) {
      // Duplicate column names collapse -- `select id, id from t` gives one
      // key. That is what a dict means, and renaming them behind someone's
      // back would be worse than the collision.
      record[names[index]] = row[index];
    }
    return record;
  });
}

export function payloadFor(run: QueryRun): BridgePayload {
  const failed = run.state === "FAILED";
  return {
    ok: !failed,
    error: failed ? run.error : null,
    columns: run.result_columns.map(function name(column) {
      return column.name;
    }),
    rows: failed ? [] : toRecords(run),
    row_count: run.row_count,
    truncated: run.truncated,
    notice: run.notice,
    duration_ms: run.duration_ms,
  };
}

export function emptyPayload(error: string): BridgePayload {
  return {
    ok: false,
    error: error,
    columns: [],
    rows: [],
    row_count: 0,
    truncated: false,
    notice: "",
    duration_ms: 0,
  };
}

export interface BridgeContext {
  workspaceId: number | null;
  sourceId: number | null;
}

/**
 * Ask the server to run `sql` and return the answer as a JSON string.
 *
 * Never rejects. A rejected promise crossing into Python surfaces as a
 * JsException with a JavaScript stack in it, which tells a person writing
 * Python nothing they can act on. Every failure comes back as `ok: false` with
 * a message, and the Python side raises a normal exception from that.
 */
export async function runForPython(
  context: BridgeContext,
  sql: string,
  limit: number | null
): Promise<string> {
  if (context.workspaceId === null || context.sourceId === null) {
    return JSON.stringify(
      emptyPayload("Pick a workspace and a data source above before querying.")
    );
  }
  try {
    const run = await runQuery({
      workspace: context.workspaceId,
      data_source: context.sourceId,
      sql: sql,
      limit: limit === null ? undefined : limit,
    });
    return JSON.stringify(payloadFor(run));
  } catch (caught) {
    return JSON.stringify(
      emptyPayload(
        caught instanceof Error ? caught.message : "The query could not be sent."
      )
    );
  }
}

/**
 * The `honeycomb` module, defined in Python because that is what it is.
 *
 * Registered once per runtime. `query` is a coroutine: the round trip to the
 * server is real network time, and pretending otherwise would mean blocking
 * the only thread the tab has. `runPythonAsync` means `await` works at the top
 * level of a script, so the cost to the person writing it is one keyword.
 */
export const BOOTSTRAP = `
import json as _json, sys as _sys, types as _types
import _honeycomb_bridge as _bridge

_mod = _types.ModuleType("honeycomb")
_mod.__doc__ = "Read data through the Honeycomb server. See help(honeycomb.query)."

# Filled in before every run by _set_context, so they describe the pickers as
# they are now rather than as they were when the runtime started.
_mod.workspace = None
_mod.source = None
_mod.database = None
_mod.rows = []
_mod.columns = []


async def query(sql, limit=None):
    """Run SQL on the selected data source and return rows as a list of dicts.

    The statement runs on the server, over the source's own read-only
    connection -- not in this tab, which has no database credentials. Every
    limit the SQL console has applies here too: the row cap, the statement
    timeout and the rate limit.

        rows = await honeycomb.query("select * from accounts limit 10")
        import pandas as pd
        df = pd.DataFrame(rows)

    Raises RuntimeError with the database's own message when the statement is
    refused, so a typo reads like a database error rather than a network one.

    Which database it runs against is the source picked above, and it is
    shown beside that picker. Read it in code as honeycomb.database. A table
    that exists in one source and not another is the commonest reason for a
    "doesn't exist" error here.
    """
    payload = _json.loads(await _bridge.run_query(sql, limit))
    if not payload["ok"]:
        raise RuntimeError(payload["error"])
    _mod.columns = payload["columns"]
    _mod.rows = payload["rows"]
    if payload["truncated"]:
        print(payload["notice"])
    return payload["rows"]


def _set_context(raw):
    state = _json.loads(raw)
    _mod.workspace = state.get("workspace")
    _mod.source = state.get("source")
    _mod.database = state.get("database")
    if state.get("attached") is not None:
        _mod.rows = state["attached"]["rows"]
        _mod.columns = state["attached"]["columns"]


_mod.query = query
_mod._set_context = _set_context
_sys.modules["honeycomb"] = _mod
`;
