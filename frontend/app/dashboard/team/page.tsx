"use client";

/**
 * The team: who is in this workspace, who has been invited, and the form that
 * invites one more.
 *
 * A member sees the list and no controls -- the server refuses them anyway, and
 * a form that always fails is worse than no form. The invite form is deliberately
 * two fields: an address and what they may do.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { CircleAlert, Mail, UserPlus, Users } from "lucide-react";
import ConfirmDialog from "@/components/dashboard/ConfirmDialog";
import EmptyState from "@/components/dashboard/EmptyState";
import { getTeam, inviteTeammate, revokeInvitation } from "@/lib/api";
import type { Team, TeamInvitation, User } from "@/lib/api";

const ROLES = [
  { value: "MEMBER", label: "Member", hint: "Can use every connection and run crawls." },
  { value: "ADMIN", label: "Admin", hint: "Can also invite people and manage the workspace." },
];

function messageOf(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message.length > 0 ? caught.message : fallback;
}

function formatDate(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleDateString();
}

function roleLabel(role: string): string {
  const found = ROLES.find((entry) => entry.value === role);
  return found ? found.label : role.charAt(0) + role.slice(1).toLowerCase();
}

export default function TeamPage() {
  const [team, setTeam] = useState<Team | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState("MEMBER");
  const [inviting, setInviting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const [pendingRevoke, setPendingRevoke] = useState<TeamInvitation | null>(null);
  const [revoking, setRevoking] = useState(false);

  const aliveRef = useRef(true);
  useEffect(function trackMounted() {
    aliveRef.current = true;
    return function unmount() {
      aliveRef.current = false;
    };
  }, []);

  const reload = useCallback(async function reload() {
    try {
      const next = await getTeam();
      if (aliveRef.current) {
        setTeam(next);
        setLoadError(null);
      }
    } catch (caught) {
      if (aliveRef.current) setLoadError(messageOf(caught, "The team could not be loaded."));
    }
  }, []);

  useEffect(
    function load() {
      void reload();
    },
    [reload]
  );

  async function handleInvite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inviting) return;
    setInviting(true);
    setFormError(null);
    setNote(null);
    try {
      const invitation = await inviteTeammate(email.trim(), role);
      setEmail("");
      setNote("Invitation sent to " + invitation.email + ".");
      await reload();
    } catch (caught) {
      setFormError(messageOf(caught, "That invitation could not be sent."));
    } finally {
      if (aliveRef.current) setInviting(false);
    }
  }

  async function handleRevoke() {
    const target = pendingRevoke;
    if (target === null || revoking) return;
    setRevoking(true);
    try {
      await revokeInvitation(target.id);
      setNote("Invitation to " + target.email + " withdrawn.");
      await reload();
    } catch (caught) {
      setLoadError(messageOf(caught, "That invitation could not be withdrawn."));
    } finally {
      if (aliveRef.current) {
        setRevoking(false);
        setPendingRevoke(null);
      }
    }
  }

  const members: User[] = team !== null ? team.members : [];
  const invitations: TeamInvitation[] = team !== null ? team.invitations : [];
  const canManage = team !== null && team.can_manage;

  return (
    <div className="panel">
      <h1 className="panel-title">Team</h1>
      <p className="panel-lede">People who can reach this workspace.</p>

      <div className="panel-body acct-stack">
        {loadError !== null ? (
          <p className="error acct-error" role="alert">
            {loadError}
          </p>
        ) : null}
        {note !== null ? (
          <p className="conn-note" role="status">
            {note}
          </p>
        ) : null}

        {canManage ? (
          <form className="team-invite" onSubmit={handleInvite}>
            <div className="field team-invite-email">
              <label className="label" htmlFor="invite_email">
                Invite someone
              </label>
              <input
                className="input"
                id="invite_email"
                type="email"
                value={email}
                placeholder="them@company.com"
                autoComplete="off"
                disabled={inviting}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="field team-invite-role">
              <label className="label" htmlFor="invite_role">
                As
              </label>
              <select
                className="input"
                id="invite_role"
                value={role}
                disabled={inviting}
                onChange={(e) => setRole(e.target.value)}
              >
                {ROLES.map((entry) => (
                  <option key={entry.value} value={entry.value}>
                    {entry.label}
                  </option>
                ))}
              </select>
            </div>
            <button type="submit" className="conn-action conn-action-primary" disabled={inviting}>
              <UserPlus size={15} strokeWidth={2} aria-hidden="true" />
              <span>{inviting ? "Sending" : "Send invitation"}</span>
            </button>
            <p className="team-invite-hint">
              {ROLES.find((entry) => entry.value === role)?.hint}
            </p>
            {formError !== null ? (
              <p className="error team-invite-error" role="alert">
                {formError}
              </p>
            ) : null}
          </form>
        ) : null}

        {team === null && loadError === null ? <p className="conn-loading">Loading…</p> : null}

        {team !== null ? (
          <>
            <section className="acct-stack">
              <h2 className="team-heading">
                {members.length === 1 ? "1 person" : members.length + " people"}
              </h2>
              <ul className="conn-list conn-list-framed">
                {members.map(function renderMember(person) {
                  return (
                    <li className="conn-row" key={person.id}>
                      <span className="conn-row-icon" aria-hidden="true">
                        <Users size={15} strokeWidth={1.8} />
                      </span>
                      <div className="conn-row-body">
                        <p className="conn-row-title">
                          {person.full_name.length > 0 ? person.full_name : person.email}
                          <span className="conn-badge">{roleLabel(person.role)}</span>
                        </p>
                        {person.full_name.length > 0 ? (
                          <p className="conn-row-meta">{person.email}</p>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </section>

            <section className="acct-stack">
              <h2 className="team-heading">Invitations</h2>
              {invitations.length === 0 ? (
                <EmptyState
                  icon={Mail}
                  title="No invitations outstanding"
                  description={
                    canManage
                      ? "Invite someone above and their invitation appears here until they accept it."
                      : "An owner or admin can invite people to this workspace."
                  }
                />
              ) : (
                <ul className="conn-list conn-list-framed">
                  {invitations.map(function renderInvitation(invitation) {
                    return (
                      <li className="conn-row" key={invitation.id}>
                        <span className="conn-row-icon" aria-hidden="true">
                          <Mail size={15} strokeWidth={1.8} />
                        </span>
                        <div className="conn-row-body">
                          <p className="conn-row-title">
                            {invitation.email}
                            <span className="conn-badge">{roleLabel(invitation.role)}</span>
                          </p>
                          <p className="conn-row-meta">
                            Invited
                            {invitation.invited_by.length > 0 ? " by " + invitation.invited_by : ""}
                            {" · expires " + formatDate(invitation.expires_at)}
                          </p>
                        </div>
                        {canManage ? (
                          <div className="conn-row-actions">
                            <button
                              type="button"
                              className="conn-action conn-action-danger"
                              onClick={() => setPendingRevoke(invitation)}
                            >
                              <CircleAlert size={14} strokeWidth={1.9} aria-hidden="true" />
                              <span>Withdraw</span>
                            </button>
                          </div>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          </>
        ) : null}
      </div>

      <ConfirmDialog
        open={pendingRevoke !== null}
        title="Withdraw this invitation?"
        description={
          <>
            The link sent to <strong>{pendingRevoke !== null ? pendingRevoke.email : ""}</strong>{" "}
            stops working. You can invite them again at any time.
          </>
        }
        confirmLabel="Withdraw"
        pendingLabel="Withdrawing"
        destructive
        pending={revoking}
        onConfirm={handleRevoke}
        onCancel={() => setPendingRevoke(null)}
      />
    </div>
  );
}
