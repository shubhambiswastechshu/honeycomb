"use client";

/**
 * Settings panel -- the organization rather than the person.
 *
 * PATCH /tenant/ answers 403 for anyone below ADMIN, so the rename form is
 * rendered disabled with an explanation for members instead of letting them
 * type into a refusal. The slug is shown read-only next to it because renaming
 * the organization deliberately does not move the sign-in identifier.
 *
 * Everything on this page comes from GET /auth/me/. Nothing is invented.
 */

import type { FormEvent } from "react";
import { useSession } from "@/components/dashboard/SessionProvider";
import { updateTenant } from "@/lib/api";
import {
  FactList,
  FormFeedback,
  ReadOnlyField,
  SectionCard,
  SubmitButton,
  TextField,
  canManageOrganization,
  roleLabel,
  useServerBackedValue,
  useSubmitState,
} from "@/components/dashboard/AccountForms";

export default function SettingsPage() {
  const { session } = useSession();

  return (
    <div className="panel">
      <h1 className="panel-title">Settings</h1>
      <p className="panel-lede">
        Settings for {session.tenant.name}.
      </p>

      <div className="panel-body acct-stack">
        <OrganizationForm />
        <WorkspaceSummary />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Organization                                                        */
/* ------------------------------------------------------------------ */

function OrganizationForm() {
  const { session, refresh } = useSession();
  const state = useSubmitState();
  const [name, setName] = useServerBackedValue(session.tenant.name);

  const mayRename = canManageOrganization(session.user.role);
  const locked = !mayRename;

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (state.pending || locked) {
      return;
    }
    const value = name.trim();
    void state.run(async function save(): Promise<void> {
      await updateTenant({ name: value });
      // The tenant name is part of the session the whole shell reads from.
      await refresh();
    }, "Organization name updated.");
  }

  return (
    <SectionCard
      title="Organization"
      description="The display name everyone in this workspace sees."
    >
      <form className="acct-form" onSubmit={handleSubmit}>
        {locked ? (
          <p className="acct-locked" role="note">
            Only an owner or an admin can rename the organization. You are
            signed in as a {roleLabel(session.user.role).toLowerCase()}, so
            these fields are read-only.
          </p>
        ) : null}

        <TextField
          id="settings-org-name"
          label="Organization name"
          type="text"
          value={name}
          onChange={setName}
          autoComplete="organization"
          maxLength={120}
          disabled={locked || state.pending}
        />

        <ReadOnlyField
          label="Organization slug"
          value={session.tenant.slug}
          hint="The stable identifier used when you sign in to this organization. It is fixed and does not change when the name above changes."
        />

        <SubmitButton
          pending={state.pending}
          label="Save organization"
          pendingLabel="Saving"
          disabled={locked}
        />
        <FormFeedback error={state.error} success={state.success} />
      </form>
    </SectionCard>
  );
}

/* ------------------------------------------------------------------ */
/* Workspace summary                                                   */
/* ------------------------------------------------------------------ */

function WorkspaceSummary() {
  const { session } = useSession();

  return (
    <SectionCard
      title="Workspace"
      description="What this session is currently signed in as."
    >
      <FactList
        items={[
          { term: "Organization", value: session.tenant.name },
          { term: "Slug", value: session.tenant.slug },
          { term: "Your role", value: roleLabel(session.user.role) },
        ]}
      />
    </SectionCard>
  );
}
