"use client";

/**
 * The 56px top bar: brand on the left, one search field, nothing else.
 *
 * The search input is deliberately inert for now -- it holds its own value and
 * has no submit handler and no results UI, because there is nothing to search
 * yet. It is still a real, labelled, focusable input so the shortcut and the
 * accessibility story are correct the day it gains behaviour.
 */

import { useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import { LogoMark } from "@/components/ui/Logo";

/** True for the platforms whose modifier is actually Command. */
function usesCommandKey(): boolean {
  const nav = window.navigator;
  const platform =
    typeof nav.platform === "string" && nav.platform.length > 0
      ? nav.platform
      : nav.userAgent;
  return /Mac|iPhone|iPad|iPod/.test(platform);
}

export default function TopBar() {
  const inputRef = useRef<HTMLInputElement | null>(null);
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
    </header>
  );
}
