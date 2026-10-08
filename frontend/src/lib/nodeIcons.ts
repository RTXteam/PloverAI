// one outline icon per kind of entity, drawn inside the round nodes of
// the reasoning figure, its legend and the query diagram. kind is coded
// twice, by colour (lib/vizPalette.ts) and by this icon, so it never
// rests on colour alone. icons are plain shapes in a 24 x 24 box, stroked
// with round caps, rendered three ways: React (Glyph), an SVG data URI
// (Cytoscape node background) and SVG markup (the figure export).

import type { Kind } from "@/lib/vizPalette";

type Part = { d: string } | { cx: number; cy: number; r: number };

const ICONS: Record<Kind, Part[]> = {
  // a capsule at 45 degrees with its seam.
  chemical: [{ d: "M4.6 12.6 12.6 4.6a4.95 4.95 0 0 1 7 7l-8 8a4.95 4.95 0 0 1-7-7z" }, { d: "M8.6 8.6l7 7" }],
  // a pulse trace: a condition of the body.
  disease: [{ d: "M2.5 12.5h4.2l2.3-6 4.4 11 2.4-5h5.7" }],
  // a double helix with two rungs.
  gene: [
    { d: "M8 3c0 4.6 8 4.4 8 9s-8 4.4-8 9" },
    { d: "M16 3c0 4.6-8 4.4-8 9s8 4.4 8 9" },
    { d: "M9.6 6.4h4.8M9.6 17.6h4.8" },
  ],
  // three steps joined: a pathway or process.
  process: [
    { cx: 6, cy: 7, r: 2.4 },
    { cx: 18, cy: 7, r: 2.4 },
    { cx: 12, cy: 17.5, r: 2.4 },
    { d: "M8.4 7h7.2M7.2 9.1l3.6 6.3M16.8 9.1l-3.6 6.3" },
  ],
  // a cell with its nucleus.
  anatomy: [{ cx: 12, cy: 12, r: 7.6 }, { cx: 13.6, cy: 10.4, r: 2.6 }],
  other: [{ cx: 12, cy: 12, r: 2.2 }],
};

function partMarkup(part: Part): string {
  return "d" in part ? `<path d="${part.d}"/>` : `<circle cx="${part.cx}" cy="${part.cy}" r="${part.r}"/>`;
}

// the icon as SVG markup, to sit at (x, y) with the given size.
export function iconSvg(kind: Kind, x: number, y: number, size: number, color: string): string {
  const scale = size / 24;
  return (
    `<g transform="translate(${x - size / 2} ${y - size / 2}) scale(${scale})" fill="none" stroke="${color}" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${partsOf(kind).map(partMarkup).join("")}</g>`
  );
}

// the icon as an image for a Cytoscape node background.
export function iconDataUri(kind: Kind, color: string): string {
  const svg =
    // drawn at 96 px so the canvas never scales a small bitmap up (blurred
    // icons at high zoom or on a retina screen).
    `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 24 24" fill="none" stroke="${color}" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${partsOf(kind).map(partMarkup).join("")}</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

// the icon as React SVG children, for an <svg viewBox="0 0 24 24">.
export function iconParts(kind: Kind): Part[] {
  return partsOf(kind);
}

// a kind the palette does not know draws as "other".
function partsOf(kind: Kind): Part[] {
  return ICONS[kind] ?? ICONS.other;
}
