"use client";

/**
 * The 56px top bar: brand on the left, the search field on the true centre,
 * and the account controls on the right.
 *
 * The search itself lives in GlobalSearch, which owns its data, its results
 * panel and the Ctrl/Cmd+K shortcut.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LogoMark } from "@/components/ui/Logo";
import GlobalSearch from "@/components/dashboard/GlobalSearch";
import NotificationBell from "@/components/dashboard/NotificationBell";
import SignOutButton from "@/components/dashboard/SignOutButton";
import { useSession } from "@/components/dashboard/SessionProvider";

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
  // The avatar shows only a letter, so the name has to reach a screen reader
  // some other way -- it is the label and the tooltip.
  const displayName =
    session.user.full_name.trim().length > 0
      ? session.user.full_name
      : session.user.email;
  return (
    <header className="dash-topbar">
      <div className="dash-brand">
        <LogoMark size={22} />
        <span className="dash-brand-text">Honeycomb</span>
      </div>

      <GlobalSearch />

      {/* The account controls. They were in the rail's foot, which put the
          two things a person reaches for least in the column reserved for
          the things they reach for most. */}
      <div className="dash-account">
        {/* Left of the account, because it is about the workspace rather than
            about this person. */}
        <NotificationBell />
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
