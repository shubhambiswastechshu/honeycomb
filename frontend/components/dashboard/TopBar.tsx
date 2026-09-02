"use client";

/**
 * The 56px top bar: brand on the left, the search field on the true centre,
 * and the account controls on the right.
 *
 * The search input is deliberately inert for now -- it holds its own value and
 * has no submit handler and no results UI, because there is nothing to search
 * yet. It is still a real, labelled, focusable input so the shortcut and the
 * accessibility story are correct the day it gains behaviour.
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Search } from "lucide-react";
import { LogoMark } from "@/components/ui/Logo";
import SignOutButton from "@/components/dashboard/SignOutButton";
import { useSession } from "@/components/dashboard/SessionProvider";

/** True for the platforms whose modifier is actually Command. */
function usesCommandKey(): boolean {
  const nav = window.navigator;
  const platform =
    typeof nav.platform === "string" && nav.platform.length > 0
      ? nav.platform
      : nav.userAgent;
  return /Mac|iPhone|iPad|iPod/.test(platform);
}

/** First visible character of the name, falling back to the address. */
function monogram(fullName: string, email: string): string {
  const name = fullName.trim();
  if (name.length > 0) {
    return name.charAt(0).toUpperCase();
  }
  const address = email.trim();
  return address.length > 0 ? address.charAt(0).toUpperCase() : "?";
}

export default function TopBar() {
  const { session } = useSession();
  const pathname = usePathname();
  const inputRef = useRef<HTMLInputElement | null>(null);
  // The avatar shows only a letter, so the name has to reach a screen reader
  // some other way -- it is the label and the tooltip.
  const displayName =
    session.user.full_name.trim().length > 0
      ? session.user.full_name
      : session.user.email;
  const [query, setQuery] = useState<string>("");
  // The handler below accepts Meta *or* Control, so the hint has to say which
  // one this machine has. Empty until the effect runs: the server cannot know
  // the platform, and rendering a guess would either mismatch the markup on
  // hydration or show Windows users a key their keyboard does not have.
  const [shortcutHint, setShortcutHint] = useState<string>("");

  useEffect(function detectPlatform() {
    setShortcutHint(usesCommandKey() ? "⌘K" : "Ctrl K");
  }, []);

  useEffect(function bindShortcut() {
    function onKeyDown(event: KeyboardEvent): void {
      const input = inputRef.current;
      if (input === null) {
        return;
      }

      const isK = event.key === "k" || event.key === "K";
      if (isK && (event.metaKey || event.ctrlKey) && !event.altKey) {
        // The only key we take over: the browser's own Cmd/Ctrl-K would
        // otherwise jump to the address bar's search.
        event.preventDefault();
        input.focus();
        input.select();
        return;
      }

      // Escape gives focus back without cancelling anything else on the page.
      if (event.key === "Escape" && document.activeElement === input) {
        input.blur();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return function unbind() {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  return (
    <header className="dash-topbar">
      <div className="dash-brand">
        <LogoMark size={22} />
        <span className="dash-brand-text">Honeycomb</span>
      </div>

      <div className="dash-search" role="search">
        <label className="dash-visually-hidden" htmlFor="dash-search-input">
          Search Honeycomb
        </label>
        <Search
          className="dash-search-icon"
          size={15}
          strokeWidth={1.9}
          aria-hidden="true"
        />
        <input
          id="dash-search-input"
          ref={inputRef}
          className="dash-search-input"
          type="text"
          name="q"
          placeholder="Search"
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={function onChange(event) {
            setQuery(event.target.value);
          }}
        />
        {shortcutHint !== "" ? (
          <span className="dash-search-hint" aria-hidden="true">
            {shortcutHint}
          </span>
        ) : null}
      </div>

      {/* The account controls. They were in the rail's foot, which put the
          two things a person reaches for least in the column reserved for
          the things they reach for most. */}
      <div className="dash-account">
        <Link
          href="/dashboard/profile"
          className="dash-account-link"
          title={displayName + " — profile"}
          aria-label={displayName + " — profile"}
          aria-current={pathname === "/dashboard/profile" ? "page" : undefined}
        >
          <span className="dash-account-mark" aria-hidden="true">
            {monogram(session.user.full_name, session.user.email)}
          </span>
          <span className="dash-account-name">{displayName}</span>
        </Link>
        <SignOutButton />
      </div>
    </header>
  );
}
