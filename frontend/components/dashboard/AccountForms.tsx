"use client";

/**
 * The pieces the Profile and Settings panels share.
 *
 * Both panels are stacks of small, independent forms: one section card each,
 * each with its own pending flag, its own error and its own success note. That
 * independence is the whole point -- a failed password change must not disable
 * the name field two cards up -- so the state lives in useSubmitState() and is
 * instantiated once per form rather than once per page.
 *
 * Styling reuses the auth-screen classes from globals.css (.field, .label,
 * .input, .button, .error) wherever they fit; only genuinely new structures
 * (the section card, the success note, the fact list) get new class names,
 * which live in the appended block at the end of app/dashboard/dashboard.css.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
import { Check, Loader2 } from "lucide-react";

/** How long a success confirmation stays on screen before it clears itself. */
const SUCCESS_TIMEOUT_MS = 3200;

const UNKNOWN_ERROR = "Something went wrong. Please try again.";

/**
 * lib/api.ts turns {"current_password": ["Current password is incorrect."]}
 * into "current password: Current password is incorrect." -- it only strips
 * that prefix inside checkSignUpStep(), and only for the four signup fields.
 * Every field these panels own is labelled right above the message, so the
 * prefix is pure repetition. The sentence after it is the server's, untouched.
 */
const FIELD_PREFIXES = [
  "current password: ",
  "new password: ",
  "new email: ",
  "full name: ",
  "name: ",
];

function cleanMessage(message: string): string {
  const lowered = message.toLowerCase();
  for (let index = 0; index < FIELD_PREFIXES.length; index += 1) {
    const prefix = FIELD_PREFIXES[index];
    if (lowered.indexOf(prefix) === 0) {
      return message.slice(prefix.length);
    }
  }
  return message;
}

/* ------------------------------------------------------------------ */
/* Per-form submit state                                               */
/* ------------------------------------------------------------------ */

export interface SubmitState {
  /** True from the moment the request leaves until it settles. */
  pending: boolean;
  /** The server's message, verbatim, or a client-side validation message. */
  error: string | null;
  /** Confirmation text; clears itself after roughly three seconds. */
  success: string | null;
  /** Reject before the network: used for the client-only confirm-password rule. */
  fail: (message: string) => void;
  /** Runs one submit, owning pending/error/success for its whole lifetime. */
  run: (task: () => Promise<void>, successMessage: string) => Promise<void>;
}

export function useSubmitState(): SubmitState {
  const [pending, setPending] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const aliveRef = useRef<boolean>(true);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  const cancelExpiry = useCallback(function cancelExpiry(): void {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const fail = useCallback(
    function fail(message: string): void {
      cancelExpiry();
      setSuccess(null);
      setError(message);
    },
    [cancelExpiry]
  );

  const run = useCallback(
    async function run(
      task: () => Promise<void>,
      successMessage: string
    ): Promise<void> {
      cancelExpiry();
      setError(null);
      setSuccess(null);
      setPending(true);
      try {
        await task();
        if (!aliveRef.current) {
          return;
        }
        setSuccess(successMessage);
        timerRef.current = setTimeout(function expire() {
          timerRef.current = null;
          if (aliveRef.current) {
            setSuccess(null);
          }
        }, SUCCESS_TIMEOUT_MS);
      } catch (caught) {
        if (!aliveRef.current) {
          return;
        }
        const message =
          caught instanceof Error && caught.message.length > 0
            ? cleanMessage(caught.message)
            : UNKNOWN_ERROR;
        setError(message);
      } finally {
        if (aliveRef.current) {
          setPending(false);
        }
      }
    },
    [cancelExpiry]
  );

  return {
    pending: pending,
    error: error,
    success: success,
    fail: fail,
    run: run,
  };
}

/* ------------------------------------------------------------------ */
/* Layout pieces                                                       */
/* ------------------------------------------------------------------ */

export interface SectionCardProps {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  /**
   * id for the heading, so a dialog wrapping this card can name itself with
   * aria-labelledby instead of repeating the title in an aria-label.
   */
  titleId?: string;
}

/** One bordered block: heading, optional explanation, then whatever it holds. */
export function SectionCard({
  title,
  description,
  children,
  titleId,
}: SectionCardProps) {
  return (
    <section className="acct-card">
      <div className="acct-card-head">
        <h2 className="acct-card-title" id={titleId}>
          {title}
        </h2>
        {description !== undefined && description !== null ? (
          <p className="acct-card-text">{description}</p>
        ) : null}
      </div>
      {children}
    </section>
  );
}

export interface TextFieldProps {
  id: string;
  label: string;
  type: "text" | "email" | "password";
  value: string;
  onChange: (value: string) => void;
  /** Never omitted: password managers need the right token on every field. */
  autoComplete: string;
  disabled?: boolean;
  required?: boolean;
  placeholder?: string;
  maxLength?: number;
  /** Rendered under the input and wired up with aria-describedby. */
  hint?: ReactNode;
}

export function TextField({
  id,
  label,
  type,
  value,
  onChange,
  autoComplete,
  disabled,
  required,
  placeholder,
  maxLength,
  hint,
}: TextFieldProps) {
  const hintId = id + "-hint";
  const hasHint = hint !== undefined && hint !== null;

  function handleChange(event: ChangeEvent<HTMLInputElement>): void {
    onChange(event.target.value);
  }

  return (
    <div className="field">
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        name={id}
        className="input"
        type={type}
        value={value}
        onChange={handleChange}
        autoComplete={autoComplete}
        disabled={disabled === true}
        required={required !== false}
        placeholder={placeholder}
        maxLength={maxLength}
        aria-describedby={hasHint ? hintId : undefined}
      />
      {hasHint ? (
        <p className="acct-hint" id={hintId}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/** A value the user cannot change, shown with the same label treatment. */
export function ReadOnlyField({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: ReactNode;
}) {
  return (
    <div className="field">
      <span className="label">{label}</span>
      <p className="acct-readonly">{value}</p>
      {hint !== undefined && hint !== null ? (
        <p className="acct-hint">{hint}</p>
      ) : null}
    </div>
  );
}

export interface FormFeedbackProps {
  error: string | null;
  success: string | null;
}

/**
 * Both messages for one form. The success paragraph is always in the DOM so
 * the polite live region exists before its text changes -- a region mounted
 * together with its content is not reliably announced.
 */
export function FormFeedback({ error, success }: FormFeedbackProps) {
  return (
    <div className="acct-feedback">
      {error !== null ? (
        <p className="error acct-error" role="alert">
          {error}
        </p>
      ) : null}
      <p className="acct-note" aria-live="polite">
        {success !== null ? (
          <>
            <Check size={14} strokeWidth={2.2} aria-hidden="true" />
            <span>{success}</span>
          </>
        ) : null}
      </p>
    </div>
  );
}

export interface SubmitButtonProps {
  pending: boolean;
  label: string;
  pendingLabel: string;
  disabled?: boolean;
}

export function SubmitButton({
  pending,
  label,
  pendingLabel,
  disabled,
}: SubmitButtonProps) {
  return (
    <div className="acct-actions">
      <button
        type="submit"
        className="button acct-button"
        disabled={pending || disabled === true}
      >
        {pending ? (
          <>
            <Loader2
              className="acct-button-spinner"
              size={15}
              strokeWidth={2.2}
              aria-hidden="true"
            />
            <span>{pendingLabel}</span>
          </>
        ) : (
          label
        )}
      </button>
    </div>
  );
}

export interface Fact {
  term: string;
  value: string;
}

/** Read-only summary rows. Only ever fed values the session actually carries. */
export function FactList({ items }: { items: Fact[] }) {
  return (
    <dl className="acct-facts">
      {items.map(function renderFact(item: Fact) {
        return (
          <div className="acct-fact" key={item.term}>
            <dt className="acct-fact-term">{item.term}</dt>
            <dd className="acct-fact-value">{item.value}</dd>
          </div>
        );
      })}
    </dl>
  );
}

/* ------------------------------------------------------------------ */
/* Shared helpers                                                      */
/* ------------------------------------------------------------------ */

const ROLE_LABELS: Record<string, string> = {
  OWNER: "Owner",
  ADMIN: "Admin",
  MEMBER: "Member",
};

/** "OWNER" -> "Owner". Anything unrecognised is shown exactly as it arrived. */
export function roleLabel(role: string): string {
  const known = ROLE_LABELS[role];
  return known !== undefined ? known : role;
}

/** Only these two may rename the organization; the server returns 403 otherwise. */
export function canManageOrganization(role: string): boolean {
  return role === "OWNER" || role === "ADMIN";
}

/**
 * Keeps a text input in step with the server's value without stealing what the
 * user is typing: the field is only overwritten when the value coming from the
 * session actually changed (i.e. a save landed and the session was re-read).
 */
export function useServerBackedValue(
  serverValue: string
): [string, (next: string) => void] {
  const [value, setValue] = useState<string>(serverValue);
  const lastServerRef = useRef<string>(serverValue);

  useEffect(
    function adoptServerValue() {
      if (lastServerRef.current !== serverValue) {
        lastServerRef.current = serverValue;
        setValue(serverValue);
      }
    },
    [serverValue]
  );

  return [value, setValue];
}
