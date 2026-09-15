"use client";

/**
 * The top bar's search: one field that jumps to a page, one of your
 * connections, or any connector in the catalogue.
 *
 * It searches what a person actually navigates between in this product, and
 * nothing else. There is no server-side search endpoint because none is needed:
 * the catalogue and the tenant's connections are small, already exposed, and
 * already what the rest of the dashboard lists. They are fetched once, the
 * first time the field is focused -- not on every page load, since most visits
 * never touch search.
 *
 * Keyboard first: Ctrl/Cmd+K focuses, arrows move, Enter opens, Escape clears
 * and then leaves. It follows the ARIA combobox pattern, so a screen reader
 * hears the active result rather than silence while arrowing.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Activity,
  Blocks,
  Database,
  LayoutGrid,
  Search,
  Settings,
  User,
  Users,
  Waypoints,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import { FORAGER_CONSOLE_URL, listConnections, listConnectors } from "@/lib/api";
import type { Connection, ConnectorSpec } from "@/lib/api";

/** Results shown per group. Past this, typing more is faster than scrolling. */
const PER_GROUP = 5;

interface PageEntry {
  title: string;
  href: string;
  icon: LucideIcon;
  /** Words people type when they mean this page but not its name. */
  keywords: string;
  external?: boolean;
}

const PAGES: PageEntry[] = [
  { title: "Overview", href: "/dashboard", icon: LayoutGrid, keywords: "home dashboard chart" },
  { title: "MCPs", href: "/dashboard/connectors", icon: Blocks, keywords: "connectors marketplace catalogue add connect" },
  { title: "Data", href: "/dashboard/data", icon: Database, keywords: "connections inventory mcp url endpoints" },
  { title: "Crawler", href: FORAGER_CONSOLE_URL, icon: Waypoints, keywords: "forager crawl worker console seo", external: true },
  { title: "Activity", href: "/dashboard/activity", icon: Activity, keywords: "log calls history usage" },
  { title: "Team", href: "/dashboard/team", icon: Users, keywords: "members invite people" },
  { title: "Settings", href: "/dashboard/settings", icon: Settings, keywords: "organisation organization workspace" },
  { title: "Profile", href: "/dashboard/profile", icon: User, keywords: "account password email name" },
];

type ResultKind = "page" | "connection" | "connector";

interface Result {
  id: string;
  kind: ResultKind;
  title: string;
  detail: string;
  href: string;
  external: boolean;
  icon?: LucideIcon;
  slug?: string;
}

const GROUP_LABEL: Record<ResultKind, string> = {
  page: "Pages",
  connection: "Your connections",
  connector: "Connectors",
};

/**
 * 0 = no match. Higher is better: a title that starts with the query beats one
 * that merely contains it, which beats a match only in the supporting text.
 * Without this, "go" puts Google Search Console below a description that happens
 * to mention "go".
 */
function score(needle: string, title: string, rest: string): number {
  const t = title.toLowerCase();
  if (t.startsWith(needle)) {
    return 3;
  }
  if (t.includes(needle)) {
    return 2;
  }
  return rest.toLowerCase().includes(needle) ? 1 : 0;
}

function rank<T>(items: T[], pick: (item: T) => number): T[] {
  return items
    .map(function withScore(item) {
      return { item: item, s: pick(item) };
    })
    .filter(function matched(entry) {
      return entry.s > 0;
    })
    .sort(function byScore(a, b) {
      return b.s - a.s;
    })
    .slice(0, PER_GROUP)
    .map(function unwrap(entry) {
      return entry.item;
    });
}

function usesCommandKey(): boolean {
  const nav = window.navigator;
  const platform =
    typeof nav.platform === "string" && nav.platform.length > 0 ? nav.platform : nav.userAgent;
  return /Mac|iPhone|iPad|iPod/.test(platform);
}

export default function GlobalSearch() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listId = useId();

  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [shortcutHint, setShortcutHint] = useState("");

  const [connectors, setConnectors] = useState<ConnectorSpec[] | null>(null);
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const loadStarted = useRef(false);

  useEffect(function detectPlatform() {
    setShortcutHint(usesCommandKey() ? "⌘K" : "Ctrl K");
  }, []);

  /* Fetched on first focus. Both halves are independent: a failure to list
     connections must not hide the catalogue, and pages need neither. */
  const ensureLoaded = useCallback(function ensureLoaded() {
    if (loadStarted.current) {
      return;
    }
    loadStarted.current = true;
    listConnectors()
      .then(setConnectors)
      .catch(function fail() {
        setConnectors([]);
        setLoadError(true);
      });
    listConnections()
      .then(setConnections)
      .catch(function fail() {
        setConnections([]);
        setLoadError(true);
      });
  }, []);

  useEffect(function bindShortcut() {
    function onKeyDown(event: KeyboardEvent): void {
      const isK = event.key === "k" || event.key === "K";
      if (isK && (event.metaKey || event.ctrlKey) && !event.altKey) {
        // The browser's own Cmd/Ctrl-K would jump to the address bar.
        event.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return function unbind() {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  const results = useMemo(
    function buildResults(): Result[] {
      const needle = query.trim().toLowerCase();
      if (needle.length === 0) {
        return [];
      }

      const pages = rank(PAGES, function (p) {
        return score(needle, p.title, p.keywords);
      }).map(function toResult(p): Result {
        return {
          id: "page:" + p.href,
          kind: "page",
          title: p.title,
          detail: p.external ? "Opens the crawler console" : "Page",
          href: p.href,
          external: p.external === true,
          icon: p.icon,
        };
      });

      const mine = rank(connections || [], function (c) {
        const title = c.name.trim() || c.connector_label;
        return score(needle, title, c.connector_label + " " + c.connector + " " + c.mcp_url);
      }).map(function toResult(c): Result {
        const title = c.name.trim() || c.connector_label;
        return {
          id: "connection:" + c.id,
          kind: "connection",
          title: title,
          detail: c.status === "error" ? c.connector_label + " · needs attention" : c.connector_label,
          href: "/dashboard/connectors/" + c.connector,
          external: false,
          slug: c.connector,
        };
      });

      const catalogue = rank(connectors || [], function (c) {
        return score(needle, c.label, c.description + " " + c.slug + " " + c.category);
      }).map(function toResult(c): Result {
        return {
          id: "connector:" + c.slug,
          kind: "connector",
          title: c.label,
          detail:
            (c.category ? c.category + " · " : "") +
            c.tool_count +
            (c.tool_count === 1 ? " tool" : " tools") +
            (c.connected_count > 0 ? " · connected" : ""),
          href: "/dashboard/connectors/" + c.slug,
          external: false,
          slug: c.slug,
        };
      });

      return pages.concat(mine, catalogue);
    },
    [query, connections, connectors],
  );

  // A new query starts from the top result, not wherever the cursor was.
  useEffect(
    function resetActive() {
      setActive(0);
    },
    [query],
  );

  const loading = connectors === null || connections === null;
  const showPanel = open && query.trim().length > 0;

  function go(result: Result | undefined): void {
    if (result === undefined) {
      return;
    }
    setOpen(false);
    setQuery("");
    inputRef.current?.blur();
    if (result.external) {
      window.location.assign(result.href);
    } else {
      router.push(result.href);
    }
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>): void {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      if (results.length > 0) {
        setActive(function next(i) {
          return (i + 1) % results.length;
        });
      }
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (results.length > 0) {
        setActive(function prev(i) {
          return (i - 1 + results.length) % results.length;
        });
      }
    } else if (event.key === "Enter") {
      event.preventDefault();
      go(results[active]);
    } else if (event.key === "Escape") {
      // First Escape clears the query; the second gives focus back to the page.
      if (query.length > 0) {
        setQuery("");
      } else {
        inputRef.current?.blur();
      }
    }
  }

  const activeId = showPanel && results[active] ? listId + "-" + active : undefined;

  return (
    <div className="dash-search" role="search">
      <label className="dash-visually-hidden" htmlFor="dash-search-input">
        Search pages, connections and connectors
      </label>
      <Search className="dash-search-icon" size={15} strokeWidth={1.9} aria-hidden="true" />
      <input
        id="dash-search-input"
        ref={inputRef}
        className="dash-search-input"
        type="text"
        name="q"
        placeholder="Search pages, connections, connectors"
        autoComplete="off"
        spellCheck={false}
        role="combobox"
        aria-expanded={showPanel}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeId}
        value={query}
        onFocus={function onFocus() {
          ensureLoaded();
          setOpen(true);
        }}
        onBlur={function onBlur() {
          setOpen(false);
        }}
        onChange={function onChange(event) {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onKeyDown={onKeyDown}
      />
      {shortcutHint !== "" && !showPanel ? (
        <span className="dash-search-hint" aria-hidden="true">
          {shortcutHint}
        </span>
      ) : null}

      {showPanel ? (
        <div className="gsearch-panel">
          {results.length > 0 ? (
            <ul className="gsearch-list" id={listId} role="listbox" aria-label="Search results">
              {results.map(function renderResult(result, index) {
                const Icon = result.icon;
                const startsGroup = index === 0 || results[index - 1].kind !== result.kind;
                return (
                  <li key={result.id} role="presentation">
                    {startsGroup ? (
                      <p className="gsearch-group" aria-hidden="true">
                        {GROUP_LABEL[result.kind]}
                      </p>
                    ) : null}
                    <div
                      id={listId + "-" + index}
                      role="option"
                      aria-selected={index === active}
                      className={index === active ? "gsearch-item is-active" : "gsearch-item"}
                      // mousedown, not click: the input's blur fires before
                      // click would, closing the panel under the pointer.
                      onMouseDown={function onMouseDown(event) {
                        event.preventDefault();
                        go(result);
                      }}
                      onMouseEnter={function onMouseEnter() {
                        setActive(index);
                      }}
                    >
                      <span className="gsearch-mark" aria-hidden="true">
                        {result.slug ? (
                          <ConnectorMark slug={result.slug} label={result.title} size={22} />
                        ) : Icon ? (
                          <Icon size={16} strokeWidth={1.9} />
                        ) : null}
                      </span>
                      <span className="gsearch-text">
                        <span className="gsearch-title">{result.title}</span>
                        <span className="gsearch-detail">{result.detail}</span>
                      </span>
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="gsearch-empty" id={listId} role="status">
              {loading
                ? "Loading…"
                : "Nothing matches “" + query.trim() + "”. Try a connector name, like Google or Meta."}
            </p>
          )}
          {loadError && !loading ? (
            <p className="gsearch-note">Some results could not be loaded. Pages still work.</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
