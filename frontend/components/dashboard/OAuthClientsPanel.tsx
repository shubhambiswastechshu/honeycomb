"use client";

/**
 * The AI clients connected to one connection through the OAuth popup.
 *
 * claude.ai never sees a pasted key: it signs in through /oauth/authorize and
 * holds a token the server issued. Those tokens used to be invisible here,
 * so the only way to cut one off was to delete the whole connection. This
 * lists them, one row per client, and disconnects one without touching the
 * keys or any other client.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Unplug } from "lucide-react";
import ConfirmDialog from "@/components/dashboard/ConfirmDialog";
import { SectionCard } from "@/components/dashboard/AccountForms";
import { listAuthorizations, revokeAuthorization } from "@/lib/api";
import type { OAuthAuthorizationRow } from "@/lib/api";

function formatWhen(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
}

function messageOf(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message.length > 0 ? caught.message : fallback;
}

export default function OAuthClientsPanel({ connectionId }: { connectionId: number }) {
  const [rows, setRows] = useState<OAuthAuthorizationRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<OAuthAuthorizationRow | null>(null);
  const [revoking, setRevoking] = useState<boolean>(false);
  const aliveRef = useRef<boolean>(true);

  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  const load = useCallback(
    function load(): Promise<void> {
      return listAuthorizations(connectionId).then(function apply(next) {
        if (aliveRef.current) {
          setRows(next);
        }
      });
    },
    [connectionId]
  );

  useEffect(
    function initial() {
      setRows(null);
      setError(null);
      setPending(null);
      load().catch(function fail(caught: unknown) {
        if (aliveRef.current) {
          setError(messageOf(caught, "Connected AI clients could not be loaded."));
        }
      });
    },
    [load]
  );

  function handleRevoke(): void {
    const row = pending;
    if (row === null || revoking) {
      return;
    }
    setRevoking(true);
    setError(null);
    revokeAuthorization(connectionId, row.id)
      .then(load)
      .catch(function fail(caught: unknown) {
        if (aliveRef.current) {
          setError(messageOf(caught, "The client could not be disconnected."));
        }
      })
      .then(function settle() {
        if (aliveRef.current) {
          setRevoking(false);
          setPending(null);
        }
      });
  }

  return (
    <SectionCard
      title="Connected AI clients"
      description="Clients such as claude.ai that connected by signing in, rather than with a key."
    >
      {error !== null ? (
        <p className="error acct-error" role="alert">
          {error}
        </p>
      ) : null}
      {rows === null && error === null ? (
        <p className="conn-loading">Loading connected clients…</p>
      ) : null}
      {rows !== null && rows.length === 0 ? (
        <p className="conn-none">No AI client has connected by signing in.</p>
      ) : null}
      {rows !== null && rows.length > 0 ? (
        <ul className="conn-list">
          {rows.map(function renderRow(row: OAuthAuthorizationRow) {
            const lastUsed = row.last_used_at;
            return (
              <li className="conn-row" key={row.id}>
                <span className="conn-row-icon" aria-hidden="true">
                  <Bot size={15} strokeWidth={1.8} />
                </span>
                <div className="conn-row-body">
                  <p className="conn-row-title">{row.client_name}</p>
                  <p className="conn-row-meta">
                    {(lastUsed !== null ? "Last used " + formatWhen(lastUsed) : "Never used") +
                      " · approved " +
                      formatWhen(row.approved_at) +
                      (row.approved_by.length > 0 ? " by " + row.approved_by : "")}
                  </p>
                </div>
                <div className="conn-row-actions">
                  <button
                    type="button"
                    className="conn-action conn-action-danger"
                    onClick={function ask() {
                      setError(null);
                      setPending(row);
                    }}
                  >
                    <Unplug size={14} strokeWidth={1.9} aria-hidden="true" />
                    <span>Disconnect</span>
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}

      <ConfirmDialog
        open={pending !== null}
        title="Disconnect this client?"
        description={
          <>
            <strong>{pending !== null ? pending.client_name : "This client"}</strong> loses
            access immediately. To use it again, add the connector in that client again.
          </>
        }
        confirmLabel="Disconnect"
        pendingLabel="Disconnecting"
        destructive
        pending={revoking}
        onConfirm={handleRevoke}
        onCancel={function cancel() {
          setPending(null);
        }}
      />
    </SectionCard>
  );
}
