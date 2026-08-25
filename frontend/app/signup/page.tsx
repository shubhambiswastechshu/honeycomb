"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AuthCard, ErrorBanner, Field } from "@/app/AuthCard";
import LoadingScreen from "@/components/ui/LoadingScreen";
import { checkSignUpStep, ensureCsrf, signUp } from "@/lib/api";
import type { SignUpCheckPayload } from "@/lib/api";

const STEP_COUNT = 4;

const STEP_COPY = [
  { title: "What's your name?", subtitle: "So your team knows who you are." },
  { title: "Your email", subtitle: "You will sign in with this address." },
  {
    title: "Name your organization",
    subtitle: "This becomes your workspace. You can rename it later.",
  },
  { title: "Set a password", subtitle: "At least 8 characters." },
];

export default function SignUpPage() {
  const router = useRouter();

  const [step, setStep] = useState(0);
  // Drives which direction the panel animates from.
  const [goingBack, setGoingBack] = useState(false);

  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState("");
  const [organizationName, setOrganizationName] = useState("");
  const [password, setPassword] = useState("");

  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const stepRef = useRef<HTMLFormElement | null>(null);

  // Prime the csrftoken cookie before the first step check POSTs. A signed-in
  // visitor never reaches this page -- middleware sends them to /dashboard.
  useEffect(function prepare() {
    let cancelled = false;
    ensureCsrf()
      .catch(function ignore() {
        // The first submit primes it again and surfaces the real failure.
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

  // Put the cursor in the first field of whichever step just arrived, so the
  // whole flow can be typed without reaching for the mouse.
  useEffect(
    function focusFirstField() {
      if (restoring) {
        return;
      }
      const input = stepRef.current?.querySelector("input");
      if (input instanceof HTMLInputElement) {
        input.focus();
      }
    },
    [step, restoring]
  );

  function goNext() {
    setError(null);
    setGoingBack(false);
    setStep(function advance(current) {
      return Math.min(current + 1, STEP_COUNT - 1);
    });
  }

  function goBack() {
    setError(null);
    setGoingBack(true);
    setStep(function retreat(current) {
      return Math.max(current - 1, 0);
    });
  }

  /** What the server should check before this step is allowed to advance. */
  function stepCheckPayload(): SignUpCheckPayload {
    if (step === 0) {
      return { full_name: `${firstName.trim()} ${lastName.trim()}`.trim() };
    }
    if (step === 1) {
      return { email: email.trim() };
    }
    if (step === 2) {
      return { organization_name: organizationName.trim() };
    }
    return {
      password: password,
      context_email: email.trim(),
      context_full_name: `${firstName.trim()} ${lastName.trim()}`.trim(),
    };
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    // Every step validates against the server before moving on, so a taken
    // address or a rejected password is reported here and not four steps later.
    if (step < STEP_COUNT - 1) {
      setLoading(true);
      setError(null);
      try {
        await checkSignUpStep(stepCheckPayload());
        goNext();
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Please check this step.");
      } finally {
        setLoading(false);
      }
      return;
    }

    setLoading(true);
    setError(null);
    try {
      await signUp({
        organization_name: organizationName.trim(),
        full_name: `${firstName.trim()} ${lastName.trim()}`.trim(),
        email: email.trim(),
        password: password,
      });
      setPassword("");
      // The auth cookies are set; middleware will let the dashboard through.
      // Loading stays on so the button does not flick back mid-navigation.
      router.replace("/dashboard");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Sign up failed.");
      setLoading(false);
    }
  }

  if (restoring) {
    return <LoadingScreen label="Loading" />;
  }

  const copy = STEP_COPY[step];
  const isLastStep = step === STEP_COUNT - 1;

  // Every step submits the same button; only the word changes. The two loading
  // labels are not interchangeable: the last step is creating the workspace,
  // while the earlier ones are only validating the field against
  // /auth/signup/check/ -- calling that "Creating..." would claim more than has
  // happened.
  const submitLabel = isLastStep
    ? loading
      ? "Creating..."
      : "Create workspace"
    : loading
      ? "Checking..."
      : "Next";

  return (
    <AuthCard title={copy.title} subtitle={copy.subtitle} headingKey={step}>
      <StepIndicator step={step} />
      {error !== null ? <ErrorBanner message={error} /> : null}

      {/* Keyed on step so React remounts the panel and the CSS entrance replays. */}
      <form
        key={step}
        ref={stepRef}
        className="step"
        data-direction={goingBack ? "back" : "forward"}
        onSubmit={handleSubmit}
      >
        {step === 0 ? (
          <>
            <Field
              id="first_name"
              label="First name"
              type="text"
              value={firstName}
              onChange={setFirstName}
              autoComplete="given-name"
              placeholder="Ada"
              disabled={loading}
            />
            <Field
              id="last_name"
              label="Last name"
              type="text"
              value={lastName}
              onChange={setLastName}
              autoComplete="family-name"
              placeholder="Lovelace"
              disabled={loading}
            />
          </>
        ) : null}

        {step === 1 ? (
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
        ) : null}

        {step === 2 ? (
          <Field
            id="organization_name"
            label="Organization name"
            type="text"
            value={organizationName}
            onChange={setOrganizationName}
            autoComplete="organization"
            placeholder="Acme Inc"
            disabled={loading}
          />
        ) : null}

        {step === 3 ? (
          <Field
            id="password"
            label="Password"
            type="password"
            value={password}
            onChange={setPassword}
            autoComplete="new-password"
            minLength={8}
            disabled={loading}
          />
        ) : null}

        {/* One button for every step. The intermediate steps used to get a
            round icon-only arrow, which had to carry an aria-label and a title
            to say what the word says by itself, and made the last step look
            like a different control rather than the end of the same run. */}
        <button className="button" type="submit" disabled={loading}>
          {submitLabel}
        </button>
      </form>

      {step > 0 ? (
        <button
          className="link-button"
          type="button"
          onClick={goBack}
          disabled={loading}
        >
          Back
        </button>
      ) : null}

      <p className="footnote">
        Already have an account? <Link href="/signin">Sign in</Link>
      </p>
    </AuthCard>
  );
}

/** Hex pips, one per step, filling as the user moves through the flow. */
function StepIndicator({ step }: { step: number }) {
  const pips = [];
  for (let index = 0; index < STEP_COUNT; index += 1) {
    pips.push(
      <span
        key={index}
        className={index <= step ? "step-pip is-filled" : "step-pip"}
      />
    );
  }

  return (
    <div className="step-indicator">
      <div className="step-pips" aria-hidden="true">
        {pips}
      </div>
      <span className="step-count">
        Step {step + 1} of {STEP_COUNT}
      </span>
    </div>
  );
}
