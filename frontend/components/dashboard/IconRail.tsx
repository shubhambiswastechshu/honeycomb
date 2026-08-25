"use client";

/**
 * The narrow left rail: icons only, no visible text labels. Each item carries
 * an aria-label and a title, plus a CSS-only tooltip that slides out to the
 * right on pointer devices (absolutely positioned, so it never shifts layout).
 *
 * Under 720px the rail turns into a bottom bar; see dashboard.css.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  Database,
  LayoutGrid,
  Settings,
  UserRound,
  Users,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

interface RailItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /**
   * "/dashboard" is a prefix of every other route, so it only lights up on an
   * exact match. The rest also match their own children.
   */
  exact?: boolean;
}

const MAIN_ITEMS: RailItem[] = [
  { href: "/dashboard", label: "Overview", icon: LayoutGrid, exact: true },
  { href: "/dashboard/data", label: "Data", icon: Database },
  { href: "/dashboard/activity", label: "Activity", icon: Activity },
  { href: "/dashboard/team", label: "Team", icon: Users },
  { href: "/dashboard/settings", label: "Settings", icon: Settings },
];

const PROFILE_ITEM: RailItem = {
  href: "/dashboard/profile",
  label: "Profile",
  icon: UserRound,
};

function isActive(pathname: string, item: RailItem): boolean {
  if (item.exact === true) {
    return pathname === item.href;
  }
  return pathname === item.href || pathname.indexOf(item.href + "/") === 0;
}

function RailLink({ item, active }: { item: RailItem; active: boolean }) {
  const Icon = item.icon;
  return (
    <li className="rail-cell">
      <Link
        href={item.href}
        className="rail-item"
        aria-label={item.label}
        title={item.label}
        aria-current={active ? "page" : undefined}
      >
        <Icon size={19} strokeWidth={1.75} aria-hidden="true" />
        <span className="rail-tip" role="presentation">
          {item.label}
        </span>
      </Link>
    </li>
  );
}

export default function IconRail() {
  const pathname = usePathname();

  return (
    <nav className="dash-rail" aria-label="Dashboard sections">
      <ul className="rail-list">
        {MAIN_ITEMS.map(function renderItem(item) {
          return (
            <RailLink
              key={item.href}
              item={item}
              active={isActive(pathname, item)}
            />
          );
        })}
      </ul>

      <span className="rail-divider" aria-hidden="true" />

      <ul className="rail-list rail-list-foot">
        <RailLink item={PROFILE_ITEM} active={isActive(pathname, PROFILE_ITEM)} />
      </ul>
    </nav>
  );
}
