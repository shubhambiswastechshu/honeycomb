"use client";

/**
 * One connector, and the caller's connections to it.
 *
 * Four tabs, because the four jobs are genuinely different errands and only
 * one is ever being run: add or remove an instance, decide what it is allowed
 * to do, get the URL and key that make it reachable, and see what it has been
 * doing. The tab strip is the same interaction as the Profile panel -- roving
 * tabindex, arrows and Home/End move selection and focus together -- because a
 * second tab idiom in the same product would be a bug, not a variation.
 *
 * Three of the four tabs are about one connection rather than the connector,
 * so a picker appears above them as soon as a second instance exists. With no
 * instances at all they say so and point at the connect action instead of
 * rendering controls that could not do anything.
 *
 * Every value here comes from /api/connectors/ and /api/connections/. Nothing
 * is filled in while loading and nothing is invented when a request fails.
 *
 * This is also where a Google connection lands. The OAuth callback runs on the
 * server and cannot render anything the user should read, so it bounces the
 * browser back to this URL carrying ?connected=1 or ?error=<message>. Reading
 * that needs useSearchParams(), which suspends, so the whole view sits behind a
 * Suspense boundary -- without one the production build fails while
 * prerendering this route.
 */

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  Activity,
  ArrowLeft,
  CircleAlert,
  KeyRound,
  Layers,
  Pencil,
  Plug,
  Plus,
  RefreshCw,
  Trash2,
  Wrench,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import ConfirmDialog from "@/components/dashboard/ConfirmDialog";
import Modal from "@/components/dashboard/Modal";
import McpUrl from "@/components/dashboard/McpUrl";
import ConnectForm from "@/components/dashboard/ConnectForm";
import ConnectorMark from "@/components/dashboard/ConnectorMark";
import EmptyState from "@/components/dashboard/EmptyState";
import McpKeyPanel from "@/components/dashboard/McpKeyPanel";
import {
  deleteConnection,
  getConnector,
  listConnectionActivity,
  listConnectionTools,
  listConnections,
  toggleConnectionTool,
} from "@/lib/api";
import type {
  ActivityRow,
  Connection,
  ConnectorDetail,
  ConnectorTool,
} from "@/lib/api";

const LOAD_FAILED =
  "This connector could not be loaded. Check that the backend is running and try again.";

const ACTIVITY_LIMIT = 50;

interface TabSpec {
  id: string;
  label: string;
  icon: LucideIcon;
}

const TABS: TabSpec[] = [
  { id: "instances", label: "Instances", icon: Layers },
  { id: "tools", label: "Tools", icon: Wrench },
  { id: "access", label: "Access", icon: KeyRound },
  { id: "activity", label: "Activity", icon: Activity },
];

function messageOf(caught: unknown, fallback: string): string {
  if (caught instanceof Error && caught.message.length > 0) {
    return caught.message;
  }
  return fallback;
}

function formatWhen(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) {
    return iso;
  }
  return when.toLocaleString();
}

/** The name a connection shows in a list when its owner left the field blank. */
function connectionTitle(connection: Connection): string {
  const name = connection.name.trim();
  return name.length > 0 ? name : connection.connector_label;
}

/** The message shown after Google sends the browser back with ?connected=1. */
const CONNECTED_NOTE =
  "Google account connected. Its write tools are switched off until you turn them on.";

/**
 * The route itself: nothing but the Suspense boundary the view's
 * useSearchParams() requires. The fallback is the same shell the view renders
 * while it loads, so a reader never sees the page change shape twice.
 */
export default function ConnectorDetailPage({
  params,
}: {
  params: { slug: string };
}) {
  return (
    <Suspense
      fallback={
        <div className="panel">
          <BackLink />
          <h1 className="panel-title">Connector</h1>
          <div className="panel-body">
            <p className="conn-loading">Loading…</p>
          </div>
        </div>
      }
    >
      <ConnectorDetailView slug={params.slug} />
    </Suspense>
  );
}

function ConnectorDetailView({ slug }: { slug: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();

  const [connector, setConnector] = useState<ConnectorDetail | null>(null);
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [current, setCurrent] = useState<string>(TABS[0].id);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [connecting, setConnecting] = useState<boolean>(false);
  const [editing, setEditing] = useState<Connection | null>(null);

  // The outcome of a round trip through Google, read once out of the URL.
  const [oauthNote, setOauthNote] = useState<string | null>(null);
  const [oauthError, setOauthError] = useState<string | null>(null);

  const [pendingDelete, setPendingDelete] = useState<Connection | null>(null);
  const [deleting, setDeleting] = useState<boolean>(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const listRef = useRef<HTMLDivElement | null>(null);
  const aliveRef = useRef<boolean>(true);

  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  useEffect(
    function load() {
      let alive = true;
      setConnector(null);
      setConnections(null);
      setLoadError(null);

      Promise.all([getConnector(slug), listConnections(slug)])
        .then(function apply(result: [ConnectorDetail, Connection[]]) {
          if (!alive) {
            return;
          }
          setConnector(result[0]);
          setConnections(result[1]);
        })
        .catch(function fail(caught: unknown) {
          if (!alive) {
            return;
          }
          setLoadError(messageOf(caught, LOAD_FAILED));
        });

      return function stop() {
        alive = false;
      };
    },
    [slug]
  );

  const reloadConnections = useCallback(
    async function reloadConnections(): Promise<void> {
      const rows = await listConnections(slug);
      if (aliveRef.current) {
        setConnections(rows);
      }
    },
    [slug]
  );

  /* ---- The return leg of the Google flow ---- */

  const connectedParam = search.get("connected");
  const errorParam = search.get("error");
  const consumedRef = useRef<boolean>(false);

  useEffect(
    function consumeOAuthReturn() {
      // Reset rather than return early, so a second round trip through Google
      // in the same mounted page is read again instead of being swallowed.
      if (connectedParam === null && errorParam === null) {
        consumedRef.current = false;
        return;
      }
      if (consumedRef.current) {
        return;
      }
      consumedRef.current = true;

      if (errorParam !== null && errorParam.length > 0) {
        setOauthNote(null);
        // The callback's message is the server's, and for a misconfiguration it
        // names the redirect URI to register, so it is shown exactly as sent.
        setOauthError(errorParam);
      } else {
        setOauthError(null);
        setOauthNote(CONNECTED_NOTE);
        // The connection was created by the callback, so this page has never
        // seen it. Nothing below is right until the list is re-read.
        void reloadConnections();
      }

      // The form the user left to go to Google is stale now either way.
      setConnecting(false);
      setEditing(null);

      // Back to the clean path: leaving the parameter in place would replay
      // this banner on every refresh, long after it stopped being true.
      router.replace(pathname);
    },
    [connectedParam, errorParam, pathname, reloadConnections, router]
  );

  const rows = connections !== null ? connections : [];
  const selected =
    rows.find(function isSelected(row: Connection) {
      return row.id === selectedId;
    }) ??
    (rows.length > 0 ? rows[0] : null);

  /* ---- Tab strip: identical behaviour to the Profile panel ---- */

  function selectByIndex(index: number): void {
    const next = TABS[(index + TABS.length) % TABS.length];
    setCurrent(next.id);
    const list = listRef.current;
    if (list !== null) {
      const button = list.querySelector<HTMLButtonElement>(
        "#connector-tab-" + next.id
      );
      if (button !== null) {
        button.focus();
      }
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const index = TABS.findIndex(function isCurrent(tab: TabSpec) {
      return tab.id === current;
    });
    if (event.key === "ArrowRight") {
      event.preventDefault();
      selectByIndex(index + 1);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      selectByIndex(index - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      selectByIndex(0);
    } else if (event.key === "End") {
      event.preventDefault();
      selectByIndex(TABS.length - 1);
    }
  }

  /* ---- Mutations ---- */

  function handleSaved(saved: Connection): void {
    setSelectedId(saved.id);
    void reloadConnections();
    if (editing !== null) {
      // An edit stays put: closing the form here would unmount the success
      // note before it was ever painted, so the save would look like nothing.
      return;
    }
    setConnecting(false);
    // A brand new connection is useless until its URL and key are in hand, so
    // the page goes where that work happens rather than back to a list.
    setCurrent("access");
  }

  function handleDelete(): void {
    const target = pendingDelete;
    if (target === null || deleting) {
      return;
    }
    setDeleting(true);
    setDeleteError(null);
    void deleteConnection(target.id)
      .then(async function done(): Promise<void> {
        await reloadConnections();
        if (aliveRef.current) {
          setPendingDelete(null);
          if (selectedId === target.id) {
            setSelectedId(null);
          }
        }
      })
      .catch(function fail(caught: unknown) {
        if (!aliveRef.current) {
          return;
        }
        // The dialog has nowhere to put a failure, so it closes and the
        // message lands on the list behind it rather than nowhere at all.
        setPendingDelete(null);
        setDeleteError(
          messageOf(caught, "This connection could not be deleted.")
        );
      })
      .then(function settle() {
        if (aliveRef.current) {
          setDeleting(false);
        }
      });
  }

  /* ---- Render ---- */

  if (loadError !== null) {
    return (
      <div className="panel">
        <BackLink />
        <h1 className="panel-title">Connector</h1>
        <div className="panel-body">
          <p className="error" role="alert">
            {loadError}
          </p>
        </div>
      </div>
    );
  }

  if (connector === null || connections === null) {
    return (
      <div className="panel">
        <BackLink />
        <h1 className="panel-title">Connector</h1>
        <div className="panel-body">
          <p className="conn-loading">Loading…</p>
        </div>
      </div>
    );
  }

  const active =
    TABS.find(function isCurrent(tab: TabSpec) {
      return tab.id === current;
    }) ?? TABS[0];

  return (
    <div className="panel">
      <BackLink />
      <h1 className="panel-title">{connector.label}</h1>
      <p className="panel-lede">
        {connector.description.length > 0
          ? connector.description
          : "Connect this source and expose its tools to Claude."}
      </p>

      <div className="panel-body acct-stack">
        {oauthError !== null ? (
          <p className="error acct-error" role="alert">
            {oauthError}
          </p>
        ) : null}

        {oauthNote !== null ? (
          <p className="conn-note" role="status">
            {oauthNote}
          </p>
        ) : null}

        <div className="conn-head">
          {/* The marketplace card's mark, at the size a page header wants --
              same component, so a connector looks like itself on both screens. */}
          <ConnectorMark slug={connector.slug} label={connector.label} size={46} />
          <div className="conn-head-body">
            <p className="conn-meta">
              {connector.category.length > 0 ? (
                <span className="conn-badge">{connector.category}</span>
              ) : null}
              <span className="conn-badge">{connector.auth}</span>
              <span className="conn-meta-text">
                {connector.tool_count === 1
                  ? "1 tool"
                  : String(connector.tool_count) + " tools"}
                {" · "}
                {rows.length === 1
                  ? "1 connection"
                  : String(rows.length) + " connections"}
              </span>
            </p>
          </div>
          <div className="conn-head-actions">
            <button
              type="button"
              className="conn-action conn-action-primary"
              onClick={function startConnect() {
                setEditing(null);
                setConnecting(true);
                setCurrent("instances");
              }}
            >
              <Plus size={15} strokeWidth={2} aria-hidden="true" />
              <span>Connect</span>
            </button>
          </div>
        </div>

        <div className="acct-tabs">
          <div
            className="acct-tablist"
            role="tablist"
            aria-label={connector.label + " sections"}
            ref={listRef}
            onKeyDown={onKeyDown}
          >
            {TABS.map(function renderTab(tab: TabSpec) {
              const Icon = tab.icon;
              const isCurrent = tab.id === current;
              return (
                <button
                  key={tab.id}
                  type="button"
                  id={"connector-tab-" + tab.id}
                  className="acct-tab"
                  role="tab"
                  aria-selected={isCurrent}
                  aria-controls={"connector-panel-" + tab.id}
                  tabIndex={isCurrent ? 0 : -1}
                  onClick={function onClick() {
                    setCurrent(tab.id);
                  }}
                >
                  <Icon size={15} strokeWidth={1.9} aria-hidden="true" />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </div>

          <div
            className="acct-tabpanel"
            id={"connector-panel-" + active.id}
            role="tabpanel"
            aria-labelledby={"connector-tab-" + active.id}
            tabIndex={-1}
          >
            {active.id === "instances" ? (
              <InstancesTab
                connector={connector}
                connections={rows}
                connecting={connecting}
                editing={editing}
                onStartConnect={function startConnect() {
                  setEditing(null);
                  setConnecting(true);
                }}
                onCancelForm={function cancelForm() {
                  setConnecting(false);
                  setEditing(null);
                }}
                onEdit={function startEdit(row: Connection) {
                  setConnecting(false);
                  setEditing(row);
                }}
                onSaved={handleSaved}
                onAskDelete={function askDelete(row: Connection) {
                  setDeleteError(null);
                  setPendingDelete(row);
                }}
                onManage={function manage(row: Connection) {
                  setSelectedId(row.id);
                  setCurrent("tools");
                }}
                deleteError={deleteError}
              />
            ) : null}

            {active.id !== "instances" ? (
              <div className="acct-stack">
                <InstancePicker
                  connections={rows}
                  selected={selected}
                  onSelect={function onSelect(row: Connection) {
                    setSelectedId(row.id);
                  }}
                />

                {active.id === "tools" ? (
                  <ToolsTab connector={connector} connection={selected} />
                ) : null}

                {active.id === "access" ? (
                  selected !== null ? (
                    <McpKeyPanel
                      key={selected.id}
                      connection={selected}
                      onKeysChanged={function refreshCounts() {
                        void reloadConnections();
                      }}
                    />
                  ) : (
                    <NoInstances
                      onConnect={function startConnect() {
                        setEditing(null);
                        setConnecting(true);
                        setCurrent("instances");
                      }}
                    />
                  )
                ) : null}

                {active.id === "activity" ? (
                  selected !== null ? (
                    <ActivityTab key={selected.id} connection={selected} />
                  ) : (
                    <NoInstances
                      onConnect={function startConnect() {
                        setEditing(null);
                        setConnecting(true);
                        setCurrent("instances");
                      }}
                    />
                  )
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this connection?"
        description={
          <>
            <strong>
              {pendingDelete !== null ? connectionTitle(pendingDelete) : ""}
            </strong>{" "}
            will stop answering, its keys are destroyed with it, and its stored
            credentials are deleted. Anything in Claude still pointing at its
            URL will break. This cannot be undone.
          </>
        }
        confirmLabel="Delete connection"
        pendingLabel="Deleting"
        destructive
        pending={deleting}
        onConfirm={handleDelete}
        onCancel={function cancelDelete() {
          setPendingDelete(null);
          setDeleteError(null);
        }}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Chrome                                                              */
/* ------------------------------------------------------------------ */

function BackLink() {
  return (
    <Link className="conn-back" href="/dashboard/connectors">
      <ArrowLeft size={14} strokeWidth={2} aria-hidden="true" />
      <span>All connectors</span>
    </Link>
  );
}

function NoInstances({ onConnect }: { onConnect: () => void }) {
  return (
    <div className="conn-empty-wrap">
      <EmptyState
        icon={Plug}
        title="Nothing connected yet"
        description="Connect this source first. Its tools, endpoint and activity all belong to a connection."
      />
      <div className="conn-empty-actions">
        <button
          type="button"
          className="conn-action conn-action-primary"
          onClick={onConnect}
        >
          <Plus size={15} strokeWidth={2} aria-hidden="true" />
          <span>Connect</span>
        </button>
      </div>
    </div>
  );
}

/** Only rendered when the choice is real: one connection needs no picker. */
function InstancePicker({
  connections,
  selected,
  onSelect,
}: {
  connections: Connection[];
  selected: Connection | null;
  onSelect: (connection: Connection) => void;
}) {
  if (connections.length < 2) {
    return null;
  }
  return (
    <div className="conn-picker">
      <span className="conn-picker-label">Connection</span>
      {connections.map(function renderChoice(row: Connection) {
        const isCurrent = selected !== null && selected.id === row.id;
        return (
          <button
            key={row.id}
            type="button"
            className="conn-picker-item"
            aria-pressed={isCurrent}
            onClick={function choose() {
              onSelect(row);
            }}
          >
            {connectionTitle(row)}
          </button>
        );
      })}
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const failing = status === "error";
  return (
    <span
      className={failing ? "conn-status conn-status-error" : "conn-status"}
    >
      <span className="conn-status-dot" aria-hidden="true" />
      <span>{failing ? "Error" : "Active"}</span>
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Instances                                                           */
/* ------------------------------------------------------------------ */

interface InstancesTabProps {
  connector: ConnectorDetail;
  connections: Connection[];
  connecting: boolean;
  editing: Connection | null;
  onStartConnect: () => void;
  onCancelForm: () => void;
  onEdit: (connection: Connection) => void;
  onSaved: (connection: Connection) => void;
  onAskDelete: (connection: Connection) => void;
  onManage: (connection: Connection) => void;
  deleteError: string | null;
}

function InstancesTab({
  connector,
  connections,
  connecting,
  editing,
  onStartConnect,
  onCancelForm,
  onEdit,
  onSaved,
  onAskDelete,
  onManage,
  deleteError,
}: InstancesTabProps) {
  const showForm = connecting || editing !== null;

  return (
    <div className="acct-stack">
      {/* Connecting is a modal, the same shell the confirmations use: the page
          behind it blurs, Escape and the backdrop dismiss it, and focus is
          trapped in the form. Inline, it pushed the instance list down the
          page and left no clear way back out of a half-filled form. */}
      <Modal
        open={showForm}
        onClose={onCancelForm}
        panelClassName="hc-modal-form"
        showClose
        labelledBy="connect-form-title"
      >
        <ConnectForm
          key={editing !== null ? "edit-" + String(editing.id) : "create"}
          connector={connector}
          existing={editing}
          onSaved={onSaved}
        />
      </Modal>

      {deleteError !== null ? (
        <p className="error acct-error" role="alert">
          {deleteError}
        </p>
      ) : null}

      {connections.length === 0 && !showForm ? (
        <NoInstances onConnect={onStartConnect} />
      ) : null}

      {connections.length > 0 ? (
        <ul className="conn-list">
          {connections.map(function renderConnection(row: Connection) {
            return (
              <li className="conn-row" key={row.id}>
                <span className="conn-row-icon" aria-hidden="true">
                  <Plug size={15} strokeWidth={1.8} />
                </span>
                <div className="conn-row-body">
                  <p className="conn-row-title">
                    {connectionTitle(row)}
                    <StatusPill status={row.status} />
                  </p>
                  <p className="conn-row-meta">
                    {String(row.tool_count) +
                      (row.tool_count === 1 ? " tool" : " tools")}
                    {row.disabled_tools.length > 0
                      ? " (" + String(row.disabled_tools.length) + " off)"
                      : ""}
                    {" · " +
                      String(row.key_count) +
                      (row.key_count === 1 ? " key" : " keys")}
                    {" · added " + formatWhen(row.created_at)}
                  </p>
                  {row.status === "error" && row.last_error.length > 0 ? (
                    <p className="conn-row-error">
                      <CircleAlert
                        size={13}
                        strokeWidth={1.9}
                        aria-hidden="true"
                      />
                      <span>{row.last_error}</span>
                    </p>
                  ) : null}
                  {/* The URL was only on the Access tab, which meant the one
                      thing this whole product exists to hand over took three
                      clicks to find. */}
                  <McpUrl
                    url={row.mcp_url}
                    label={"Copy the MCP URL for " + connectionTitle(row)}
                  />
                </div>
                <div className="conn-row-actions">
                  <button
                    type="button"
                    className="conn-action"
                    onClick={function manage() {
                      onManage(row);
                    }}
                  >
                    <Wrench size={14} strokeWidth={1.9} aria-hidden="true" />
                    <span>Tools</span>
                  </button>
                  <button
                    type="button"
                    className="conn-action"
                    onClick={function edit() {
                      onEdit(row);
                    }}
                  >
                    <Pencil size={14} strokeWidth={1.9} aria-hidden="true" />
                    <span>Edit</span>
                  </button>
                  <button
                    type="button"
                    className="conn-action conn-action-danger"
                    onClick={function remove() {
                      onAskDelete(row);
                    }}
                  >
                    <Trash2 size={14} strokeWidth={1.9} aria-hidden="true" />
                    <span>Delete</span>
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Tools                                                               */
/* ------------------------------------------------------------------ */

/**
 * With a connection the list is live and each row toggles; without one it is
 * the connector's catalog, read only. Write tools carry a badge in both cases:
 * a tool that can change a customer's record must not be indistinguishable
 * from one that reads a row back.
 */
function ToolsTab({
  connector,
  connection,
}: {
  connector: ConnectorDetail;
  connection: Connection | null;
}) {
  const [tools, setTools] = useState<ConnectorTool[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const aliveRef = useRef<boolean>(true);
  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  const connectionId = connection !== null ? connection.id : null;

  useEffect(
    function loadTools() {
      if (connectionId === null) {
        setTools(null);
        setError(null);
        return;
      }
      let alive = true;
      setTools(null);
      setError(null);
      listConnectionTools(connectionId)
        .then(function apply(rows: ConnectorTool[]) {
          if (alive) {
            setTools(rows);
          }
        })
        .catch(function fail(caught: unknown) {
          if (alive) {
            setError(messageOf(caught, "The tool list could not be loaded."));
          }
        });
      return function stop() {
        alive = false;
      };
    },
    [connectionId]
  );

  function toggle(tool: ConnectorTool): void {
    if (connectionId === null || busy !== null) {
      return;
    }
    const next = tool.enabled === false;
    setBusy(tool.name);
    setError(null);
    void toggleConnectionTool(connectionId, tool.name, next)
      .then(function apply(rows: ConnectorTool[]) {
        if (aliveRef.current) {
          setTools(rows);
        }
      })
      .catch(function fail(caught: unknown) {
        if (aliveRef.current) {
          setError(messageOf(caught, "That tool could not be changed."));
        }
      })
      .then(function settle() {
        if (aliveRef.current) {
          setBusy(null);
        }
      });
  }

  const catalog = connection === null ? connector.tools : tools;

  return (
    <div className="acct-stack">
      {connection === null ? (
        <p className="conn-note">
          This is what {connector.label} can do. Connect it to choose which of
          these tools Claude is allowed to call.
        </p>
      ) : null}

      {error !== null ? (
        <p className="error acct-error" role="alert">
          {error}
        </p>
      ) : null}

      {catalog === null && error === null ? (
        <p className="conn-loading">Loading tools…</p>
      ) : null}

      {catalog !== null && catalog.length === 0 ? (
        <EmptyState
          icon={Wrench}
          title="No tools"
          description="This connector publishes no tools yet."
        />
      ) : null}

      {catalog !== null && catalog.length > 0 ? (
        <ul className="conn-list conn-list-framed">
          {catalog.map(function renderTool(tool: ConnectorTool) {
            const enabled = tool.enabled !== false;
            return (
              <li className="conn-row conn-tool" key={tool.name}>
                <div className="conn-row-body">
                  <p className="conn-row-title">
                    <code className="conn-tool-name">{tool.name}</code>
                    {tool.write ? (
                      <span className="conn-badge conn-badge-write">
                        Writes data
                      </span>
                    ) : null}
                  </p>
                  <p className="conn-row-meta">{tool.description}</p>
                </div>
                <div className="conn-row-actions">
                  {connection === null ? null : (
                    <button
                      type="button"
                      className="conn-switch"
                      role="switch"
                      aria-checked={enabled}
                      aria-label={
                        (enabled ? "Disable " : "Enable ") + tool.name
                      }
                      disabled={busy !== null}
                      onClick={function onToggle() {
                        toggle(tool);
                      }}
                    >
                      <span className="conn-switch-knob" aria-hidden="true" />
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Activity                                                            */
/* ------------------------------------------------------------------ */

function ActivityTab({ connection }: { connection: Connection }) {
  const [rows, setRows] = useState<ActivityRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloading, setReloading] = useState<boolean>(false);
  const [nonce, setNonce] = useState<number>(0);

  const connectionId = connection.id;

  useEffect(
    function loadActivity() {
      let alive = true;
      setError(null);
      listConnectionActivity(connectionId, ACTIVITY_LIMIT)
        .then(function apply(result: ActivityRow[]) {
          if (alive) {
            setRows(result);
          }
        })
        .catch(function fail(caught: unknown) {
          if (alive) {
            setError(messageOf(caught, "Activity could not be loaded."));
          }
        })
        .then(function settle() {
          if (alive) {
            setReloading(false);
          }
        });
      return function stop() {
        alive = false;
      };
    },
    [connectionId, nonce]
  );

  return (
    <div className="acct-stack">
      <div className="conn-activity-head">
        <p className="conn-note">
          The last {ACTIVITY_LIMIT} tool calls made through this connection.
        </p>
        <button
          type="button"
          className="conn-action"
          disabled={reloading}
          onClick={function refresh() {
            setReloading(true);
            setNonce(function bump(previous: number) {
              return previous + 1;
            });
          }}
        >
          <RefreshCw size={14} strokeWidth={1.9} aria-hidden="true" />
          <span>{reloading ? "Refreshing" : "Refresh"}</span>
        </button>
      </div>

      {error !== null ? (
        <p className="error acct-error" role="alert">
          {error}
        </p>
      ) : null}

      {rows === null && error === null ? (
        <p className="conn-loading">Loading activity…</p>
      ) : null}

      {rows !== null && rows.length === 0 ? (
        <EmptyState
          icon={Activity}
          title="No calls yet"
          description="Once Claude calls a tool on this connection, every call shows up here."
        />
      ) : null}

      {rows !== null && rows.length > 0 ? (
        <ul className="conn-list">
          {rows.map(function renderRow(row: ActivityRow) {
            const failed = row.status !== "ok";
            return (
              <li className="conn-row" key={row.id}>
                <div className="conn-row-body">
                  <p className="conn-row-title">
                    <code className="conn-tool-name">{row.tool_name}</code>
                    <span
                      className={
                        failed
                          ? "conn-status conn-status-error"
                          : "conn-status"
                      }
                    >
                      <span className="conn-status-dot" aria-hidden="true" />
                      <span>{failed ? "Error" : "OK"}</span>
                    </span>
                  </p>
                  <p className="conn-row-meta">
                    {formatWhen(row.created_at)}
                    {row.duration_ms !== null
                      ? " · " + String(row.duration_ms) + " ms"
                      : ""}
                  </p>
                  {failed && row.error_message.length > 0 ? (
                    <p className="conn-row-error">
                      <CircleAlert
                        size={13}
                        strokeWidth={1.9}
                        aria-hidden="true"
                      />
                      <span>{row.error_message}</span>
                    </p>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
