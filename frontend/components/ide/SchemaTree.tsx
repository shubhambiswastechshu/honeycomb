"use client";

/**
 * The left panel: what the connected role can actually see.
 *
 * "Actually see" is the point. The list comes from information_schema over the
 * source's own connection, so it is already filtered to the tables that role
 * has privileges on -- a table nobody can read is not offered, and someone
 * writing a query is never sent after a name that will fail.
 *
 * Tables start collapsed. A warehouse has hundreds, and an expanded tree is a
 * scrollbar rather than an index.
 */

import { useMemo, useState } from "react";
import { ChevronRight, Columns3, Eye, RefreshCw, Search, Table2 } from "lucide-react";

export interface SchemaColumn {
  name: string;
  type: string;
}

export interface SchemaTable {
  schema: string;
  name: string;
  qualified: string;
  kind: string;
  columns: SchemaColumn[];
}

export interface SchemaTreeProps {
  tables: SchemaTable[] | null;
  loading: boolean;
  error: string | null;
  /**
   * The database these tables live in.
   *
   * Shown because a tree of bare table names does not say where it came from,
   * and on MySQL that gap sends people to `USE <something>` -- which cannot
   * work here (see the hint in the SQL page) and fails with an access error
   * that reads as a permissions problem rather than a misunderstanding.
   */
  database?: string;
  /** Puts a name where the cursor is. */
  onPick: (text: string) => void;
  onRefresh: () => void;
}

export default function SchemaTree({
  tables,
  loading,
  error,
  database,
  onPick,
  onRefresh,
}: SchemaTreeProps) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [filter, setFilter] = useState("");

  const shown = useMemo(
    function applyFilter() {
      if (tables === null) {
        return [];
      }
      const needle = filter.trim().toLowerCase();
      if (needle === "") {
        return tables;
      }
      // Matching columns too, because half of what anyone searches a schema
      // for is "which table has customer_id in it".
      return tables.filter(function match(table) {
        if (table.qualified.toLowerCase().indexOf(needle) !== -1) {
          return true;
        }
        return table.columns.some(function column(entry) {
          return entry.name.toLowerCase().indexOf(needle) !== -1;
        });
      });
    },
    [tables, filter]
  );

  function toggle(key: string): void {
    setOpen(function update(current) {
      return { ...current, [key]: !current[key] };
    });
  }

  return (
    <aside className="ide-schema" aria-label="Schema">
      <div className="ide-schema-head">
        <span className="ide-schema-title">
          {database ? database : "Schema"}
        </span>
        <button
          type="button"
          className="ide-icon-button"
          onClick={onRefresh}
          disabled={loading}
          title="Reload the schema"
          aria-label="Reload the schema"
        >
          <RefreshCw
            size={13}
            strokeWidth={1.75}
            className={loading ? "ide-spin" : undefined}
            aria-hidden="true"
          />
        </button>
      </div>

      {tables !== null && tables.length > 0 ? (
        <div className="ide-schema-search">
          <Search size={13} strokeWidth={1.75} aria-hidden="true" />
          <input
            type="search"
            value={filter}
            placeholder="Filter tables and columns"
            aria-label="Filter tables and columns"
            onChange={function change(event) {
              setFilter(event.target.value);
            }}
          />
        </div>
      ) : null}

      <div className="ide-schema-body">
        {error !== null ? <p className="ide-schema-note ide-schema-bad">{error}</p> : null}

        {error === null && loading && tables === null ? (
          <p className="ide-schema-note">Reading the schema…</p>
        ) : null}

        {error === null && !loading && tables === null ? (
          <p className="ide-schema-note">Pick a source to see its tables.</p>
        ) : null}

        {tables !== null && tables.length === 0 ? (
          <p className="ide-schema-note">
            This role can read no tables. That is a grant on the database, not a
            problem here.
          </p>
        ) : null}

        {shown.length === 0 && tables !== null && tables.length > 0 ? (
          <p className="ide-schema-note">Nothing matches “{filter}”.</p>
        ) : null}

        <ul className="ide-tree">
          {shown.map(function renderTable(table) {
            const key = table.schema + "." + table.name;
            const expanded = open[key] === true;
            const Glyph = table.kind === "view" ? Eye : Table2;
            return (
              <li key={key}>
                <div className="ide-tree-row">
                  <button
                    type="button"
                    className="ide-tree-toggle"
                    onClick={function onToggle() {
                      toggle(key);
                    }}
                    aria-expanded={expanded}
                    aria-label={
                      (expanded ? "Collapse " : "Expand ") + table.qualified
                    }
                  >
                    <ChevronRight
                      size={13}
                      strokeWidth={2}
                      className={expanded ? "ide-tree-caret ide-open" : "ide-tree-caret"}
                      aria-hidden="true"
                    />
                  </button>
                  {/* Two controls, not one: the caret opens the table and the
                      name inserts it. Making the whole row do both means one
                      of the two happens by accident every time. */}
                  <button
                    type="button"
                    className="ide-tree-name"
                    onClick={function pick() {
                      onPick(table.qualified);
                    }}
                    title={"Insert " + table.qualified}
                  >
                    <Glyph size={13} strokeWidth={1.75} aria-hidden="true" />
                    <span>{table.name}</span>
                    <span className="ide-tree-count">{table.columns.length}</span>
                  </button>
                </div>

                {expanded ? (
                  <ul className="ide-tree-columns">
                    {table.columns.map(function renderColumn(column) {
                      return (
                        <li key={column.name}>
                          <button
                            type="button"
                            className="ide-tree-column"
                            onClick={function pick() {
                              onPick(column.name);
                            }}
                            title={"Insert " + column.name}
                          >
                            <Columns3 size={12} strokeWidth={1.75} aria-hidden="true" />
                            <span className="ide-tree-column-name">{column.name}</span>
                            <span className="ide-tree-column-type">{column.type}</span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                ) : null}
              </li>
            );
          })}
        </ul>
      </div>
    </aside>
  );
}
