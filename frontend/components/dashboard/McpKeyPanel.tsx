"use client";

/**
 * Everything needed to point Claude at one connection: the MCP URL and the
 * bearer keys that make it answer.
 *
 * The one-time token is the reason this component exists in its own file.
 * POST /keys/ returns the plaintext once and the server keeps only a hash, so
 * the value in `minted` below is the only copy in the world. It is held in
 * React state and nowhere else -- not in browser storage, not in a ref that
 * outlives the panel, not in the URL. Navigating away, switching connections
 * or dismissing the callout drops it for good, which is exactly what the
 * warning next to it promises.
 *
 * The URL is not a secret and the keys are; they are shown as two separate
 * blocks so that difference is visible rather than implied.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Check, Copy, KeyRound, Trash2, TriangleAlert } from "lucide-react";
import ConfirmDialog from "@/components/dashboard/ConfirmDialog";
import { copyText } from "@/components/dashboard/McpUrl";
import { listKeys, mintKey, revokeKey } from "@/lib/api";
import type { Connection, McpKeyRow } from "@/lib/api";
import {
  FormFeedback,
  SectionCard,
  SubmitButton,
  TextField,
  useSubmitState,
} from "@/components/dashboard/AccountForms";

const COPIED_TIMEOUT_MS = 2000;

const LOAD_FAILED = "The keys for this connection could not be loaded.";


function formatWhen(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) {
    return iso;
  }
  return when.toLocaleString();
}

export interface McpKeyPanelProps {
  connection: Connection;
  /** Called after a key is minted or revoked so key_count stays honest. */
  onKeysChanged?: () => void;
}

export default function McpKeyPanel({
  connection,
  onKeysChanged,
}: McpKeyPanelProps) {
  const connectionId = connection.id;

  const [keys, setKeys] = useState<McpKeyRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  /** The plaintext token, for as long as this component is mounted. */
  const [minted, setMinted] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<McpKeyRow | null>(null);
  const [revoking, setRevoking] = useState<boolean>(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  const mintState = useSubmitState();
  const [label, setLabel] = useState<string>("");

  const aliveRef = useRef<boolean>(true);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
      if (copyTimerRef.current !== null) {
        clearTimeout(copyTimerRef.current);
        copyTimerRef.current = null;
      }
    };
  }, []);

  const reload = useCallback(
    async function reload(): Promise<void> {
      const rows = await listKeys(connectionId);
      if (aliveRef.current) {
        setKeys(rows);
      }
    },
    [connectionId]
  );

  useEffect(
    function loadKeys() {
      let current = true;
      setKeys(null);
      setLoadError(null);
      // A token belongs to the connection it was minted for; showing it after
      // a switch would attach it to the wrong URL.
      setMinted(null);
      setPendingRevoke(null);
      setRevokeError(null);

      listKeys(connectionId)
        .then(function apply(rows: McpKeyRow[]) {
          if (current) {
            setKeys(rows);
          }
        })
        .catch(function fail(caught: unknown) {
          if (!current) {
            return;
          }
          setLoadError(
            caught instanceof Error && caught.message.length > 0
              ? caught.message
              : LOAD_FAILED
          );
        });

      return function stop() {
        current = false;
      };
    },
    [connectionId]
  );

  function flagCopied(what: string): void {
    if (copyTimerRef.current !== null) {
      clearTimeout(copyTimerRef.current);
    }
    setCopied(what);
    copyTimerRef.current = setTimeout(function clear() {
      copyTimerRef.current = null;
      if (aliveRef.current) {
        setCopied(null);
      }
    }, COPIED_TIMEOUT_MS);
  }

  function handleCopyUrl(): void {
    void copyText(connection.mcp_url).then(function done(ok: boolean) {
      if (ok && aliveRef.current) {
        flagCopied("url");
      }
    });
  }

  function handleCopyToken(): void {
    const token = minted;
    if (token === null) {
      return;
    }
    void copyText(token).then(function done(ok: boolean) {
      if (ok && aliveRef.current) {
        flagCopied("token");
      }
    });
  }

  function handleMint(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (mintState.pending) {
      return;
    }
    const wanted = label.trim();
    void mintState.run(async function create(): Promise<void> {
      const created = await mintKey(connectionId, wanted);
      if (!aliveRef.current) {
        return;
      }
      setMinted(created.token);
      setLabel("");
      await reload();
      if (onKeysChanged !== undefined) {
        onKeysChanged();
      }
    }, "Key created. Copy it now -- it is not shown again.");
  }

  function handleRevoke(): void {
    const row = pendingRevoke;
    if (row === null || revoking) {
      return;
    }
    setRevoking(true);
    setRevokeError(null);
    void revokeKey(connectionId, row.id)
      .then(async function done(): Promise<void> {
        await reload();
        if (onKeysChanged !== undefined) {
          onKeysChanged();
        }
        if (aliveRef.current) {
          setPendingRevoke(null);
        }
      })
      .catch(function fail(caught: unknown) {
        if (!aliveRef.current) {
          return;
        }
        // The dialog has no slot for a failure, so it closes and the message
        // lands on the key list behind it rather than nowhere at all.
        setPendingRevoke(null);
        setRevokeError(
          caught instanceof Error && caught.message.length > 0
            ? caught.message
            : "The key could not be revoked."
        );
      })
      .then(function settle() {
        if (aliveRef.current) {
          setRevoking(false);
        }
      });
  }

  const live =
    keys === null
      ? []
      : keys.filter(function isLive(row: McpKeyRow) {
          return row.revoked_at === null;
        });

  return (
    <div className="acct-stack">
      <SectionCard
        title="MCP endpoint"
        description="Add this URL as a custom connector in Claude, then authenticate it with one of the keys below."
      >
        <div className="conn-url">
          <code className="conn-url-text">{connection.mcp_url}</code>
          <button
            type="button"
            className="conn-copy"
            onClick={handleCopyUrl}
            aria-label="Copy the MCP URL"
          >
            {copied === "url" ? (
              <Check size={14} strokeWidth={2.2} aria-hidden="true" />
            ) : (
              <Copy size={14} strokeWidth={1.9} aria-hidden="true" />
            )}
            <span>{copied === "url" ? "Copied" : "Copy"}</span>
          </button>
        </div>
        <p className="acct-hint">
          The URL alone grants nothing. Every request must carry{" "}
          <code>Authorization: Bearer</code> and a key from this connection.
        </p>
      </SectionCard>

      {minted !== null ? (
        <section className="conn-token" role="alert">
          <p className="conn-token-title">
            <TriangleAlert size={15} strokeWidth={2} aria-hidden="true" />
            <span>Copy this key now</span>
          </p>
          <p className="conn-token-note">
            This is the only time it will ever be shown. It is not stored in
            this browser, and closing this box discards it.
          </p>
          <code className="conn-token-text">{minted}</code>
          <div className="conn-token-actions">
            <button
              type="button"
              className="conn-copy"
              onClick={handleCopyToken}
            >
              {copied === "token" ? (
                <Check size={14} strokeWidth={2.2} aria-hidden="true" />
              ) : (
                <Copy size={14} strokeWidth={1.9} aria-hidden="true" />
              )}
              <span>{copied === "token" ? "Copied" : "Copy key"}</span>
            </button>
            <button
              type="button"
              className="conn-action"
              onClick={function dismiss() {
                setMinted(null);
                setCopied(null);
              }}
            >
              I have saved it
            </button>
          </div>
        </section>
      ) : null}

      <SectionCard
        title="Keys"
        description="One key per client is worth the trouble: revoking a laptop then costs you nothing everywhere else."
      >
        <form className="conn-inline-form" onSubmit={handleMint}>
          <TextField
            id="mcp-key-label"
            label="Label"
            type="text"
            value={label}
            onChange={setLabel}
            autoComplete="off"
            required={false}
            maxLength={80}
            placeholder="Claude desktop"
            disabled={mintState.pending}
          />
          <SubmitButton
            pending={mintState.pending}
            label="Create key"
            pendingLabel="Creating"
          />
        </form>
        <FormFeedback error={mintState.error} success={mintState.success} />

        {loadError !== null ? (
          <p className="error acct-error" role="alert">
            {loadError}
          </p>
        ) : null}

        {keys === null && loadError === null ? (
          <p className="conn-loading">Loading keys…</p>
        ) : null}

        {keys !== null && keys.length === 0 ? (
          <p className="conn-none">
            No keys yet. Create one above to let a client reach this connection.
          </p>
        ) : null}

        {keys !== null && keys.length > 0 ? (
          <ul className="conn-list">
            {keys.map(function renderKey(row: McpKeyRow) {
              // Read into locals so the null checks narrow inside the JSX
              // below without a cast.
              const revokedAt = row.revoked_at;
              const lastUsedAt = row.last_used_at;
              const revoked = revokedAt !== null;
              return (
                <li className="conn-row" key={row.id}>
                  <span className="conn-row-icon" aria-hidden="true">
                    <KeyRound size={15} strokeWidth={1.8} />
                  </span>
                  <div className="conn-row-body">
                    <p className="conn-row-title">
                      {row.label.length > 0 ? row.label : "Untitled key"}
                      <code className="conn-prefix">{row.key_prefix}…</code>
                    </p>
                    <p className="conn-row-meta">
                      {revokedAt !== null
                        ? "Revoked " + formatWhen(revokedAt)
                        : lastUsedAt !== null
                          ? "Last used " + formatWhen(lastUsedAt)
                          : "Never used"}
                      {" · created " + formatWhen(row.created_at)}
                    </p>
                  </div>
                  <div className="conn-row-actions">
                    {revoked ? (
                      <span className="conn-badge">Revoked</span>
                    ) : (
                      <button
                        type="button"
                        className="conn-action conn-action-danger"
                        onClick={function askRevoke() {
                          setRevokeError(null);
                          setPendingRevoke(row);
                        }}
                      >
                        <Trash2 size={14} strokeWidth={1.9} aria-hidden="true" />
                        <span>Revoke</span>
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        ) : null}

        {revokeError !== null ? (
          <p className="error acct-error" role="alert">
            {revokeError}
          </p>
        ) : null}

        {live.length === 0 && keys !== null && keys.length > 0 ? (
          <p className="acct-hint">
            Every key here is revoked, so this endpoint currently answers
            nothing. Create a new one to bring it back.
          </p>
        ) : null}
      </SectionCard>

      <ConfirmDialog
        open={pendingRevoke !== null}
        title="Revoke this key?"
        description={
          <>
            Any client still using{" "}
            <strong>
              {pendingRevoke !== null && pendingRevoke.label.length > 0
                ? pendingRevoke.label
                : "this key"}
            </strong>{" "}
            will stop working immediately. This cannot be undone; mint a new key
            instead if you only need to rotate it.
          </>
        }
        confirmLabel="Revoke key"
        pendingLabel="Revoking"
        destructive
        pending={revoking}
        onConfirm={handleRevoke}
        onCancel={function cancelRevoke() {
          setPendingRevoke(null);
          setRevokeError(null);
        }}
      />
    </div>
  );
}
