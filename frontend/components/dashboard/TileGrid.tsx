/**
 * The overview's launcher: one tile per thing this workspace can do.
 *
 * Each tile is a whole-card link rather than a card containing a link, so the
 * hit area is the card -- a 260px box with a 90px target inside it is the usual
 * reason a tile grid feels imprecise. `meta` is for a live count; it stays
 * undefined until the number is actually known, because a tile that shows "0"
 * while its request is still in flight reads as "you have none" rather than as
 * "still loading".
 */

import Link from "next/link";
import type { LucideIcon } from "lucide-react";

export interface TileSpec {
  href: string;
  label: string;
  description: string;
  icon: LucideIcon;
  meta?: string;
}

export default function TileGrid({ tiles }: { tiles: TileSpec[] }) {
  return (
    <ul className="tile-grid">
      {tiles.map(function renderTile(tile) {
        const Icon = tile.icon;
        return (
          <li key={tile.href} className="tile-cell">
            <Link className="tile" href={tile.href}>
              <span className="tile-icon" aria-hidden="true">
                <Icon size={19} strokeWidth={1.75} />
              </span>
              <span className="tile-body">
                <span className="tile-label">{tile.label}</span>
                <span className="tile-text">{tile.description}</span>
              </span>
              {tile.meta !== undefined ? (
                <span className="tile-meta">{tile.meta}</span>
              ) : null}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
