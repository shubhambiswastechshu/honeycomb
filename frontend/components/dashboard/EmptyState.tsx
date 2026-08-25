/**
 * The one empty state every panel uses: a hexagon-framed icon, a title and a
 * single line of explanation. No counts, no samples, no placeholder rows --
 * there is no data behind these panels yet and the UI must not imply otherwise.
 */

import type { LucideIcon } from "lucide-react";

export interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description: string;
}

export default function EmptyState({
  icon: Icon,
  title,
  description,
}: EmptyStateProps) {
  return (
    <div className="empty">
      <span className="empty-icon" aria-hidden="true">
        <Icon size={22} strokeWidth={1.6} />
      </span>
      <h2 className="empty-title">{title}</h2>
      <p className="empty-text">{description}</p>
    </div>
  );
}
