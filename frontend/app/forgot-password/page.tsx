"use client";

/**
 * "I cannot get in." -- while email is still switched off.
 *
 * The reset flow itself is built and tested: /auth/password-reset/ mints the
 * link and /reset-password redeems it. What is missing is a mail server, so a
 * form here would take an address, say "check your email", and send nothing.
 * Until the credentials are in place this page says the true thing instead and
 * points at the person who can actually help.
 *
 * To turn it back on: set EMAIL_HOST, EMAIL_HOST_USER and EMAIL_HOST_PASSWORD
 * on the API, then restore the form from git history (it is the commit that
 * added this page).
 */

import Link from "next/link";
import { AuthCard } from "@/app/AuthCard";

export default function ForgotPasswordPage() {
  return (
    <AuthCard
      title="Password reset is not live yet"
      subtitle="We are still setting up email for this workspace."
    >
      <p className="footnote">
        Ask your Honeycomb admin to set a new password for you — they can do it
        in seconds. Once email is switched on, this page will send you a reset
        link instead.
      </p>
      <p className="footnote">
        <Link href="/signin">Back to sign in</Link>
      </p>
    </AuthCard>
  );
}
