"use client";

/**
 * The form that turns a connector into a connection.
 *
 * There is no hand-written field list here and there must not be one: a
 * connector declares `cred_fields` and this builds an input per name. A new
 * connector therefore becomes connectable without a frontend change, which is
 * the whole reason the registry publishes the names at all.
 *
 * A connector whose `auth` is "google_oauth" takes the other branch entirely.
 * There is nothing to paste for one of those -- the credential is a refresh
 * token that only Google can issue -- so the form collapses to a single action
 * that hands the browser to Google. The connection is then created by the
 * server's callback, which is also why that branch never calls onSaved: this
 * component is gone by the time the connection exists.
 *
 * Two shapes, one component. With no `existing` it creates; with one it edits,
 * and every credential input then becomes optional -- a blank secret means
 * "leave the stored one alone" rather than "store an empty string", because
 * the server never sends credentials back and the user cannot see what is
 * already there to retype it.
 *
 * Field values live in component state and go straight into the request body.
 * They are never persisted anywhere on this side.
 */

import { useState } from "react";
import type { FormEvent } from "react";
import { createConnection, startGoogleOAuth, updateConnection } from "@/lib/api";
import type { Connection, ConnectorSpec } from "@/lib/api";
import {
  FormFeedback,
  SectionCard,
  SubmitButton,
  TextField,
  useSubmitState,
} from "@/components/dashboard/AccountForms";

/** The registry's value for a connector that is connected through Google. */
const GOOGLE_AUTH = "google_oauth";

/**
 * Words that must not be sentence-cased on their way to a label. Without this
 * `api_key` reads as "Api key", which looks like a typo in a product whose
 * users are the sort of people who own API keys.
 */
const ACRONYMS: Record<string, string> = {
  api: "API",
  url: "URL",
  uri: "URI",
  id: "ID",
  db: "DB",
  dsn: "DSN",
  sql: "SQL",
  ssl: "SSL",
  ssh: "SSH",
  http: "HTTP",
  https: "HTTPS",
  aws: "AWS",
  gcp: "GCP",
  crm: "CRM",
  jwt: "JWT",
  pat: "PAT",
  oauth: "OAuth",
  sdk: "SDK",
};

/**
 * A field whose name contains one of these is rendered masked. The test is
 * deliberately generous: showing a non-secret as dots is a small annoyance,
 * while printing a live API key onto a shared screen is not.
 */
const SECRET_HINTS = [
  "token",
  "secret",
  "password",
  "passwd",
  "key",
  "credential",
  "private",
  "signature",
];

/** "api_key" -> "API key", "base_url" -> "Base URL", "account" -> "Account". */
export function fieldLabel(name: string): string {
  const words = name.split(/[_\s-]+/).filter(function kept(word: string) {
    return word.length > 0;
  });
  if (words.length === 0) {
    return name;
  }
  const rendered = words.map(function render(word: string, index: number) {
    const lower = word.toLowerCase();
    const known = ACRONYMS[lower];
    if (known !== undefined) {
      return known;
    }
    if (index === 0) {
      return lower.charAt(0).toUpperCase() + lower.slice(1);
    }
    return lower;
  });
  return rendered.join(" ");
}

function isSecretField(name: string): boolean {
  const lower = name.toLowerCase();
  for (let index = 0; index < SECRET_HINTS.length; index += 1) {
    if (lower.indexOf(SECRET_HINTS[index]) !== -1) {
      return true;
    }
  }
  return false;
}

/** Every field starts empty, including when editing. */
function blankValues(fields: string[]): Record<string, string> {
  const values: Record<string, string> = {};
  for (let index = 0; index < fields.length; index += 1) {
    values[fields[index]] = "";
  }
  return values;
}

export interface ConnectFormProps {
  connector: ConnectorSpec;
  /** Present when re-opening an existing connection to rename or re-key it. */
  existing?: Connection | null;
  /** Handed the saved connection so the page can select and refresh it. */
  onSaved: (connection: Connection) => void;
}

export default function ConnectForm({
  connector,
  existing,
  onSaved,
}: ConnectFormProps) {
  // Normalised to a single nullable const so the narrowing survives into the
  // submit handler, which is a nested function.
  const target: Connection | null =
    existing !== undefined && existing !== null ? existing : null;
  const editing = target !== null;
  const state = useSubmitState();
  const [name, setName] = useState<string>(target !== null ? target.name : "");
  const [values, setValues] = useState<Record<string, string>>(
    blankValues(connector.cred_fields)
  );

  function setField(field: string, next: string): void {
    setValues(function merge(previous: Record<string, string>) {
      const merged: Record<string, string> = {};
      const keys = Object.keys(previous);
      for (let index = 0; index < keys.length; index += 1) {
        merged[keys[index]] = previous[keys[index]];
      }
      merged[field] = next;
      return merged;
    });
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (state.pending) {
      return;
    }

    const creds: Record<string, string> = {};
    let supplied = 0;
    for (let index = 0; index < connector.cred_fields.length; index += 1) {
      const field = connector.cred_fields[index];
      const value = values[field].trim();
      if (value.length > 0) {
        creds[field] = value;
        supplied += 1;
      }
    }

    // Creating with a partially filled form would store credentials that can
    // only fail on the first tool call, so it is refused before the network.
    if (target === null && supplied < connector.cred_fields.length) {
      state.fail("Fill in every credential field before connecting.");
      return;
    }

    const label = name.trim();

    if (target !== null) {
      void state.run(async function save(): Promise<void> {
        const body: { name?: string; creds?: Record<string, string> } = {
          name: label,
        };
        // Omitted entirely when blank: sending {} would overwrite the stored
        // credentials with nothing.
        if (supplied > 0) {
          body.creds = creds;
        }
        const saved = await updateConnection(target.id, body);
        setValues(blankValues(connector.cred_fields));
        onSaved(saved);
      }, supplied > 0 ? "Connection updated." : "Name updated.");
      return;
    }

    void state.run(async function create(): Promise<void> {
      const saved = await createConnection({
        connector: connector.slug,
        name: label,
        creds: creds,
      });
      setName("");
      setValues(blankValues(connector.cred_fields));
      onSaved(saved);
    }, "Connected.");
  }

  /**
   * Hand the browser to Google. Nothing is saved on this side: the server
   * mints a one-time state nonce, Google sends the user back to the callback,
   * and the callback is what creates the connection.
   */
  function handleGoogle(): void {
    if (state.pending) {
      return;
    }
    void state.run(async function begin(): Promise<void> {
      const started = await startGoogleOAuth(connector.slug);
      const url = started.authorize_url;
      // A start that answered 200 with no URL would otherwise leave the button
      // looking like it worked while the page sat exactly where it was.
      if (typeof url !== "string" || url.length === 0) {
        throw new Error(
          "The server did not return a Google sign-in link. Try again in a moment."
        );
      }
      // assign() rather than replace(): the connector page stays in history, so
      // backing out of Google's consent screen lands where the user started.
      window.location.assign(url);
    }, "Taking you to Google…");
  }

  if (connector.auth === GOOGLE_AUTH) {
    return (
      <SectionCard
        titleId="connect-form-title"
        title={editing ? "Reconnect " + connector.label : "Connect " + connector.label}
        description={
          editing
            ? "Signing in again replaces the Google access stored on this connection. The connection itself, its URL and its keys all stay as they are."
            : "There is nothing to paste for " +
              connector.label +
              ". Google issues the credential and this page never sees it."
        }
      >
        <div className="acct-stack">
          <p className="conn-note">
            You will pick a Google account and grant read access to{" "}
            {connector.label}, then come straight back here. Write tools stay
            switched off until you turn them on yourself.
          </p>

          <div className="conn-form-actions">
            <button
              type="button"
              className="conn-action conn-action-primary"
              onClick={handleGoogle}
              disabled={state.pending}
            >
              {state.pending ? "Opening Google" : "Continue with Google"}
            </button>
          </div>

          {/* The failure worth reading here is the server's own: when Google is
              not configured it names the exact redirect URI an admin has to
              register, so it is shown verbatim rather than summarised. */}
          <FormFeedback error={state.error} success={state.success} />
        </div>
      </SectionCard>
    );
  }

  const description = editing
    ? "Leave the credential fields blank to keep the ones already stored. Anything you type replaces them."
    : "These credentials are encrypted before they are stored and are never sent back to this page.";

  return (
    <SectionCard
      titleId="connect-form-title"
      title={editing ? "Edit connection" : "Connect " + connector.label}
      description={description}
    >
      <form className="acct-form" onSubmit={handleSubmit}>
        <TextField
          id="connect-name"
          label="Name"
          type="text"
          value={name}
          onChange={setName}
          autoComplete="off"
          required={false}
          maxLength={80}
          placeholder={connector.label}
          disabled={state.pending}
          hint="Optional. Useful once you have more than one of these connected."
        />

        {connector.cred_fields.map(function renderField(field: string) {
          const secret = isSecretField(field);
          return (
            <TextField
              key={field}
              id={"connect-cred-" + field}
              label={fieldLabel(field)}
              type={secret ? "password" : "text"}
              value={values[field]}
              onChange={function onFieldChange(next: string) {
                setField(field, next);
              }}
              // "off" rather than "new-password": a password manager offering
              // to generate a value for someone else's API key is nonsense.
              autoComplete="off"
              required={!editing}
              disabled={state.pending}
              placeholder={editing ? "Unchanged" : undefined}
            />
          );
        })}

        {connector.cred_fields.length === 0 ? (
          <p className="acct-hint">
            {connector.label} needs no credentials from you. Give the connection
            a name and it is ready to use.
          </p>
        ) : null}

        <div className="conn-form-actions">
          <SubmitButton
            pending={state.pending}
            label={editing ? "Save changes" : "Connect"}
            pendingLabel={editing ? "Saving" : "Connecting"}
          />
        </div>

        <FormFeedback error={state.error} success={state.success} />
      </form>
    </SectionCard>
  );
}
