/**
 * Honeycomb wordmark: the same seven-hex comb cluster as the loader, drawn
 * static as SVG. Deliberately unanimated — the loader owns the motion.
 *
 * Geometry: pointy-top hexagons of circumradius 9.2 on a lattice of spacing
 * 10 * sqrt(3), so the cells interlock with a hairline of breathing room.
 */

const HEX_POINTS = "0,-9.2 -7.97,-4.6 -7.97,4.6 0,9.2 7.97,4.6 7.97,-4.6";

// Center cell first, then the ring clockwise from due east.
const CELLS: Array<{ x: number; y: number; fill: string }> = [
  { x: 0, y: 0, fill: "#ea9d3e" },
  { x: 17.32, y: 0, fill: "#e8a23f" },
  { x: 8.66, y: -15, fill: "#e5ac3f" },
  { x: -8.66, y: -15, fill: "#e5b53f" },
  { x: -17.32, y: 0, fill: "#e5bd3f" },
  { x: -8.66, y: 15, fill: "#ecc23d" },
  { x: 8.66, y: 15, fill: "#eec33d" },
];

export function LogoMark({ size = 26 }: { size?: number }) {
  return (
    <svg
      className="logo-mark"
      width={size}
      height={size}
      viewBox="-27 -26 54 52"
      role="img"
      aria-label="Honeycomb"
      focusable="false"
    >
      {CELLS.map(function renderCell(cell) {
        return (
          <polygon
            key={`${cell.x}:${cell.y}`}
            points={HEX_POINTS}
            fill={cell.fill}
            transform={`translate(${cell.x} ${cell.y})`}
          />
        );
      })}
    </svg>
  );
}

/** Mark plus wordmark, used as the page header on both auth screens. */
export default function Logo() {
  return (
    <div className="logo">
      <LogoMark />
      <span className="logo-text">Honeycomb</span>
    </div>
  );
}
