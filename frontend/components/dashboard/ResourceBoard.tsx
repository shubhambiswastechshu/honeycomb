"use client";

/**
 * The list-plus-create surface every product page uses.
 *
 * Sources, saved queries and pipelines differ only in their fields and how a
 * row reads, so they share this instead of three near-identical pages that
 * drift apart the first time one of them gets a fix.
 *
 * Two field kinds are resolved here rather than by the caller:
 *
 *   "workspace"  the tenant's workspaces, fetched once.
 *   "source"     the data sources *inside the chosen workspace*, refetched
 *                whenever that choice changes. Offering every source in the
 *                organization would let someone pick one the server then
 *                rejects, which is a worse way to learn the rule than not
 *                being offered it.
 *
 * When a page needs a workspace and the organization has none, the form is
 * replaced by a pointer to where workspaces are made. An empty required select
 * is a dead end -- the visitor cannot tell whether the page is broken or
 * whether they are missing a step.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import Link from "next/link";
import { Loader2, Plus, Trash2, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import EmptyState from "@/components/dashboard/EmptyState";
import { listDataSources, listWorkspaces } from "@/lib/api";
import type { DataSource, Workspace } from "@/lib/api";

export type FieldKind = "text" | "textarea" | "select" | "workspace" | "source";

export interface SelectOption {
  value: string;
  label: string;
}

export interface FieldSpec {
  name: string;
  label: string;
  kind: FieldKind;
  placeholder?: string;
  required?: boolean;
  help?: string;
  /** Only for kind "select"; the other kinds build their own options. */
  options?: SelectOption[];
}

export interface ResourceBoardProps<T> {
  icon: LucideIcon;
  title: string;
  lede: string;
  addLabel: string;
  emptyTitle: string;
  emptyText: string;
  fields: FieldSpec[];
  load: () => Promise<T[]>;
  create: (values: Record<string, unknown>) => Promise<T>;
  remove: (item: T) => Promise<void>;
  rowKey: (item: T) => number;
  renderRow: (item: T) => ReactNode;
  /**
   * Extra controls beside Delete on each row.
   *
   * Takes `refresh` rather than expecting the caller to hold its own copy of
   * the list: an action that changes a row -- testing a connection writes its
   * status -- has to be able to say "re-read", and the board is the thing that
   * owns the list.
   */
  rowActions?: (item: T, refresh: () => Promise<void>) => ReactNode;
}

const GENERIC = "Something went wrong. Please try again.";

function message(caught: unknown): string {
  return caught instanceof Error ? caught.message : GENERIC;
}

export default function ResourceBoard<T>({
  icon: Icon,
  title,
  lede,
  addLabel,
  emptyTitle,
  emptyText,
  fields,
  load,
  create,
  remove,
  rowKey,
  renderRow,
  rowActions,
}: ResourceBoardProps<T>) {
  const [items, setItems] = useState<T[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Opened by ?new in the URL, so a tile labelled "New workspace" lands on the
  // form rather than on a list the visitor then has to find a button in.
  // Read from window rather than useSearchParams: the hook forces the page
  // into a Suspense boundary for static rendering, and one query parameter is
  // not worth that.
  const [open, setOpen] = useState(false);

  useEffect(function openFromUrl() {
    if (typeof window === "undefined") {
      return;
    }
    if (new URLSearchParams(window.location.search).has("new")) {
      setOpen(true);
    }
  }, []);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  const [sources, setSources] = useState<DataSource[]>([]);

  const needsWorkspace = useMemo(
    function scan() {
      return fields.some(function isWorkspace(field) {
        return field.kind === "workspace";
      });
    },
    [fields]
  );
  const needsSource = useMemo(
    function scan() {
      return fields.some(function isSource(field) {
        return field.kind === "source";
      });
    },
    [fields]
  );

  const refresh = useCallback(
    async function refresh(): Promise<void> {
      try {
        setItems(await load());
        setError(null);
      } catch (caught) {
        setError(message(caught));
      }
    },
    [load]
  );

  useEffect(
    function loadOnMount() {
      void refresh();
    },
    [refresh]
  );

  useEffect(
    function loadWorkspaces() {
      if (!needsWorkspace) {
        return;
      }
      let cancelled = false;
      listWorkspaces()
        .then(function keep(list) {
          if (!cancelled) {
            setWorkspaces(list);
          }
        })
        .catch(function ignore() {
          if (!cancelled) {
            setWorkspaces([]);
          }
        });
      return function cleanup() {
        cancelled = true;
      };
    },
    [needsWorkspace]
  );

  const chosenWorkspace = values.workspace;

  useEffect(
    function loadSources() {
      if (!needsSource || !chosenWorkspace) {
        setSources([]);
        return;
      }
      let cancelled = false;
      listDataSources(Number(chosenWorkspace))
        .then(function keep(list) {
          if (!cancelled) {
            setSources(list);
          }
        })
        .catch(function ignore() {
          if (!cancelled) {
            setSources([]);
          }
        });
      return function cleanup() {
        cancelled = true;
      };
    },
    [needsSource, chosenWorkspace]
  );

  function optionsFor(field: FieldSpec): SelectOption[] {
    if (field.kind === "workspace") {
      return (workspaces || []).map(function toOption(workspace) {
        return { value: String(workspace.id), label: workspace.name };
      });
    }
    if (field.kind === "source") {
      return sources.map(function toOption(source) {
        return {
          value: String(source.id),
          label: source.name + " · " + source.kind_display,
        };
      });
    }
    return field.options || [];
  }

  function setValue(name: string, value: string): void {
    setValues(function update(current) {
      const next = { ...current, [name]: value };
      // A source only makes sense inside its workspace, so changing the
      // workspace drops a choice that is about to become invalid rather than
      // letting it be submitted and bounced.
      if (name === "workspace") {
        delete next.source;
        delete next.data_source;
      }
      return next;
    });
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setSaving(true);
    setFormError(null);

    // Empty optional fields are dropped rather than sent as "". A blank string
    // is a value; the server would take it as "set this to empty" instead of
    // "leave it alone", and for a foreign key it is not even a valid one.
    const body: Record<string, unknown> = {};
    for (const field of fields) {
      const raw = (values[field.name] || "").trim();
      if (raw === "") {
        continue;
      }
      const isReference = field.kind === "workspace" || field.kind === "source";
      body[field.name] = isReference ? Number(raw) : raw;
    }

    try {
      await create(body);
      setValues({});
      setOpen(false);
      await refresh();
    } catch (caught) {
      setFormError(message(caught));
    } finally {
      setSaving(false);
    }
  }

  async function drop(item: T): Promise<void> {
    try {
      await remove(item);
      await refresh();
    } catch (caught) {
      setError(message(caught));
    }
  }

  const blockedOnWorkspace =
    needsWorkspace && workspaces !== null && workspaces.length === 0;

  return (
    <div className="panel">
      <div className="board-head">
        <div>
          <h1 className="panel-title">{title}</h1>
          <p className="panel-lede">{lede}</p>
        </div>
        {!blockedOnWorkspace ? (
          <button
            type="button"
            className="board-add"
            onClick={function toggle() {
              setOpen(!open);
              setFormError(null);
            }}
            aria-expanded={open}
          >
            {/* The glyph has to agree with the word: a plus beside "Cancel"
                says the button still adds something. */}
            {open ? (
              <X size={16} strokeWidth={2} aria-hidden="true" />
            ) : (
              <Plus size={16} strokeWidth={2} aria-hidden="true" />
            )}
            {open ? "Cancel" : addLabel}
          </button>
        ) : null}
      </div>

      <div className="panel-body">
        {error !== null ? <p className="board-error">{error}</p> : null}

        {blockedOnWorkspace ? (
          <p className="board-note">
            This needs a workspace first.{" "}
            <Link href="/dashboard/workspaces">Create one</Link> and come back.
          </p>
        ) : null}

        {open && !blockedOnWorkspace ? (
          <form className="board-form" onSubmit={submit}>
            {fields.map(function renderField(field) {
              const id = "field-" + field.name;
              const isSelect =
                field.kind === "select" ||
                field.kind === "workspace" ||
                field.kind === "source";
              return (
                <div className="board-field" key={field.name}>
                  <label className="label" htmlFor={id}>
                    {field.label}
                  </label>

                  {isSelect ? (
                    <select
                      id={id}
                      className="input"
                      value={values[field.name] || ""}
                      required={field.required}
                      disabled={saving}
                      onChange={function change(event) {
                        setValue(field.name, event.target.value);
                      }}
                    >
                      <option value="">
                        {field.placeholder || "Choose one"}
                      </option>
                      {optionsFor(field).map(function renderOption(option) {
                        return (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        );
                      })}
                    </select>
                  ) : field.kind === "textarea" ? (
                    <textarea
                      id={id}
                      className="input board-textarea"
                      value={values[field.name] || ""}
                      placeholder={field.placeholder}
                      required={field.required}
                      disabled={saving}
                      rows={6}
                      onChange={function change(event) {
                        setValue(field.name, event.target.value);
                      }}
                    />
                  ) : (
                    <input
                      id={id}
                      className="input"
                      type="text"
                      value={values[field.name] || ""}
                      placeholder={field.placeholder}
                      required={field.required}
                      disabled={saving}
                      onChange={function change(event) {
                        setValue(field.name, event.target.value);
                      }}
                    />
                  )}

                  {field.help !== undefined ? (
                    <p className="board-help">{field.help}</p>
                  ) : null}
                </div>
              );
            })}

            {formError !== null ? (
              <p className="board-error">{formError}</p>
            ) : null}

            <button className="button board-submit" type="submit" disabled={saving}>
              {saving ? (
                <>
                  <Loader2
                    className="board-spinner"
                    size={16}
                    aria-hidden="true"
                  />
                  Saving...
                </>
              ) : (
                addLabel
              )}
            </button>
          </form>
        ) : null}

        {items === null ? null : items.length === 0 ? (
          <EmptyState icon={Icon} title={emptyTitle} description={emptyText} />
        ) : (
          <ul className="board-list">
            {items.map(function renderItem(item) {
              return (
                <li className="board-row" key={rowKey(item)}>
                  <div className="board-row-body">{renderRow(item)}</div>
                  {rowActions !== undefined ? rowActions(item, refresh) : null}
                  <button
                    type="button"
                    className="board-delete"
                    aria-label="Delete"
                    title="Delete"
                    onClick={function onDelete() {
                      void drop(item);
                    }}
                  >
                    <Trash2 size={15} strokeWidth={1.75} aria-hidden="true" />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
