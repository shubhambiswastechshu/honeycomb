"use client";

/**
 * The Tools tab: which of a connector's tools this connection lets Claude call.
 *
 * Built for connectors with dozens of tools. A flat list of sixty switches is a
 * list nobody reads, so tools are grouped the way the connector files them
 * (Pages, Instagram, Ads...), each group has one switch for all of it, and a
 * search and a filter narrow the list to the handful someone came to change.
 * Every row says the provider permission the tool needs, because "why does this
 * tool fail" is almost always "the token was never granted that".
 *
 * Switches apply at once and optimistically; the server's answer replaces the
 * local state, and a failure puts the previous state back and says so. Turning
 * on anything that changes data asks first -- that is the one switch whose
 * mistake costs money or a customer's record.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Search, ShieldAlert, Wrench } from "lucide-react";
import ConfirmDialog from "@/components/dashboard/ConfirmDialog";
import EmptyState from "@/components/dashboard/EmptyState";
import { listConnectionTools, toggleConnectionTools } from "@/lib/api";
import type { Connection, ConnectorDetail, ConnectorTool } from "@/lib/api";
import "./tool-switches.css";

type Filter = "all" | "on" | "off" | "write";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "on", label: "On" },
  { key: "off", label: "Off" },
  { key: "write", label: "Changes data" },
];

const UNGROUPED = "Tools";

interface PendingChange {
  names: string[];
  enabled: boolean;
  writes: string[];
}

/** "instagram_business_discovery" -> "Instagram business discovery". */
function titleOf(name: string): string {
  const words = name.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function isOn(tool: ConnectorTool): boolean {
  return tool.enabled !== false;
}

function matches(tool: ConnectorTool, query: string, filter: Filter): boolean {
  if (filter === "on" && !isOn(tool)) return false;
  if (filter === "off" && isOn(tool)) return false;
  if (filter === "write" && !tool.write) return false;
  if (query.length === 0) return true;
  const haystack = [tool.name, titleOf(tool.name), tool.description, tool.permission || "", tool.group || ""]
    .join(" ")
    .toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .every((word) => haystack.includes(word));
}

export default function ToolSwitches({
  connector,
  connection,
}: {
  connector: ConnectorDetail;
  connection: Connection | null;
}) {
  const connectionId = connection !== null ? connection.id : null;
  const readOnly = connectionId === null;

  const [tools, setTools] = useState<ConnectorTool[] | null>(readOnly ? connector.tools : null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [query, setQuery] = useState<string>("");
  const [filter, setFilter] = useState<Filter>("all");
  const [pending, setPending] = useState<PendingChange | null>(null);

  const aliveRef = useRef<boolean>(true);
  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  useEffect(
    function loadTools() {
      if (connectionId === null) {
        setTools(connector.tools);
        setError(null);
        return;
      }
      let alive = true;
      setTools(null);
      setError(null);
      listConnectionTools(connectionId)
        .then(function apply(rows: ConnectorTool[]) {
          if (alive) setTools(rows);
        })
        .catch(function fail(caught: unknown) {
          if (alive) {
            setError(caught instanceof Error && caught.message ? caught.message : "The tool list could not be loaded.");
          }
        });
      return function stop() {
        alive = false;
      };
    },
    [connectionId, connector.tools]
  );

  const all = useMemo(() => tools || [], [tools]);
  const onCount = all.filter(isOn).length;

  /** Every tool per group, in the order the connector lists them. */
  const groups = useMemo(
    function group() {
      const order: string[] = [];
      const byGroup = new Map<string, ConnectorTool[]>();
      for (const tool of all) {
        const name = tool.group && tool.group.length > 0 ? tool.group : UNGROUPED;
        if (!byGroup.has(name)) {
          byGroup.set(name, []);
          order.push(name);
        }
        byGroup.get(name)!.push(tool);
      }
      return order.map((name) => ({ name: name, tools: byGroup.get(name)! }));
    },
    [all]
  );

  const visibleCount = all.filter((t) => matches(t, query.trim(), filter)).length;
  const filterCounts: Record<Filter, number> = {
    all: all.length,
    on: onCount,
    off: all.length - onCount,
    write: all.filter((t) => t.write).length,
  };

  function apply(names: string[], enabled: boolean): void {
    if (connectionId === null || busy || names.length === 0) return;
    const previous = tools;
    setTools((current) =>
      current === null ? current : current.map((t) => (names.includes(t.name) ? { ...t, enabled: enabled } : t))
    );
    setBusy(true);
    setError(null);
    void toggleConnectionTools(connectionId, names, enabled)
      .then(function settle(rows: ConnectorTool[]) {
        if (aliveRef.current) setTools(rows);
      })
      .catch(function fail(caught: unknown) {
        if (!aliveRef.current) return;
        setTools(previous);
        setError(
          caught instanceof Error && caught.message ? caught.message : "Those switches could not be changed. Try again."
        );
      })
      .then(function done() {
        if (aliveRef.current) setBusy(false);
      });
  }

  /** Turning on a tool that changes data asks first; everything else applies at once. */
  function request(names: string[], enabled: boolean): void {
    const writes = all.filter((t) => names.includes(t.name) && t.write && !isOn(t)).map((t) => t.name);
    if (enabled && writes.length > 0) {
      setPending({ names: names, enabled: enabled, writes: writes });
      return;
    }
    apply(names, enabled);
  }

  if (error !== null && tools === null) {
    return (
      <p className="error acct-error" role="alert">
        {error}
      </p>
    );
  }
  if (tools === null) {
    return <p className="conn-loading">Loading tools…</p>;
  }
  if (all.length === 0) {
    return <EmptyState icon={Wrench} title="No tools" description="This connector publishes no tools yet." />;
  }

  const shownNames = all.filter((t) => matches(t, query.trim(), filter)).map((t) => t.name);
  const percent = Math.round((100 * onCount) / all.length);

  return (
    <div className="ts">
      {readOnly ? (
        <p className="conn-note">
          This is everything {connector.label} can do. Connect it to choose which of these tools Claude is allowed to
          call.
        </p>
      ) : null}

      <div className="ts-summary">
        <div className="ts-count">
          <p>
            <strong>{onCount}</strong> of {all.length} tools {readOnly ? "available" : "on"}
          </p>
          {!readOnly ? (
            <span className="ts-meter" aria-hidden="true">
              <span style={{ width: percent + "%" }} />
            </span>
          ) : null}
        </div>

        <label className="ts-search">
          <Search size={15} aria-hidden="true" />
          <span className="ts-sr">Search tools</span>
          <input
            type="search"
            placeholder="Search tools, permissions or groups"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>

        <div className="ts-filters" role="group" aria-label="Show tools">
          {FILTERS.map((f) =>
            readOnly && (f.key === "on" || f.key === "off") ? null : (
              <button
                key={f.key}
                type="button"
                className="ts-filter"
                aria-pressed={filter === f.key}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
                <span className="ts-filter-n">{filterCounts[f.key]}</span>
              </button>
            )
          )}
        </div>

        {!readOnly ? (
          <div className="ts-bulk">
            <button
              type="button"
              className="conn-action"
              disabled={busy || shownNames.length === 0}
              onClick={() => request(shownNames, true)}
            >
              {query || filter !== "all" ? "Turn on shown" : "Turn all on"}
            </button>
            <button
              type="button"
              className="conn-action"
              disabled={busy || shownNames.length === 0}
              onClick={() => request(shownNames, false)}
            >
              {query || filter !== "all" ? "Turn off shown" : "Turn all off"}
            </button>
          </div>
        ) : null}
      </div>

      {error !== null ? (
        <p className="error acct-error" role="alert">
          {error}
        </p>
      ) : null}

      {visibleCount === 0 ? <p className="conn-note">No tools match that search.</p> : null}

      {groups.map(function renderGroup(group) {
        const shown = group.tools.filter((t) => matches(t, query.trim(), filter));
        if (shown.length === 0) return null;
        const on = group.tools.filter(isOn).length;
        const state = on === 0 ? "off" : on === group.tools.length ? "on" : "mixed";
        const permissions = Array.from(new Set(group.tools.map((t) => t.permission || "").filter(Boolean)));
        const headingId = "ts-group-" + group.name.replace(/[^a-z0-9]+/gi, "-").toLowerCase();

        return (
          <section key={group.name} className="ts-group" aria-labelledby={headingId}>
            <header className="ts-group-head">
              <div className="ts-group-text">
                <h3 id={headingId}>{group.name}</h3>
                <p className="ts-group-meta">
                  {readOnly
                    ? group.tools.length + (group.tools.length === 1 ? " tool" : " tools")
                    : on + " of " + group.tools.length + " on"}
                </p>
                {permissions.length > 0 ? (
                  <p className="ts-perms">
                    {permissions.map((p) => (
                      <code key={p} className="ts-perm">
                        {p}
                      </code>
                    ))}
                  </p>
                ) : null}
              </div>
              {!readOnly ? (
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={state === "mixed" ? "mixed" : state === "on"}
                  aria-label={"All " + group.name + " tools"}
                  className={"ts-switch ts-switch-group is-" + state}
                  disabled={busy}
                  onClick={() =>
                    request(
                      group.tools.map((t) => t.name),
                      state !== "on"
                    )
                  }
                >
                  <span className="ts-knob" aria-hidden="true" />
                </button>
              ) : null}
            </header>

            <ul className="ts-list">
              {shown.map(function renderTool(tool) {
                const enabled = isOn(tool);
                return (
                  <li key={tool.name} className={enabled || readOnly ? "ts-tool" : "ts-tool is-off"}>
                    <div className="ts-tool-body">
                      <p className="ts-tool-title">
                        <span>{titleOf(tool.name)}</span>
                        {tool.write ? (
                          <span className="ts-badge is-write">
                            <ShieldAlert size={12} aria-hidden="true" />
                            Changes data
                          </span>
                        ) : null}
                      </p>
                      <p className="ts-tool-desc">{tool.description}</p>
                      <p className="ts-tool-meta">
                        <code>{tool.name}</code>
                        {tool.permission ? (
                          <span>
                            needs <code>{tool.permission}</code>
                          </span>
                        ) : null}
                      </p>
                    </div>
                    {!readOnly ? (
                      <button
                        type="button"
                        role="switch"
                        aria-checked={enabled}
                        aria-label={(enabled ? "Turn off " : "Turn on ") + titleOf(tool.name)}
                        className={enabled ? "ts-switch is-on" : "ts-switch is-off"}
                        disabled={busy}
                        onClick={() => request([tool.name], !enabled)}
                      >
                        <span className="ts-knob" aria-hidden="true" />
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}

      <ConfirmDialog
        open={pending !== null}
        title={pending !== null && pending.writes.length === 1 ? "Let Claude change data?" : "Turn on tools that change data?"}
        description={
          pending !== null ? (
            <>
              {pending.writes.length === 1 ? (
                <>
                  <strong>{titleOf(pending.writes[0])}</strong> changes things in the connected account
                </>
              ) : (
                <>
                  <strong>{pending.writes.length} of these tools</strong> change things in the connected account (
                  {pending.writes.map(titleOf).join(", ")})
                </>
              )}
              . Once on, any AI client using this connection can call {pending.writes.length === 1 ? "it" : "them"}.
              You can switch {pending.writes.length === 1 ? "it" : "them"} off again at any time.
            </>
          ) : (
            ""
          )
        }
        confirmLabel="Turn on"
        pendingLabel="Turning on"
        destructive
        pending={busy}
        onConfirm={function confirm() {
          const change = pending;
          setPending(null);
          if (change !== null) apply(change.names, change.enabled);
        }}
        onCancel={function cancel() {
          setPending(null);
        }}
      />
    </div>
  );
}
