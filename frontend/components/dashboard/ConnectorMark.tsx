/**
 * The visual mark for one connector: a rounded app tile, tinted with a hue
 * derived from the connector's own slug.

 * Two decisions, both deliberate.
 *
 * NOT the honeycomb hexagon. That shape is Honeycomb's own mark -- it is the
 * logo, the loader and the empty-state frame. Wearing it on every third-party
 * connector made fifteen different products look like fifteen copies of this
 * one, which is exactly why the catalogue did not read as a store. A store
 * reads as a store because its shelf is full of things that look different
 * from each other.
 *
 * A hue per slug, not a colour per connector. There is no hand-maintained
 * table of brand colours to fall out of date, and no vendor SVGs to license:
 * the slug is hashed to a hue, so a connector registered on the backend this
 * afternoon has its own stable, distinct colour this afternoon with no
 * frontend change. Saturation and lightness are fixed, which is what keeps
 * fifteen different hues looking like one designed set rather than a bag of
 * sweets -- and keeps every glyph legible on its own tint at any hue.
 *
 * There are no image files here and no dependency beyond lucide-react, which
 * the project already ships. That is deliberate: a real marketplace would reach
 * for forty vendor SVGs, each one a licensing question, a bundle cost, and a
 * file that goes stale the day the vendor rebrands. A semantic icon -- what the
 * connector *does*, not whose logo it wears -- costs nothing and never expires.
 *
 * lucide dropped its brand icons (Github, Slack, Figma) in v1, so mapping a
 * slug to "its" logo is not on the table anyway. MARKS maps a slug to the icon
 * for its kind of work; a slug with no entry is not a bug and needs no code
 * change, because it falls back to a monogram of the label. A connector
 * registered on the backend this afternoon renders correctly this afternoon.
 */

import {
  Bot,
  ChartColumn,
  Database,
  Folder,
  Globe,
  HardDrive,
  Library,
  Mail,
  Megaphone,
  MousePointerClick,
  NotebookPen,
  Pill,
  Search,
  Server,
  ShoppingCart,
  Sparkles,
  Store,
  Table2,
  TrendingUp,
  Users,
  Video,
  Waypoints,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { CSSProperties } from "react";

/**
 * slug -> icon. Keys are the connector slugs the backend registry uses; values
 * describe the *job* the connector does. Keep this alphabetical so a new entry
 * has one obvious home and two people adding connectors do not collide on the
 * same line.
 */
const MARKS: Record<string, LucideIcon> = {
  // --- registered today. Every one of these is a connector the backend
  // actually serves, so every card in the catalogue gets a glyph and none
  // falls back to a letter. Five connectors begin with "Google", and a
  // monogram rendered all five as an identical "G".
  awr: TrendingUp,
  // Waypoints, not a spider or a bug: what Forager returns is a link graph,
  // and connected nodes say that where an insect would only say "crawler".
  forager: Waypoints,
  ga4: ChartColumn,
  google_ads: Megaphone,
  google_keywords: Search,
  google_merchant: Store,
  gsc: Globe,
  linkedin_ads: Users,
  medicines: Pill,
  meta_ad_library: Library,
  meta_ads: Megaphone,
  ms_clarity: MousePointerClick,
  open_data: Database,
  universal_search: Search,
  wordpress: NotebookPen,
  youtube: Video,

  // --- not registered yet. Kept deliberately: each is a connector falcon
  // has or is likely to gain, and an entry here means the card looks right
  // the day it is ported, with no frontend change.
  bigquery: Database,
  coolify: Server,
  crawl4ai: Bot,
  drive: Folder,
  gmail: Mail,
  image_gen: Sparkles,
  salesforce_commerce: ShoppingCart,
  sheets: Table2,
  shopify: ShoppingCart,
  storage: HardDrive,
};

/**
 * Up to two letters for a connector with no mapped icon.
 *
 * Two words give their initials ("Google Drive" -> "GD"); one word gives its
 * first two characters ("Notion" -> "NO"), because a lone letter in the
 * hexagon reads as an accident rather than as a mark. Anything unusable -- an
 * empty label, a label of punctuation -- falls back to the slug, and then to a
 * single dot, so this can never render an empty tile.
 */
function monogram(label: string, slug: string): string {
  const source = label.trim().length > 0 ? label : slug;
  const words = source.split(/[\s._/-]+/).filter(function hasLetters(word) {
    return /[a-z0-9]/i.test(word);
  });

  if (words.length >= 2) {
    return (words[0].charAt(0) + words[1].charAt(0)).toUpperCase();
  }
  if (words.length === 1) {
    return words[0].slice(0, 2).toUpperCase();
  }
  return "·";
}

export interface ConnectorMarkProps {
  slug: string;
  label: string;
  /**
   * Width of the hexagon in px. Omit it and the stylesheet's own size wins --
   * that is the default on purpose, so the marketplace card gets the size
   * dashboard.css designed for it rather than a number duplicated here that
   * could drift out of step with the sheet.
   */
  size?: number;
}

/** The glyph size used when no size prop overrides the stylesheet's 34px. */
const CSS_SIZE = 34;

/**
 * A size prop is honoured with inline width/height/font-size because the size
 * is per-caller -- card, detail header, a future list row -- and a class per
 * size would be a class per caller. The 1.13 ratio reproduces the hexagon
 * proportions the sheet uses (34x39, matching .empty-icon's 50x56 and
 * .acct-monogram's 46x53), and font-size rides on the same element so the
 * monogram, which is a plain text child, scales with it. No colour is set
 * here: the amber ground and the ink belong to .mkt-mark.
 */
/**
 * How many hues the marketplace uses. Twelve, 30 degrees apart.
 *
 * The hash is NOT taken modulo 360 directly. Hashing to a raw hue put
 * `awr` at 41 and `google_keywords` at 39 -- two degrees apart, which the eye
 * reads as one colour rendered wrong rather than as two connectors. Quantising
 * means two connectors either land on exactly the same hue, which reads as a
 * deliberate family, or a clear 30 degrees apart. Near-misses are the only
 * outcome that looks like a bug, and this makes them impossible.
 */
const HUE_SLOTS = 12;

/**
 * A stable hue for a slug: same slug, same colour, forever, with no table to
 * maintain and nothing to update when a connector is added.
 *
 * FNV-1a rather than a sum of character codes, because slugs in one family
 * ("google_ads", "google_keywords") differ by only a few characters and a sum
 * would map them to adjacent slots.
 */
export function hueFor(slug: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < slug.length; i += 1) {
    hash ^= slug.charCodeAt(i);
    // The FNV prime, by shift-and-add: a plain multiply overflows into the
    // float range and loses the low bits that carry the avalanche.
    hash +=
      (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
    hash >>>= 0;
  }
  // +12 so no connector lands on pure red at 0.
  return ((hash % HUE_SLOTS) * (360 / HUE_SLOTS) + 12) % 360;
}

/**
 * Brand marks, drawn rather than downloaded.
 *
 * A connector with an entry here wears its vendor's own logo instead of a
 * semantic glyph, because for the handful of products people recognise on
 * sight, the logo IS the fastest label. Everything else keeps the icon-plus-hue
 * treatment, which is what stops the catalogue turning into a licensing
 * exercise as connectors are added.
 *
 * Inline SVG, not an image file: it stays crisp at any size, costs no request,
 * and cannot 404. Each mark is drawn in a 24x24 box so it drops into the same
 * slot the lucide icons use.
 *
 * These are third-party trademarks reproduced to identify each vendor's own
 * product in an integration list -- the ordinary nominative use an integrations
 * directory relies on. They are not recoloured, distorted or used as Honeycomb's
 * own branding, and a mark should be removed here rather than altered if a
 * vendor's guidelines ever require it.
 */
/**
 * Brand marks supplied as files, in `public/brand/`.
 *
 * Checked before BRAND_MARKS and before the icon map, so dropping a file in
 * and adding one line here is all it takes to give a connector its real logo.
 * This is the route for marks that cannot be drawn faithfully by hand -- a
 * traced approximation of a logo people know well looks broken, and a wrong
 * logo is worse than an honest generic icon.
 *
 * Sized at 128px: four times the largest slot it renders in, so it stays sharp
 * on a 2x display without shipping a 512px asset to draw a 34px tile.
 */
const BRAND_IMAGES: Record<string, string> = {
  google_ads: "/brand/google-ads.png",
  google_merchant: "/brand/google-merchant.png",
  gsc: "/brand/google-search-console.png",
  linkedin_ads: "/brand/linkedin.png",
  meta_ad_library: "/brand/meta.png",
  meta_ads: "/brand/meta.png",
  ms_clarity: "/brand/ms-clarity.png",
  universal_search: "/brand/duckduckgo.png",
  wordpress: "/brand/wordpress.png",
  youtube: "/brand/youtube.png",
};

const BRAND_MARKS: Record<string, (box: number) => JSX.Element> = {
  ga4: function GoogleAnalytics(box: number) {
    return (
      <svg width={box} height={box} viewBox="0 0 24 24" aria-hidden="true">
        {/* Three rising elements: the amber column, the orange column and the
            dot. The icon-only lockup, not the one with the wordmark -- a
            34px tile has no room for type, and the bars alone are the mark
            people recognise. */}
        <rect x="16.6" y="2.4" width="5" height="19.2" rx="2.5" fill="#F9AB00" />
        <rect x="9.6" y="8.4" width="5" height="13.2" rx="2.5" fill="#E8710A" />
        <circle cx="5.1" cy="19.1" r="2.5" fill="#E8710A" />
      </svg>
    );
  },

};

export default function ConnectorMark({
  slug,
  label,
  size,
}: ConnectorMarkProps) {
  const brandImage = BRAND_IMAGES[slug];
  const brand = BRAND_MARKS[slug];
  const Icon: LucideIcon | undefined = MARKS[slug];
  const box = typeof size === "number" ? size : CSS_SIZE;

  // The hue is the only thing set inline. Everything about the shape, the
  // tint's strength and the glyph's weight stays in dashboard.css, which
  // derives both colours from this one custom property.
  const style: CSSProperties = { ["--mark-h" as string]: String(hueFor(slug)) };
  if (typeof size === "number") {
    style.width = size + "px";
    style.height = size + "px";
    style.fontSize = Math.round(size * 0.4) + "px";
    style.borderRadius = Math.round(size * 0.28) + "px";
  }

  // A brand mark brings its own colours, so the hue-tinted tile behind it
  // would fight them: it gets a plain, quiet ground instead.
  if (brandImage !== undefined) {
    const inner = Math.round(box * 0.66);
    return (
      <span className="mkt-mark mkt-mark-brand" style={style} aria-hidden="true">
        {/* A plain img, not next/image: this is a fixed-size decorative mark
            already sized for its slot, so the loader's srcset machinery would
            add a build dependency and a layout pass for nothing. */}
        <img
          src={brandImage}
          alt=""
          width={inner}
          height={inner}
          loading="lazy"
          decoding="async"
        />
      </span>
    );
  }

  if (brand !== undefined) {
    return (
      <span className="mkt-mark mkt-mark-brand" style={style} aria-hidden="true">
        {brand(Math.round(box * 0.62))}
      </span>
    );
  }

  return (
    <span className="mkt-mark" style={style} aria-hidden="true">
      {Icon !== undefined ? (
        <Icon size={Math.round(box * 0.5)} strokeWidth={1.9} />
      ) : (
        monogram(label, slug)
      )}
    </span>
  );
}
