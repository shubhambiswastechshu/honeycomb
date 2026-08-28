# Honeycomb — design system

Paste this whole file to another AI to get the same look. It describes the
theme only: colour, type, shape, motion, components and the loading animation.
No features, no product behaviour.

**The idea in one line:** warm honey on off-white paper. Flat — no shadows, no
gradients, no glass. Structure comes from 1px hairline borders and one amber
accent used sparingly. Light theme only, committed to on purpose.

---

## 1. Colour tokens

Put these on `:root` and never write a raw hex anywhere else.

```css
:root {
  color-scheme: light;

  /* Brand ramp — amber, warm, desaturated. */
  --amber-500: #ea9d3e;
  --amber-400: #e5ac3f;
  --amber-300: #e5bd3f;
  --amber-200: #eec33d;

  /* Neutrals. The "black" is a warm brown-olive, never #000 or a cool grey —
     this is what stops the palette reading as generic. */
  --ink:    #312f17;   /* text, headings */
  --muted:  #7a7357;   /* secondary text, icons at rest */
  --bg:     #fffdf8;   /* page — off-white with a yellow bias, never #fff */
  --border: #e3d9bf;   /* every hairline */

  --text:            var(--ink);
  --accent:          var(--amber-500);
  --accent-hover:    #d98c2d;
  --accent-contrast: var(--ink);   /* text ON amber is ink, not white */
  --error-text:      #9b3d22;      /* burnt red, still warm */
}
```

**Surfaces**, lightest to warmest:

| Use | Value |
| --- | --- |
| Page ground | `#fffdf8` (`--bg`) |
| Raised card, input field | `#ffffff` |
| Toolbars, sidebars, table headers | `#fffcf4` |
| Warm block: hover fill, notices, chips | `#fdf6e7` |

**Accent tints.** Amber is never used at full strength for a fill behind text.
Use `rgba(234, 157, 62, α)`:

| α | Use |
| --- | --- |
| `0.05` | card hover |
| `0.07` | table row hover, active editor line |
| `0.10` | list row hover |
| `0.12` | icon chip background |
| `0.14` | active nav item |
| `0.16` | selected tab, icon-button hover |
| `0.24` | text selection |

**Semantic dots** (status). Colour is never the only signal — always pair with
a `title` or a word:

```
connected  #3f7d3a      failed  #9b3d22      pending  #cbbf9c
```

---

## 2. Typography

System fonts. No webfont is loaded — that is deliberate, it keeps first paint
instant and the warm neutral palette carries the personality instead.

```css
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    Helvetica, Arial, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
```

Monospace, for code, data and identifiers:

```css
font-family: ui-monospace, "Cascadia Mono", "Segoe UI Mono", "Roboto Mono",
  monospace;
```

Scale — small and tight, this is a working tool, not a landing page:

| Role | Size | Weight | Notes |
| --- | --- | --- | --- |
| Page title | 24px | 600 | `letter-spacing: -0.01em` |
| Section heading | 18px | 600 | |
| Body | 15px | 400 | |
| UI text, buttons, inputs | 13px | 400–500 | |
| Table cells, tree rows | 12.5px | 400 | |
| Meta, captions, types | 11–11.5px | 400 | `--muted` |
| Eyebrow / overline | 11px | 600 | `uppercase`, `letter-spacing: 0.08em`, `--muted` |

Numbers that line up in columns get `font-variant-numeric: tabular-nums`.

---

## 3. Shape and spacing

Radii climb with the size of the thing:

```
input 6px · button 7px · icon button 6px · nav item 10px · card/tile 12px · pill 999px
```

- Borders are always `1px solid var(--border)`. **No box-shadows for depth** —
  shadow is used only for a floating popover (`0 10px 30px rgba(49,47,23,0.13)`).
- Spacing steps: `4 · 6 · 8 · 10 · 14 · 16 · 22 · 28 · 32`.
- Reading column caps at `~940px`; a full-bleed tool pane may ignore it.
- Layout with flex/grid + `gap`, not per-element margins.

---

## 4. Motion

Quick and small. Nothing bounces, nothing slides far.

```css
/* Hover / colour changes */  transition: … 0.16s ease;   /* 0.18s on forms */
/* Press */                   transform: translateY(1px); /* 0.12s ease */
/* Entrance */                animation: rise 0.34s cubic-bezier(0.22, 0.68, 0.35, 1) both;

@keyframes rise {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: none; }
}
```

**Two rules that matter more than the curves:**

1. **Never show a spinner for fast work.** Arm it on a timer (~450ms) and cancel
   the timer if the work finishes first. An animation that appears and vanishes
   inside a few frames reads as a glitch, not as progress.
2. **Once shown, hold it (~420ms minimum).** Otherwise work that finishes just
   past the threshold still blinks.

The same logic applies to *any* busy styling — a label swapping to "Running", a
button dimming, even the cursor changing to `progress`. If a control is disabled
merely because it is working, do not dim it and do not change the cursor: the
pointer is sitting on that control, so a 50ms change there is guaranteed to be
seen.

Always honour the preference:

```css
@media (prefers-reduced-motion: reduce) {
  .card, .error, .loading-screen { animation: none; }
  .button:active:not(:disabled)  { transform: none; }
  .cell { animation-duration: 3s; }   /* slow the loader, don't remove it */
}
```

---

## 5. The loading animation — honeycomb ripple

Seven hexagons in the classic comb cluster (one centre cell ringed by six),
rippling outward from the middle. Pure CSS, no library, no SVG.

**Markup** (rows of 2 / 3 / 2 — the `d-*` class is the ripple order, not the
position):

```html
<div class="loader" role="status" aria-label="Loading">
  <div class="hex-row"><span class="cell d-3"></span><span class="cell d-4"></span></div>
  <div class="hex-row"><span class="cell d-2"></span><span class="cell d-0"></span><span class="cell d-5"></span></div>
  <div class="hex-row"><span class="cell d-1"></span><span class="cell d-6"></span></div>
</div>
```

**CSS:**

```css
.loader {
  /* Pointy-top hexagons: height is width * 2/sqrt(3). */
  --hex-w: 15px;
  --hex-h: 17.3px;
  --hex-gap: 2px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.hex-row { display: flex; gap: var(--hex-gap); }

/* Rows interlock: each rides up into the notches of the row above. */
.hex-row + .hex-row {
  margin-top: calc(var(--hex-h) * -0.25 + var(--hex-gap) * 0.5);
}

.cell {
  width: var(--hex-w);
  height: var(--hex-h);
  clip-path: polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%);
  background-color: transparent;
  animation: 1.5s ripple ease infinite;
}

/* Ripple spreads from the centre cell outward around the ring. Each step is
   both later and a shade lighter, so the wave reads as it travels. */
.cell.d-0 { animation-delay:   0ms; --cell-color: #ea9d3e; }
.cell.d-1 { animation-delay: 120ms; --cell-color: #e8a23f; }
.cell.d-2 { animation-delay: 200ms; --cell-color: #e5ac3f; }
.cell.d-3 { animation-delay: 280ms; --cell-color: #e5b53f; }
.cell.d-4 { animation-delay: 360ms; --cell-color: #e5bd3f; }
.cell.d-5 { animation-delay: 440ms; --cell-color: #ecc23d; }
.cell.d-6 { animation-delay: 520ms; --cell-color: #eec33d; }

@keyframes ripple {
  0%   { background-color: transparent; }
  30%  { background-color: var(--cell-color); }
  60%  { background-color: transparent; }
  100% { background-color: transparent; }
}
```

**Two ways to use it.**

Full page — centred, with an optional uppercase label:

```css
.loading-screen {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 14px; min-height: 60vh;
  animation: fade 0.3s ease both;
}
.loading-label {
  margin: 0; font-size: 11px; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--muted);
}
@keyframes fade { from { opacity: 0; } to { opacity: 1; } }
```

In place — a translucent scrim **over** the stale content, never replacing it
with a blank pane (blanking first is two changes where one will do):

```css
.busy {
  position: absolute; inset: 0; z-index: 2;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 14px;
  background: rgba(255, 253, 248, 0.82);
  animation: fade 0.18s ease both;
}
```

The scrim's parent needs `position: relative`, and must not be the scrolling
element — an overlay that scrolls away from what it covers is worse than none.

---

## 6. Components

**Primary button** — amber fill, ink text:

```css
.button {
  padding: 12px 16px;
  font: inherit; font-weight: 600;
  color: var(--accent-contrast);
  background: var(--amber-500);
  border: 1px solid var(--amber-500);
  border-radius: 7px;
  cursor: pointer;
  transition: background-color 0.18s ease, border-color 0.18s ease,
    transform 0.12s ease, opacity 0.18s ease;
}
.button:hover:not(:disabled)  { background: var(--accent-hover); border-color: var(--accent-hover); }
.button:active:not(:disabled) { transform: translateY(1px); }
.button:focus-visible         { outline: none; box-shadow: 0 0 0 3px rgba(234,157,62,0.35); }
.button:disabled              { opacity: 0.65; cursor: default; }
```

**Secondary button** — same geometry, `background: var(--bg)`, `color: var(--ink)`,
`border-color: var(--border)`; on hover the border goes amber and the fill goes
`#fdf6e7`. Give a button whose label changes ("Run" → "Running") a `min-width`
so the row does not shift.

**Input:**

```css
.input {
  width: 100%; padding: 10px 12px;
  font: inherit; color: var(--text);
  background: #ffffff;
  border: 1px solid var(--border);
  border-radius: 6px; outline: none;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}
.input:hover:not(:focus):not(:disabled) { border-color: #cfc4a4; }
.input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(234,157,62,0.22); }
.input:disabled { opacity: 0.6; }
```

**Card / tile** — white on the off-white ground, hairline border, amber on hover:

```css
.tile {
  display: flex; flex-direction: column; gap: 10px;
  padding: 16px;
  border: 1px solid var(--border); border-radius: 12px;
  background: #ffffff; color: var(--ink); text-decoration: none;
  transition: border-color 0.16s ease, background-color 0.16s ease,
    transform 0.12s ease;
}
.tile:hover { border-color: var(--amber-400); background: rgba(234,157,62,0.05); }
/* Icon sits in a soft amber chip, never a solid amber square. */
.tile-icon {
  display: flex; align-items: center; justify-content: center;
  width: 34px; height: 34px; border-radius: 9px;
  background: rgba(234,157,62,0.12); color: var(--amber-500);
}
```

**Icon rail** — 60px wide, icons only, tooltips on hover. The active item gets
a tint *and* a 3px amber tab on the left edge, so the state survives greyscale:

```css
.rail-item {
  position: relative; display: flex; align-items: center; justify-content: center;
  width: 40px; height: 40px; border-radius: 10px;
  color: var(--muted);
  transition: background-color 0.16s ease, color 0.16s ease;
}
.rail-item[aria-current="page"] {
  color: var(--amber-500); background: rgba(234,157,62,0.14);
}
.rail-item[aria-current="page"]::before {
  content: ""; position: absolute; left: -10px; top: 50%;
  width: 3px; height: 20px; border-radius: 0 3px 3px 0;
  background: var(--amber-500); transform: translateY(-50%);
}
```

**Top bar** — 56px, sticky, `background: var(--bg)`, bottom hairline only.

**Tabs / pills** — no underline. Inactive is `--muted` on transparent; active is
`--ink` on `rgba(234,157,62,0.16)` with a 6px radius. Counts ride in a
`999px`-radius chip at `rgba(49,47,23,0.10)`.

**Data table** — hairline row separators only, no vertical rules, no zebra:

- header sticky, `background: #fffcf4`, **opaque** (rows scroll under it)
- header shows the column name in `--ink` 600 with its type beneath in
  `--muted` 10.5px
- row hover `rgba(234,157,62,0.07)`
- numbers right-aligned with `tabular-nums`
- `null` renders as a dimmed `NULL`, never an empty cell — "no value" and
  "empty string" are different answers
- long cells clip with the full value on `title`

**Icons** — [Lucide](https://lucide.dev), `strokeWidth={1.75}` at rest and `2`
for emphasis. 13px in dense lists, 15–16px in buttons, 19px in the rail.

---

## 7. Code surfaces

Syntax colours are drawn from the brand where possible. Amber carries keywords;
strings and numbers take the two greens, which are the only hues invented for
this purpose — highlighting needs distinctions the brand does not have, and two
new colours beats making amber mean four things.

```
keyword   #a86a12  (600 weight)      string    #3f7d3a
number    #2f6f6b                    comment   #9a9377  (italic)
function  #8a5a9e                    name      #312f17
operator  #7a7357                    invalid   #9b3d22
```

Editor chrome: transparent gutter with a right hairline, gutter numbers
`#b6ac8e`, active line `rgba(234,157,62,0.07)` as a **band not a border** (a 1px
outline shifts the text as the cursor moves), caret `--accent-hover` at 2px,
selection `rgba(234,157,62,0.24)`.

---

## 8. Voice

- Sentence case everywhere. Uppercase only for 11px eyebrows.
- Name things as a person would, not as the system does.
- Buttons say what happens: "Publish", then a toast saying "Published".
- Errors say what went wrong and what to do — no apologies, no "Oops".
- Show the underlying system's own message when it has one; a real database
  error beats "Something went wrong".

---

## 9. Don'ts

- No pure `#000`, no pure `#fff` as the page ground, no cool greys.
- No shadows for depth, no gradients, no glassmorphism, no rounded-2xl.
- No second accent colour. Semantic green/red are for status only and never
  become a brand colour.
- No amber text on an amber fill — text on accent is always `--ink`.
- Don't centre body copy; centre only empty states.
- Don't animate on every state change. Entrances and busy states only.
