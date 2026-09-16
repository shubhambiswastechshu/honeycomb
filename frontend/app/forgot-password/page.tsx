"use client";

/**
 * "I cannot get in." One field, and an answer that is the same either way.
 *
 * The confirmation never says whether the address had an account: the server
 * refuses to tell, because a different answer here is a way to find out who
 * has one. So the copy is written to be true and useful in both cases.
 */

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { AuthCard, ErrorBanner, Field } from "@/app/AuthCard";
import { ensureCsrf, requestPasswordReset } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const emailRef = useRef<HTMLInputElement | null>(null);

  useEffect(function prepare() {
    // Prime the csrftoken cookie, exactly as the sign-in page does: the POST
    // below has to carry the header.
    ensureCsrf().catch(function ignore() {
      // The submit surfaces the real failure if there is one.
    });
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await requestPasswordReset(email.trim());
      setSent(true);
    } catch (caught) {
      setError(
        caught instanceof Error && caught.message
          ? caught.message
          : "That could not be sent. Try again in a moment."
      );
      setLoading(false);
    }
  }

  if (sent) {
    return (
      <AuthCard
        title="Check your email"
        subtitle="If that address has an account, a link to choose a new password is on its way."
      >
        <p className="footnote">
          The link expires in an hour and works once. Nothing has changed until
          you use it.
        </p>
        <p className="footnote">
          Wrong address?{" "}
          <button
            type="button"
            className="link-button"
            onClick={function again() {
              setSent(false);
              setLoading(false);
            }}
          >
            Try another
          </button>
        </p>
        <p className="footnote">
          <Link href="/signin">Back to sign in</Link>
        </p>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Forgot your password?"
      subtitle="Enter your email and we will send a link to choose a new one."
    >
      {error !== null ? <ErrorBanner message={error} /> : null}
      <form onSubmit={handleSubmit}>
        <Field
          id="email"
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          placeholder="you@company.com"
          disabled={loading}
          inputRef={emailRef}
        />
        <button className="button" type="submit" disabled={loading}>
          {loading ? "Sending..." : "Send the link"}
        </button>
      </form>
      <p className="footnote">
        Remembered it? <Link href="/signin">Sign in</Link>
      </p>
    </AuthCard>
  );
}
