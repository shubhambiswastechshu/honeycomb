"use client";

/**
 * Profile panel -- everything about the signed-in person that they are allowed
 * to change themselves.
 *
 * Three forms, each an independent <form> with its own pending flag, error and
 * success note, so one failure never disables or blanks the others. Name and
 * email changes end with useSession().refresh() so the identity card, the top
 * bar and the overview all pick up the new value from the server rather than
 * from an optimistic guess.
 *
 * The forms are behind tabs rather than stacked: they are three unrelated
 * errands, and only one is ever being run. Each panel is unmounted when its
 * tab is not current, so switching away from a half-typed password change
 * discards it instead of leaving credentials in memory behind a hidden panel.
 *
 * Signing out is not here -- it lives in the rail, where it is reachable from
 * every screen.
 *
 * Nothing here is invented: every value shown comes from GET /auth/me/.
 */

import { useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { KeyRound, Mail, UserRound } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useSession } from "@/components/dashboard/SessionProvider";
import { changeEmail, changePassword, updateProfile } from "@/lib/api";
import type { Session } from "@/lib/api";
import {
  FormFeedback,
  SectionCard,
  SubmitButton,
  TextField,
  roleLabel,
  useServerBackedValue,
  useSubmitState,
} from "@/components/dashboard/AccountForms";

interface TabSpec {
  id: string;
  label: string;
  icon: LucideIcon;
  render: () => JSX.Element;
}

const TABS: TabSpec[] = [
  {
    id: "details",
    label: "Details",
    icon: UserRound,
    render: function renderDetails() {
      return <NameForm />;
    },
  },
  {
    id: "email",
    label: "Email",
    icon: Mail,
    render: function renderEmail() {
      return <EmailForm />;
    },
  },
  {
    id: "password",
    label: "Password",
    icon: KeyRound,
    render: function renderPassword() {
      return <PasswordForm />;
    },
  },
];

export default function ProfilePage() {
  const { session } = useSession();
  const [current, setCurrent] = useState<string>(TABS[0].id);
  // Arrow keys move focus as well as selection, which means the newly current
  // tab has to be told to take focus after the render that selects it.
  const listRef = useRef<HTMLDivElement | null>(null);

  function selectByIndex(index: number): void {
    const next = TABS[(index + TABS.length) % TABS.length];
    setCurrent(next.id);
    const list = listRef.current;
    if (list !== null) {
      const button = list.querySelector<HTMLButtonElement>(
        "#profile-tab-" + next.id
      );
      if (button !== null) {
        button.focus();
      }
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const index = TABS.findIndex(function isCurrent(tab) {
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

  const active =
    TABS.find(function isCurrent(tab) {
      return tab.id === current;
    }) ?? TABS[0];

  return (
    <div className="panel">
      <h1 className="panel-title">Profile</h1>
      <p className="panel-lede">
        Your account details. Changes take effect immediately.
      </p>

      <div className="panel-body acct-stack panel-form">
        <IdentityCard session={session} />

        <div className="acct-tabs">
          <div
            className="acct-tablist"
            role="tablist"
            aria-label="Profile sections"
            ref={listRef}
            onKeyDown={onKeyDown}
          >
            {TABS.map(function renderTab(tab) {
              const Icon = tab.icon;
              const isCurrent = tab.id === current;
              return (
                <button
                  key={tab.id}
                  type="button"
                  id={"profile-tab-" + tab.id}
                  className="acct-tab"
                  role="tab"
                  aria-selected={isCurrent}
                  aria-controls={"profile-panel-" + tab.id}
                  // Roving tabindex: one Tab reaches the strip, arrows move
                  // within it. Three separate tab stops would be wrong.
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
            id={"profile-panel-" + active.id}
            role="tabpanel"
            aria-labelledby={"profile-tab-" + active.id}
            tabIndex={-1}
          >
            {active.render()}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Identity                                                            */
/* ------------------------------------------------------------------ */

/** First visible character of the name, falling back to the address. */
function monogram(fullName: string, email: string): string {
  const name = fullName.trim();
  if (name.length > 0) {
    return name.charAt(0).toUpperCase();
  }
  const address = email.trim();
  if (address.length > 0) {
    return address.charAt(0).toUpperCase();
  }
  return "?";
}

function IdentityCard({ session }: { session: Session }) {
  const { user, tenant } = session;

  return (
    <section className="acct-identity">
      <span className="acct-monogram" aria-hidden="true">
        {monogram(user.full_name, user.email)}
      </span>
      <div className="acct-identity-body">
        <p className="acct-identity-name">
          {user.full_name.trim().length > 0 ? user.full_name : user.email}
        </p>
        <p className="acct-identity-meta">{user.email}</p>
        <p className="acct-identity-meta">{tenant.name}</p>
      </div>
      <span className="acct-badge">{roleLabel(user.role)}</span>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Name                                                                */
/* ------------------------------------------------------------------ */

function NameForm() {
  const { session, refresh } = useSession();
  const state = useSubmitState();
  const [fullName, setFullName] = useServerBackedValue(session.user.full_name);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (state.pending) {
      return;
    }
    const value = fullName.trim();
    void state.run(async function save(): Promise<void> {
      await updateProfile({ full_name: value });
      // Re-read the identity so the rest of the dashboard sees the new name.
      await refresh();
    }, "Name updated.");
  }

  return (
    <SectionCard
      title="Your name"
      description="How you appear to everyone else in this organization."
    >
      <form className="acct-form" onSubmit={handleSubmit}>
        <TextField
          id="profile-full-name"
          label="Full name"
          type="text"
          value={fullName}
          onChange={setFullName}
          autoComplete="name"
          maxLength={150}
          disabled={state.pending}
        />
        <SubmitButton
          pending={state.pending}
          label="Save name"
          pendingLabel="Saving"
        />
        <FormFeedback error={state.error} success={state.success} />
      </form>
    </SectionCard>
  );
}

/* ------------------------------------------------------------------ */
/* Email                                                               */
/* ------------------------------------------------------------------ */

function EmailForm() {
  const { session, refresh } = useSession();
  const state = useSubmitState();
  const [newEmail, setNewEmail] = useState<string>("");
  const [currentPassword, setCurrentPassword] = useState<string>("");

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (state.pending) {
      return;
    }
    const address = newEmail.trim();
    const password = currentPassword;
    void state.run(async function save(): Promise<void> {
      await changeEmail({ new_email: address, current_password: password });
      setNewEmail("");
      setCurrentPassword("");
      await refresh();
    }, "Email address updated.");
  }

  return (
    <SectionCard
      title="Email address"
      description={
        "You sign in with this address. It is currently " +
        session.user.email +
        "."
      }
    >
      <form className="acct-form" onSubmit={handleSubmit}>
        <TextField
          id="profile-new-email"
          label="New email address"
          type="email"
          value={newEmail}
          onChange={setNewEmail}
          autoComplete="email"
          placeholder="you@example.com"
          disabled={state.pending}
        />
        <TextField
          id="profile-email-password"
          label="Current password"
          type="password"
          value={currentPassword}
          onChange={setCurrentPassword}
          autoComplete="current-password"
          disabled={state.pending}
          hint="Your password is required because this address is how you sign in: confirming it stops anyone who finds your screen unattended from taking the account over."
        />
        <SubmitButton
          pending={state.pending}
          label="Change email"
          pendingLabel="Changing"
        />
        <FormFeedback error={state.error} success={state.success} />
      </form>
    </SectionCard>
  );
}

/* ------------------------------------------------------------------ */
/* Password                                                            */
/* ------------------------------------------------------------------ */

const MISMATCH_MESSAGE = "The two new passwords do not match.";

function PasswordForm() {
  const state = useSubmitState();
  const [currentPassword, setCurrentPassword] = useState<string>("");
  const [newPassword, setNewPassword] = useState<string>("");
  const [confirmPassword, setConfirmPassword] = useState<string>("");

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (state.pending) {
      return;
    }
    // The server has no confirm field, so this check is ours alone and must
    // never reach the network.
    if (newPassword !== confirmPassword) {
      state.fail(MISMATCH_MESSAGE);
      return;
    }
    const current = currentPassword;
    const next = newPassword;
    void state.run(async function save(): Promise<void> {
      await changePassword({ current_password: current, new_password: next });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    }, "Password changed. You are still signed in on this device.");
  }

  return (
    <SectionCard
      title="Password"
      description="Choose something you do not use anywhere else."
    >
      <form className="acct-form" onSubmit={handleSubmit}>
        <TextField
          id="profile-current-password"
          label="Current password"
          type="password"
          value={currentPassword}
          onChange={setCurrentPassword}
          autoComplete="current-password"
          disabled={state.pending}
        />
        <TextField
          id="profile-new-password"
          label="New password"
          type="password"
          value={newPassword}
          onChange={setNewPassword}
          autoComplete="new-password"
          disabled={state.pending}
        />
        <TextField
          id="profile-confirm-password"
          label="Confirm new password"
          type="password"
          value={confirmPassword}
          onChange={setConfirmPassword}
          autoComplete="new-password"
          disabled={state.pending}
          hint="Checked here in the browser; the two entries must match before anything is sent."
        />
        <SubmitButton
          pending={state.pending}
          label="Change password"
          pendingLabel="Changing"
        />
        <FormFeedback error={state.error} success={state.success} />
      </form>
    </SectionCard>
  );
}
