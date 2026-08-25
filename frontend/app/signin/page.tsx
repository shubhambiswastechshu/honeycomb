"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AuthCard,
  ErrorBanner,
  Field,
  OrganizationField,
} from "@/app/AuthCard";
import LoadingScreen from "@/components/ui/LoadingScreen";
import { AmbiguousOrganizationError, ensureCsrf, signIn } from "@/lib/api";
import type { OrganizationChoice } from "@/lib/api";

const DEFAULT_DESTINATION = "/dashboard";

/**
 * Open-redirect guard for the ?next= parameter the middleware adds.
 *
 * The value ends up in router.replace(), so anything the browser would treat
 * as another origin has to be rejected. Hand-inspecting the first characters
 * is not enough: URL parsing *strips* tab, LF and CR before deciding what the
 * string means, so "/\t//evil.example" passes a naive charAt() guard and then
 * normalises to "///evil.example" -- a protocol-relative URL whose origin is
 * evil.example, which Next treats as external and navigates to hard.
 *
 * So: reject C0 control characters outright, keep the cheap shape check, and
 * then let the URL parser have the final word by comparing the resolved
 * origin. The authority is what actually decides where the browser goes.
 */
function safeNextPath(raw: string | null): string {
  if (raw === null || raw.length === 0) {
    return DEFAULT_DESTINATION;
  }
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f]/.test(raw)) {
    return DEFAULT_DESTINATION;
  }
  if (raw.charAt(0) !== "/") {
    return DEFAULT_DESTINATION;
  }
  if (raw.charAt(1) === "/" || raw.charAt(1) === "\\") {
    return DEFAULT_DESTINATION;
  }
  const origin = window.location.origin;
  let resolved: URL;
  try {
    resolved = new URL(raw, origin);
  } catch (invalid) {
    return DEFAULT_DESTINATION;
  }
  if (resolved.origin !== origin) {
    return DEFAULT_DESTINATION;
  }
  // Re-serialised from the parsed URL, never from `raw`, so what we hand to
  // the router is exactly what the parser agreed was same-origin.
  return resolved.pathname + resolved.search + resolved.hash;
}

export default function SignInPage() {
  const router = useRouter();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [organizations, setOrganizations] = useState<OrganizationChoice[] | null>(
    null
  );
  const [organizationSlug, setOrganizationSlug] = useState("");
  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const destinationRef = useRef(DEFAULT_DESTINATION);

  // Read ?next= from the location rather than useSearchParams(), which would
  // opt the whole page out of prerendering unless it sat behind a Suspense
  // boundary. Then prime the csrftoken cookie so the first POST has a header
  // to send. A signed-in visitor never gets this far -- middleware redirects
  // them to /dashboard before the page renders.
  useEffect(function prepare() {
    let cancelled = false;
    const params = new URLSearchParams(window.location.search);
    destinationRef.current = safeNextPath(params.get("next"));
    ensureCsrf()
      .catch(function ignore() {
        // The submit below primes it again and surfaces the real failure.
      })
      .then(function done() {
        if (!cancelled) {
          setRestoring(false);
        }
      });
    return function cleanup() {
      cancelled = true;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await signIn({
        email: email,
        password: password,
        organization_slug: organizations !== null ? organizationSlug : undefined,
      });
      setPassword("");
      // The auth cookies are set; middleware will let the dashboard through.
      // Loading stays on so the button does not flick back mid-navigation.
      router.replace(destinationRef.current);
    } catch (caught) {
      if (caught instanceof AmbiguousOrganizationError) {
        // The password was right, but in more than one organization. Show the
        // picker and let the user resubmit with a choice.
        setOrganizations(caught.organizations);
        setOrganizationSlug(caught.organizations[0].slug);
        // Not caught.message: the API detail ends "Retry with
        // organization_slug.", which is right for an API consumer and
        // meaningless next to the select this branch is about to render.
        setError(
          "This email is used in more than one organization. Choose which one to sign in to."
        );
      } else {
        setError(caught instanceof Error ? caught.message : "Sign in failed.");
      }
      setLoading(false);
    }
  }

  if (restoring) {
    return <LoadingScreen label="Loading" />;
  }

  return (
    <AuthCard title="Sign in" subtitle="Access your Honeycomb workspace.">
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
        />
        <Field
          id="password"
          label="Password"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
          disabled={loading}
        />
        {organizations !== null ? (
          <OrganizationField
            id="organization_slug"
            label="Organization"
            value={organizationSlug}
            onChange={setOrganizationSlug}
            options={organizations}
            disabled={loading}
          />
        ) : null}
        <button className="button" type="submit" disabled={loading}>
          {loading ? "Signing in..." : "Sign in"}
        </button>
      </form>
      <p className="footnote">
        Need an account? <Link href="/signup">Create one</Link>
      </p>
    </AuthCard>
  );
}
