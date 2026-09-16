"use client";

/**
 * The other end of the emailed link: choose a new password.
 *
 * uid and token are read straight off the location rather than through
 * useSearchParams(), which would opt this route out of prerendering unless it
 * sat behind a Suspense boundary -- the same reason the sign-in page reads
 * ?next= that way.
 *
 * No session is created on success. Whoever opened the link has proved they
 * can read the mailbox, which is enough to set a password and not enough to
 * be handed the account; they sign in with what they just chose.
 */

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AuthCard, ErrorBanner, Field } from "@/app/AuthCard";
import LoadingScreen from "@/components/ui/LoadingScreen";
import { confirmPasswordReset, ensureCsrf } from "@/lib/api";

export default function ResetPasswordPage() {
  const router = useRouter();

  const [link, setLink] = useState<{ uid: string; token: string } | null>(null);
  const [reading, setReading] = useState(true);
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const passwordRef = useRef<HTMLInputElement | null>(null);

  useEffect(function readLink() {
    const params = new URLSearchParams(window.location.search);
    const uid = params.get("uid");
    const token = params.get("token");
    if (uid !== null && uid.length > 0 && token !== null && token.length > 0) {
      setLink({ uid: uid, token: token });
    }
    ensureCsrf()
      .catch(function ignore() {
        // The submit surfaces the real failure if there is one.
      })
      .then(function ready() {
        setReading(false);
      });
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (link === null) {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await confirmPasswordReset({
        uid: link.uid,
        token: link.token,
        newPassword: password,
      });
      setPassword("");
      setDone(true);
    } catch (caught) {
      setError(
        caught instanceof Error && caught.message
          ? caught.message
          : "That did not work. Ask for a new link and try again."
      );
      // Same idea as sign-in: the cursor goes back to the field that has to
      // change, with its text selected.
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

  if (reading) {
    return <LoadingScreen label="Loading" />;
  }

  if (link === null) {
    return (
      <AuthCard
        title="That link is not complete"
        subtitle="Reset links expire after an hour and can only be used once."
      >
        <p className="footnote">
          <Link href="/forgot-password">Ask for a new link</Link>
        </p>
      </AuthCard>
    );
  }

  if (done) {
    return (
      <AuthCard
        title="Password updated"
        subtitle="Sign in with your new password."
      >
        <button
          className="button"
          type="button"
          onClick={function toSignIn() {
            router.replace("/signin");
          }}
        >
          Go to sign in
        </button>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Choose a new password"
      subtitle="This link works once, so pick something you will keep."
    >
      {error !== null ? <ErrorBanner message={error} /> : null}
      <form onSubmit={handleSubmit}>
        <Field
          id="new_password"
          label="New password"
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
          {loading ? "Saving..." : "Save the new password"}
        </button>
      </form>
      <p className="footnote">
        <Link href="/signin">Back to sign in</Link>
      </p>
    </AuthCard>
  );
}
