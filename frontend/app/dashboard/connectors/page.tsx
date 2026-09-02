"use client";

/**
 * The connector marketplace: the front door of the MCP portal.
 *
 * Reads like an app store, but every number on it is real. GET /api/connectors/
 * returns the catalogue with this tenant's connected_count already resolved, so
 * a card never has to guess and this page never has to fabricate.
 *
 * Three rules shape the whole file:
 *
 * 1. Loading shows nothing rather than a wrong number. A card reading
 *    "Connect" while the request is in flight is not a loading state, it is a
 *    false statement the user will act on -- they connect a tool they already
 *    had. So `connectors` stays null until the response lands, and the grid
 *    does not exist before then.
 *
 * 2. Searching and category filtering happen here, over the array already in
 *    memory. The catalogue is a few dozen rows that arrived in one request;
 *    round-tripping a keystroke to Django would add latency, a race between
 *    responses, and a throttle bucket, and would buy nothing.
 *
 * 3. Nothing is invented to fill space. No sample connectors, no "popular"
 *    ordering pulled out of the air, no install counts. When a search matches
 *    nothing, the page says so.
 *
 * Every class here is one dashboard.css already defines for this route
 * (.mkt-*, plus the shell's .panel / .empty / .error / .dash-visually-hidden).
 * The markup is shaped to that stylesheet rather than the other way round --
 * .mkt-search-icon must stay the input's immediately preceding sibling, and
 * .mkt-empty is a grid item, so it lives inside .mkt-grid.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent } from "react";
import Link from "next/link";
import { Blocks, Check, Plus, Search } from "lucide-react";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import EmptyState from "@/components/dashboard/EmptyState";
import PanelCover from "@/components/dashboard/PanelCover";
import LoadingScreen from "@/components/ui/LoadingScreen";
import { listConnectors } from "@/lib/api";
import type { ConnectorSpec } from "@/lib/api";

/** Chip value meaning "do not filter". Not a category, so it cannot collide. */
const ALL = "__all__";

/**
 * Shown for a connector whose category is blank. This is a label for the
 * absence of one, not invented data: an uncategorised connector still has to be
 * reachable from the chip strip, and quietly dropping it from every chip but
 * "All" would hide it from anyone browsing by category.
 */
const UNCATEGORIZED = "Other";

const LOAD_ERROR = "Could not load the connector catalogue.";

function categoryOf(connector: ConnectorSpec): string {
  const category = connector.category.trim();
  return category.length > 0 ? category : UNCATEGORIZED;
}

/**
 * Case-insensitive substring match over label and description.
 *
 * Deliberately dumb: no fuzzy matching, no ranking. Someone typing "cal" wants
 * every row containing "cal", and a scoring function that quietly demoted one
 * of them would be a worse answer dressed up as a better one.
 */
function matchesQuery(connector: ConnectorSpec, needle: string): boolean {
  if (needle.length === 0) {
    return true;
  }
  return (
    connector.label.toLowerCase().indexOf(needle) !== -1 ||
    connector.description.toLowerCase().indexOf(needle) !== -1
  );
}

function toolCountLabel(count: number): string {
  return count === 1 ? "1 tool" : count + " tools";
}

export default function ConnectorsPage() {
  // null is "not loaded yet"; [] is "the catalogue is empty". Two different
  // facts that render two different things, so they get two different values
  // rather than one array that starts out empty and lies for a frame.
  const [connectors, setConnectors] = useState<ConnectorSpec[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState<string>("");
  const [category, setCategory] = useState<string>(ALL);

  const searchRef = useRef<HTMLInputElement | null>(null);
  const aliveRef = useRef<boolean>(true);

  useEffect(function loadCatalogue() {
    aliveRef.current = true;

    listConnectors()
      .then(function received(rows: ConnectorSpec[]) {
        if (!aliveRef.current) {
          return;
        }
        setConnectors(rows);
        setError(null);
      })
      .catch(function failed(caught: unknown) {
        if (!aliveRef.current) {
          return;
        }
        // `connectors` deliberately stays null. A failed load must not fall
        // through to the "no connectors are registered" empty state, which
        // would tell the user something false about their deployment.
        setError(caught instanceof Error ? caught.message : LOAD_ERROR);
      });

    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  /**
   * The chip strip, built from the catalogue rather than hardcoded, so a
   * connector registered under a new category appears under it with no
   * frontend change -- the same reason ConnectorMark falls back rather than
   * failing on an unknown slug.
   */
  const categories = useMemo(
    function collectCategories(): string[] {
      if (connectors === null) {
        return [];
      }
      const seen: string[] = [];
      for (let index = 0; index < connectors.length; index += 1) {
        const name = categoryOf(connectors[index]);
        if (seen.indexOf(name) === -1) {
          seen.push(name);
        }
      }
      seen.sort();
      // "Other" is a fallback, not a peer, so it sinks to the end of the strip
      // however the alphabet happened to come out.
      const other = seen.indexOf(UNCATEGORIZED);
      if (other !== -1) {
        seen.splice(other, 1);
        seen.push(UNCATEGORIZED);
      }
      return seen;
    },
    [connectors]
  );

  const visible = useMemo(
    function filterConnectors(): ConnectorSpec[] {
      if (connectors === null) {
        return [];
      }
      const needle = query.trim().toLowerCase();
      return connectors.filter(function keep(connector: ConnectorSpec) {
        if (category !== ALL && categoryOf(connector) !== category) {
          return false;
        }
        return matchesQuery(connector, needle);
      });
    },
    [connectors, query, category]
  );

  /**
   * The visible rows as one shelf.
   *
   * Categories are a filter, not a layout. Dealing fifteen cards into six
   * category rows leaves a row of one, a row of two and a ragged hole down the
   * right-hand side -- which reads as a broken grid, not as a browsable store.
   * The chips above already answer "show me only Analytics". Shelves earn
   * their keep at a few hundred connectors; the structure is kept here, as a
   * list of one, so restoring them is a change to this function alone.
   */
  const shelves = useMemo(
    function groupConnectors(): Array<{ name: string; items: ConnectorSpec[] }> {
      return visible.length > 0 ? [{ name: "", items: visible }] : [];
    },
    [visible]
  );

  /** Totals for the header. Counted from the catalogue, never guessed. */
  const totals = useMemo(
    function countCatalogue() {
      if (connectors === null) {
        return null;
      }
      return {
        connectors: connectors.length,
        tools: connectors.reduce(function add(sum, c) {
          return sum + c.tool_count;
        }, 0),
        connected: connectors.filter(function isOn(c) {
          return c.connected_count > 0;
        }).length,
      };
    },
    [connectors]
  );

  function onSearchChange(event: ChangeEvent<HTMLInputElement>): void {
    setQuery(event.target.value);
  }

  function onClearFilters(): void {
    setQuery("");
    setCategory(ALL);
    // Focus returns to the field rather than being stranded on a button that
    // is about to be unmounted, so a keyboard user can keep typing.
    if (searchRef.current !== null) {
      searchRef.current.focus();
    }
  }

  return (
    <div className="panel panel-wide">
      <PanelCover
        title="MCPs"
        lede="Connect a tool once, then point any MCP client at it."
      >
        {/* Counted, never estimated, and absent until the catalogue has
            actually arrived -- a count of 0 mid-request is a wrong answer. */}
        {totals !== null ? (
          <span className="mkt-count">
            {totals.connectors} connectors &middot; {totals.tools} tools
            {totals.connected > 0
              ? " · " + totals.connected + " connected"
              : ""}
          </span>
        ) : null}
      </PanelCover>

      <div className="panel-body">
        {error !== null ? (
          <p className="error">{error}</p>
        ) : connectors === null ? (
          <LoadingScreen label="Loading connectors" />
        ) : connectors.length === 0 ? (
          <EmptyState
            icon={Blocks}
            title="No connectors available"
            description="Connectors registered on this deployment will appear here."
          />
        ) : (
          <>
            <div className="mkt-toolbar">
              <div className="mkt-search">
                <label className="dash-visually-hidden" htmlFor="mkt-search">
                  Search connectors
                </label>
                {/* Must stay immediately before the input: dashboard.css
                    indents the field with `.mkt-search-icon + input`. */}
                <Search
                  className="mkt-search-icon"
                  size={16}
                  strokeWidth={1.8}
                  aria-hidden="true"
                />
                <input
                  id="mkt-search"
                  ref={searchRef}
                  type="search"
                  autoComplete="off"
                  placeholder="Search connectors"
                  value={query}
                  onChange={onSearchChange}
                />
              </div>

              {/* One category is not a filter: a lone chip beside "All"
                  answers no question, so the strip only appears above one. */}
              {categories.length > 1 ? (
                <div
                  className="mkt-chips"
                  role="group"
                  aria-label="Filter by category"
                >
                  <button
                    type="button"
                    className="mkt-chip"
                    aria-pressed={category === ALL}
                    onClick={function selectAll() {
                      setCategory(ALL);
                    }}
                  >
                    All
                  </button>
                  {categories.map(function renderChip(name: string) {
                    return (
                      <button
                        key={name}
                        type="button"
                        className="mkt-chip"
                        aria-pressed={category === name}
                        onClick={function selectCategory() {
                          setCategory(name);
                        }}
                      >
                        {name}
                      </button>
                    );
                  })}
                </div>
              ) : null}

              {/* Polite, not assertive: this changes on every keystroke and an
                  assertive region would interrupt the user mid-word. */}
              <span className="mkt-count" role="status" aria-live="polite">
                {visible.length === connectors.length
                  ? "All " + connectors.length + " connectors"
                  : visible.length +
                    " of " +
                    connectors.length +
                    " connectors"}
              </span>
            </div>

            {shelves.length === 0 ? (
              <p className="mkt-empty">
                <span className="mkt-empty-text">
                  Nothing in the catalogue matches that.
                </span>
                <button
                  type="button"
                  className="mkt-chip"
                  onClick={onClearFilters}
                >
                  Clear filters
                </button>
              </p>
            ) : (
              shelves.map(function renderShelf(shelf) {
                return (
                  <section className="mkt-shelf" key={shelf.name || "results"}>
                    {shelf.name !== "" ? (
                      <h2 className="mkt-shelf-title">
                        {shelf.name}
                        <span className="mkt-shelf-count">
                          {shelf.items.length}
                        </span>
                      </h2>
                    ) : null}

                    <ul className="mkt-grid">
                      {shelf.items.map(function renderCard(
                        connector: ConnectorSpec
                      ) {
                        const connected = connector.connected_count > 0;
                        return (
                          <li key={connector.slug} className="mkt-cell">
                            {/* The whole card is the link, and the pills
                                inside it are spans, not controls: an <a>
                                inside an <a> is invalid HTML that browsers
                                repair in ways nobody agreed on. */}
                            <Link
                              className="mkt-card"
                              href={"/dashboard/connectors/" + connector.slug}
                            >
                              <span className="mkt-card-head">
                                <ConnectorMark
                                  slug={connector.slug}
                                  label={connector.label}
                                />
                                <span className="mkt-card-titles">
                                  <span className="mkt-label">
                                    {connector.label}
                                  </span>
                                  <span className="mkt-tools">
                                    {toolCountLabel(connector.tool_count)}
                                  </span>
                                </span>
                                {connected ? (
                                  <span
                                    className="mkt-state is-connected"
                                    title={
                                      connector.connected_count +
                                      " connected"
                                    }
                                  >
                                    <Check
                                      size={12}
                                      strokeWidth={2.6}
                                      aria-hidden="true"
                                    />
                                    {connector.connected_count}
                                  </span>
                                ) : null}
                              </span>

                              <span className="mkt-desc">
                                {connector.description}
                              </span>

                              <span className="mkt-meta">
                                <span className="mkt-cta">
                                  <Plus
                                    size={13}
                                    strokeWidth={2.4}
                                    aria-hidden="true"
                                  />
                                  {connected ? "Add another" : "Connect"}
                                </span>
                              </span>
                            </Link>
                          </li>
                        );
                      })}
                    </ul>
                  </section>
                );
              })
            )}
          </>
        )}
      </div>
    </div>
  );
}
