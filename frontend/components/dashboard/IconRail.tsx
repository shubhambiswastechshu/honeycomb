"use client";

/**
 * The left rail: an icon above a short text label, one per section.
 *
 * The labels are visible rather than hidden behind a hover tooltip. An icon
 * rail only works when every glyph is unambiguous, and this product's sections
 * are not -- "Data", "Activity" and "Connectors" are three abstractions that
 * no icon distinguishes reliably, so the tooltip was carrying meaning that
 * belongs on the screen. The tooltip markup stays for the collapsed bottom-bar
 * layout under 720px, where there is no room for a label.
 *
 * The account controls are NOT here. They live in the top bar's right-hand
 * column, next to the person's own name.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  Database,
  LayoutGrid,
  Blocks,
  Settings,
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
  /* Second, directly under Overview: connecting a data source is the primary
     action of the product, and /dashboard/connectors/<slug> is a child route,
     so this entry stays lit while a single connector is open. */
  { href: "/dashboard/connectors", label: "MCPs", icon: Blocks },
  { href: "/dashboard/data", label: "Data", icon: Database },
  { href: "/dashboard/activity", label: "Activity", icon: Activity },
  { href: "/dashboard/team", label: "Team", icon: Users },
  { href: "/dashboard/settings", label: "Settings", icon: Settings },
];

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
        title={item.label}
        aria-current={active ? "page" : undefined}
      >
        {/* The box is a child rather than the link's own background: the
            label sits outside it, so the current section reads as a
            highlighted icon with a caption, not as a filled block of text. */}
        <span className="rail-box">
          <Icon size={19} strokeWidth={1.75} aria-hidden="true" />
        </span>
        <span className="rail-name">{item.label}</span>
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


    </nav>
  );
}
