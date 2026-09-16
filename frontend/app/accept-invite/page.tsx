"use client";

/**
 * The other end of an invitation: choose a name and a password, and you are in.
 *
 * The address is fixed by the invitation and shown rather than typed -- the
 * invitation is *for* that mailbox, and letting it be edited would let whoever
 * opened the link join under someone else's address.
 */

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AuthCard, ErrorBanner, Field } from "@/app/AuthCard";
import LoadingScreen from "@/components/ui/LoadingScreen";
import { acceptInvitation, ensureCsrf, getInvitation } from "@/lib/api";
import type { InvitationSummary } from "@/lib/api";

export default function AcceptInvitePage() {
  const router = useRouter();

  const [token, setToken] = useState<string | null>(null);
  const [invitation, setInvitation] = useState<InvitationSummary | null>(null);
  const [checking, setChecking] = useState(true);
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const passwordRef = useRef<HTMLInputElement | null>(null);

  useEffect(function readInvitation() {
    let alive = true;
    const raw = new URLSearchParams(window.location.search).get("token");
    void ensureCsrf().catch(function ignore() {
      // The submit surfaces the real failure if there is one.
    });
    if (raw === null || raw.length === 0) {
      setChecking(false);
      return;
    }
    setToken(raw);
    getInvitation(raw)
      .then(function found(summary: InvitationSummary) {
        if (alive) setInvitation(summary);
      })
      .catch(function missing() {
        // Expired, withdrawn or never real: the page says the same thing for
        // all three, because so does the server.
      })
      .then(function done() {
        if (alive) setChecking(false);
      });
    return function stop() {
      alive = false;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (token === null || loading) return;
    setLoading(true);
    setError(null);
    try {
      await acceptInvitation({ token: token, fullName: fullName.trim(), password: password });
      setPassword("");
      // Signed in by the response's cookies; straight to the workspace.
      router.replace("/dashboard");
    } catch (caught) {
      setError(
        caught instanceof Error && caught.message
          ? caught.message
          : "That invitation could not be accepted."
      );
      window.requestAnimationFrame(function backToPassword() {
        const input = passwordRef.current;
        if (input !== null) {
          input.focus();
          input.select();
        }
      });
      setLoading(false);
    }
  }

  if (checking) {
    return <LoadingScreen label="Loading" />;
  }

  if (invitation === null) {
    return (
      <AuthCard
        title="This invitation is no longer valid"
        subtitle="Invitations last seven days and can be used once."
      >
        <p className="footnote">
          Ask whoever invited you to send another, or{" "}
          <Link href="/signin">sign in</Link> if you already have an account.
        </p>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title={"Join " + invitation.organization}
      subtitle={
        (invitation.invited_by.length > 0 ? invitation.invited_by + " invited " : "You were invited as ") +
        invitation.email +
        "."
      }
    >
      {error !== null ? <ErrorBanner message={error} /> : null}
      <form onSubmit={handleSubmit}>
        <Field
          id="full_name"
          label="Your name"
          type="text"
          value={fullName}
          onChange={setFullName}
          autoComplete="name"
          placeholder="Priya Sharma"
          disabled={loading}
        />
        <Field
          id="password"
          label="Choose a password"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          minLength={8}
          disabled={loading}
          inputRef={passwordRef}
          revealable
        />
        <button className="button" type="submit" disabled={loading}>
          {loading ? "Joining..." : "Join " + invitation.organization}
        </button>
      </form>
      <p className="footnote">
        Already have an account? <Link href="/signin">Sign in</Link>
      </p>
    </AuthCard>
  );
}
