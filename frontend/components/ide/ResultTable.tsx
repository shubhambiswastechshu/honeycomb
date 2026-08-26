"use client";

/**
 * The results grid.
 *
 * A grid is where a query tool is believed or not, so the rules here are about
 * telling the truth about a value rather than about looking tidy:
 *
 *   null      renders as a dimmed NULL, never as an empty cell. "No value" and
 *             "empty string" are different answers and a blank cell says both.
 *   numbers   right-align with tabular figures so magnitudes line up. Numerics
 *             arrive as strings from the server, on purpose -- see the
 *             connector -- so alignment is decided by the column's declared
 *             type, not by typeof.
 *   long text is clipped with the full value on hover and in the copy, because
 *             one 40KB JSON cell must not set the height of every row.
 */

import { useMemo, useState } from "react";
import { Check, Copy, Download } from "lucide-react";

export interface ResultColumn {
  name: string;
  type: string;
}

export type ResultValue = string | number | boolean | null | unknown;

export interface ResultTableProps {
  columns: ResultColumn[];
  rows: ResultValue[][];
  /** Used to name the downloaded file; falls back to "result". */
  name?: string;
}

/* Type names that should sit on the right, across both engines.

   A prefix list alone does not work: PostgreSQL says "int4" and "numeric",
   MySQL says "bigint", "smallint", "tinyint" and "mediumint" -- the same
   family with the size on the front rather than the back. So integers are
   matched by containment, and the two words that contain "int" without being
   numbers are excluded by name.

     interval   a duration; left-aligning it keeps it with the other
                textual values it reads like.
     point      a geometry pair, which is text-shaped in a grid. */
const NUMERIC_PREFIXES = ["float", "numeric", "decimal", "serial", "money", "double", "real"];
const NOT_NUMERIC = ["interval", "point"];

function isNumericType(type: string): boolean {
  const name = (type || "").toLowerCase();
  for (let index = 0; index < NOT_NUMERIC.length; index += 1) {
    if (name.indexOf(NOT_NUMERIC[index]) !== -1) {
      return false;
    }
  }
  if (name.indexOf("int") !== -1) {
    return true;
  }
  for (let index = 0; index < NUMERIC_PREFIXES.length; index += 1) {
    if (name.indexOf(NUMERIC_PREFIXES[index]) === 0) {
      return true;
    }
  }
  return false;
}

/** The text of a cell, for display, for the clipboard and for CSV alike. */
function display(value: ResultValue): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "number") {
    return String(value);
  }
  return JSON.stringify(value);
}

/** RFC 4180: quote anything containing a comma, a quote or a newline. */
function csvCell(value: ResultValue): string {
  const text = display(value);
  if (/[",\r\n]/.test(text)) {
    return '"' + text.replace(/"/g, '""') + '"';
  }
  return text;
}

function toCsv(columns: ResultColumn[], rows: ResultValue[][]): string {
  const lines = [columns.map(function head(column) { return csvCell(column.name); }).join(",")];
  for (let index = 0; index < rows.length; index += 1) {
    lines.push(rows[index].map(csvCell).join(","));
  }
  return lines.join("\r\n");
}

export default function ResultTable({ columns, rows, name }: ResultTableProps) {
  const [copied, setCopied] = useState(false);

  const alignment = useMemo(
    function decideAlignment() {
      return columns.map(function align(column) {
        return isNumericType(column.type);
      });
    },
    [columns]
  );

  function download(): void {
    const csv = toCsv(columns, rows);
    // A BOM so Excel opens UTF-8 as UTF-8 rather than as the local codepage,
    // which is what turns a customer's name into mojibake in the one place
    // they are most likely to look at it.
    const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = (name || "result").replace(/[^\w.-]+/g, "-") + ".csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  async function copy(): Promise<void> {
    // Tab separated, which is what spreadsheets accept on paste.
    const text = [columns.map(function head(column) { return column.name; }).join("\t")]
      .concat(
        rows.map(function line(row) {
          return row.map(display).join("\t");
        })
      )
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(function reset() {
        setCopied(false);
      }, 1600);
    } catch (caught) {
      // Clipboard access can be refused outright (an insecure origin, a
      // permissions policy). Failing quietly is right: the download button
      // beside it does the same job and nothing is broken.
    }
  }

  if (columns.length === 0) {
    return null;
  }

  return (
    <div className="ide-result">
      <div className="ide-result-tools">
        <button type="button" className="ide-chip" onClick={copy} title="Copy as TSV">
          {copied ? (
            <Check size={13} strokeWidth={2} aria-hidden="true" />
          ) : (
            <Copy size={13} strokeWidth={1.75} aria-hidden="true" />
          )}
          {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" className="ide-chip" onClick={download} title="Download as CSV">
          <Download size={13} strokeWidth={1.75} aria-hidden="true" />
          CSV
        </button>
      </div>

      <div className="ide-grid-scroll">
        <table className="ide-grid">
          <thead>
            <tr>
              <th className="ide-grid-num" scope="col">
                <span className="dash-visually-hidden">Row</span>
              </th>
              {columns.map(function renderHead(column, index) {
                return (
                  <th
                    key={column.name + "-" + index}
                    scope="col"
                    className={alignment[index] ? "ide-grid-right" : undefined}
                  >
                    <span className="ide-grid-name">{column.name}</span>
                    <span className="ide-grid-type">{column.type}</span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map(function renderRow(row, rowIndex) {
              return (
                <tr key={rowIndex}>
                  <td className="ide-grid-num">{rowIndex + 1}</td>
                  {columns.map(function renderCell(column, cellIndex) {
                    const value = row[cellIndex];
                    const empty = value === null || value === undefined;
                    const text = display(value);
                    return (
                      <td
                        key={column.name + "-" + cellIndex}
                        className={alignment[cellIndex] ? "ide-grid-right" : undefined}
                        title={empty ? undefined : text}
                      >
                        {empty ? <span className="ide-grid-null">NULL</span> : text}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
