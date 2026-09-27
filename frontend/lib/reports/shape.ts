/**
 * Turning an arbitrary provider payload into something a person can read.
 *
 * Extracted from components/dashboard/LiveData.tsx (the connector detail
 * page's "run a tool and see what it says" panel), which needed exactly this
 * -- find the scalars, find the one array of records worth a table -- before
 * the report builder existed. One copy, because a second one drifting from
 * the first would mean the live-data preview and a report widget disagreeing
 * about what the same tool result looks like.
 *
 * Deliberately shallow: one pass over the top level, and one level into it for
 * an array. A recursive walk finds more, but it also finds arrays buried in
 * metadata and puts them on screen as though they were the answer.
 */

export type Json = unknown;

export function isRecord(value: Json): value is Record<string, Json> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isScalar(value: Json): boolean {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

/** Human label for a snake_case key, without inventing words. */
export function label(key: string): string {
  const spaced = key.replace(/_/g, " ").trim();
  return spaced.length === 0 ? key : spaced[0].toUpperCase() + spaced.slice(1);
}

/** A scalar as short display text. Not for a table cell that needs the raw number. */
export function cell(value: Json): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  if (typeof value === "number") {
    // Provider ids are numbers too, and grouping them reads as a quantity that
    // it is not. Only group values small enough to plausibly be a count.
    return Number.isInteger(value) && Math.abs(value) < 1e15
      ? value.toLocaleString("en-GB")
      : String(value);
  }
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.length + " item" + (value.length === 1 ? "" : "s");
  }
  return "{…}";
}

/** Rows shown before a caller truncates further. Callers may cap tighter than this. */
export const MAX_SHAPE_ROWS = 500;

export interface Shaped {
  /**
   * Scalar leaves of the top-level object, e.g. for a stat row. `raw` is the
   * value before `cell()` formatted it into display text -- a caller that
   * needs to know whether a stat is actually a NUMBER (a metric) rather than
   * a numeric-looking string (an id) must check this, not parse `value` back.
   */
  stats: Array<{ key: string; value: string; raw: Json }>;
  /** The first array-of-records found. */
  rows: Array<Record<string, Json>> | null;
  /** Where that array was found (the object key), so a table can be labelled honestly. */
  rowsKey: string | null;
  /** Total rows before truncation. */
  rowsTotal: number;
}

export function shape(data: Json, maxRows: number = MAX_SHAPE_ROWS): Shaped {
  const empty: Shaped = { stats: [], rows: null, rowsKey: null, rowsTotal: 0 };

  if (Array.isArray(data)) {
    const records = data.filter(isRecord);
    if (records.length === data.length && records.length > 0) {
      return { stats: [], rows: records.slice(0, maxRows), rowsKey: null, rowsTotal: data.length };
    }
    return empty;
  }

  if (!isRecord(data)) {
    return empty;
  }

  const stats: Array<{ key: string; value: string; raw: Json }> = [];
  let rows: Array<Record<string, Json>> | null = null;
  let rowsKey: string | null = null;
  let rowsTotal = 0;

  for (const key of Object.keys(data)) {
    const value = data[key];
    if (isScalar(value)) {
      stats.push({ key: key, value: cell(value), raw: value });
      continue;
    }
    if (rows === null && Array.isArray(value)) {
      const records = value.filter(isRecord);
      if (records.length > 0 && records.length === value.length) {
        rows = records.slice(0, maxRows);
        rowsKey = key;
        rowsTotal = value.length;
      }
    }
  }

  return { stats: stats, rows: rows, rowsKey: rowsKey, rowsTotal: rowsTotal };
}

/** Union of keys across the rows, in first-seen order, capped at `limit`. */
export function columnsOf(rows: Array<Record<string, Json>>, limit: number = 12): string[] {
  const seen: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (seen.indexOf(key) === -1) {
        seen.push(key);
      }
    }
  }
  return seen.slice(0, limit);
}

/** True when every value present in this column across `rows` is a number. */
export function isNumericColumn(rows: Array<Record<string, Json>>, key: string): boolean {
  let seen = 0;
  for (const row of rows) {
    const value = row[key];
    if (value === null || value === undefined) {
      continue;
    }
    seen += 1;
    if (typeof value !== "number") {
      return false;
    }
  }
  return seen > 0;
}

/** True when every value present in this column across `rows` parses as an ISO day or datetime. */
export function isDateColumn(rows: Array<Record<string, Json>>, key: string): boolean {
  const ISO = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?/;
  let seen = 0;
  for (const row of rows) {
    const value = row[key];
    if (value === null || value === undefined) {
      continue;
    }
    seen += 1;
    if (typeof value !== "string" || !ISO.test(value)) {
      return false;
    }
  }
  return seen > 0;
}

export type FieldType = "number" | "date" | "category";

/** A best guess at each column's type, for a widget choosing sensible defaults. */
export function inferFieldTypes(rows: Array<Record<string, Json>>): Record<string, FieldType> {
  const out: Record<string, FieldType> = {};
  for (const key of columnsOf(rows, 64)) {
    if (isNumericColumn(rows, key)) {
      out[key] = "number";
    } else if (isDateColumn(rows, key)) {
      out[key] = "date";
    } else {
      out[key] = "category";
    }
  }
  return out;
}
