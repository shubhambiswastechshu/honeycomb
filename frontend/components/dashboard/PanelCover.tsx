/**
 * The full-bleed band at the top of a panel: title, one line of explanation,
 * and whatever status the page wants to put beside them.
 *
 * Shared because it is now on two panels and the geometry is fiddly -- it
 * cancels .dash-main's padding through the custom properties that pane
 * declares, so a copy of it would drift from the pane's padding the first time
 * anyone changed one and not the other.
 *
 * Decorative by construction: the artwork is a CSS background, and the
 * `--data-cover` custom property is the seam where a real image takes over
 * without touching this file. That is also why the band carries no meaning of
 * its own -- everything a reader needs is the heading and the lede inside it.
 *
 * The panel that uses this must also carry `panel-wide`, or the cover reaches
 * the edge of a centred column rather than the edge of the pane.
 */

import type { ReactNode } from "react";

export interface PanelCoverProps {
  title: string;
  lede: string;
  /** Status that belongs with the heading, e.g. a count. Optional. */
  children?: ReactNode;
  /**
   * Ink scrim and inverted type instead of the cream one. The page around it
   * is unchanged -- this is a dark photograph, not a dark theme.
   */
  dark?: boolean;
}

export default function PanelCover({
  title,
  lede,
  children,
  dark,
}: PanelCoverProps) {
  return (
    <div className={dark === true ? "data-cover data-cover-dark" : "data-cover"}>
      <div className="data-cover-body">
        <h1 className="data-cover-title">{title}</h1>
        <p className="data-cover-lede">{lede}</p>
        {children !== undefined ? (
          <div className="data-cover-meta">{children}</div>
        ) : null}
      </div>
    </div>
  );
}
