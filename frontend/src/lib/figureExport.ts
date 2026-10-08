// the reasoning figure as a stand-alone file for a paper. rebuilt as SVG
// from the node positions and link endpoints Cytoscape laid out, always
// in the light palette on a white page and in a system sans-serif (a
// figure in a manuscript cannot rely on the app's web font), with the
// legend drawn under the graph so the file stands on its own. the PNG
// export rasterises the same SVG at 4x.

import type { Core } from "cytoscape";
import type { ReasoningGraph, ReasoningNodeRole } from "@/lib/api";
import { STRENGTH_ORDER, type Strength } from "@/lib/reasoningLinks";
import { iconSvg } from "@/lib/nodeIcons";
import {
  EVIDENCE_STYLE,
  KIND_LABEL,
  NODE_DIAMETER,
  VIZ,
  iconColor,
  kindFill,
  linkWidth,
  type Kind,
} from "@/lib/vizPalette";

const PAGE = { ...VIZ.light, surface: "#ffffff" };
const FONT = "Helvetica, Arial, sans-serif";
const PAD = 24;
const LABEL_MAX_WIDTH = 130;

const NODE_FONT: Record<ReasoningNodeRole, { size: number; weight: number; color: string }> = {
  query: { size: 13, weight: 600, color: PAGE.ink },
  answer: { size: 12, weight: 400, color: PAGE.ink },
  intermediate: { size: 11, weight: 400, color: PAGE.ink2 },
};
const EDGE_FONT_SIZE = 10;
const EDGE_LABEL_MAX = 150;
const QUERY_LABEL_MAX = 160;
// labels wear a halo of the page colour, as on the canvas, instead of a box.
const HALO = ` stroke="${PAGE.surface}" stroke-width="2.5" stroke-linejoin="round" paint-order="stroke"`;

export function figureSvg(cy: Core, { predicates }: { predicates: boolean }): string {
  const box = cy.elements().boundingBox({ includeLabels: true, includeOverlays: false });
  const dx = PAD - box.x1;
  const dy = PAD - box.y1;
  const parts: string[] = [];
  // predicate labels go above every line, as on the canvas, where a
  // labelled link is drawn over the others.
  const labels: string[] = [];

  cy.edges().forEach((edge) => {
    if (edge.hasClass("pending")) return;
    const s = edge.sourceEndpoint();
    const t = edge.targetEndpoint();
    // the layered view's links are curves: Cytoscape's control points,
    // joined the way it draws them, by quadratic pieces through the
    // midpoints between control points.
    const controls = edge.hasClass("lr") ? (edge.controlPoints() ?? []) : [];
    const from = controls.length > 0 ? controls[controls.length - 1] : s;
    const length = Math.hypot(t.x - from.x, t.y - from.y);
    if (length < 1) return;
    const ux = (t.x - from.x) / length;
    const uy = (t.y - from.y) / length;
    const width = linkWidth(edge.data("nFacts") as number);
    const dash = EVIDENCE_STYLE[edge.data("strength") as Strength].dash;
    const head = 6 + 1.2 * width;
    const half = 3 + 0.8 * width;
    const bx = t.x - ux * head;
    const by = t.y - uy * head;
    let path = `M${r(s.x)} ${r(s.y)}`;
    controls.forEach((c, i) => {
      const next = controls[i + 1];
      const end = next ? { x: (c.x + next.x) / 2, y: (c.y + next.y) / 2 } : { x: bx, y: by };
      path += ` Q${r(c.x)} ${r(c.y)} ${r(end.x)} ${r(end.y)}`;
    });
    if (controls.length === 0) path += ` L${r(bx)} ${r(by)}`;
    parts.push(
      // the casing of the page colour the canvas draws under every line.
      `<path d="${path}" fill="none" stroke="${PAGE.surface}" stroke-width="${r(width + 2.5)}" />`,
      `<path d="${path}" fill="none" stroke="${PAGE.edge}" stroke-width="${r(width)}"` +
        (dash ? ` stroke-dasharray="${dash.join(" ")}"` : "") +
        " />",
      `<polygon points="${r(t.x)},${r(t.y)} ${r(bx - uy * half)},${r(by + ux * half)} ${r(bx + uy * half)},${r(by - ux * half)}" fill="${PAGE.edge}" />`,
    );
    // labels the canvas hides (collisions, text-mined links, the links of
    // a bundle but its first) stay out of the file too. like the canvas,
    // they are level, wrap, and sit where the layered view put them.
    // the labels the canvas shows, where it shows them (class "speak").
    const shaped = edge.hasClass("lr") && typeof edge.data("lx") === "number";
    if (!predicates || !edge.hasClass("speak")) return;
    const font = `italic ${EDGE_FONT_SIZE}px ${FONT}`;
    const lines = wrapText(edge.data("label") as string, EDGE_LABEL_MAX, font);
    const mid = shaped ? { x: edge.data("lx") as number, y: edge.data("ly") as number } : edge.midpoint();
    const lineHeight = EDGE_FONT_SIZE * 1.25;
    const h = lines.length * lineHeight + 2;
    // in the layered view the label runs along its line.
    const angle = shaped ? ((edge.data("lrot") as number | undefined) ?? 0) : 0;
    const turn = angle ? ` transform="rotate(${r(angle)} ${r(mid.x)} ${r(mid.y)})"` : "";
    labels.push(
      `<text text-anchor="middle" font-size="${EDGE_FONT_SIZE}" font-style="italic" fill="${PAGE.ink2}"${HALO}${turn}>` +
        lines
          .map((line, i) => `<tspan x="${r(mid.x)}" y="${r(mid.y - h / 2 + 1 + EDGE_FONT_SIZE * 0.95 + i * lineHeight)}">${esc(line)}</tspan>`)
          .join("") +
        "</text>",
    );
  });

  cy.nodes().forEach((node) => {
    const { x, y } = node.position();
    if (node.hasClass("heading")) {
      if (node.data("empty")) return;
      parts.push(
        `<text x="${r(x)}" y="${r(y)}" dy="0.35em" text-anchor="middle" font-size="10" font-weight="600" letter-spacing="0.4" fill="${PAGE.muted}">${esc(node.data("label") as string)}</text>`,
      );
      return;
    }
    const role = node.data("role") as ReasoningNodeRole;
    const kind = node.data("kind") as Kind;
    const d = NODE_DIAMETER[role] ?? NODE_DIAMETER.answer;
    const fill = kindFill(kind, PAGE);
    const ring = kind === "other" ? ` stroke="${PAGE.neutral}" stroke-width="1.25"` : "";
    parts.push(`<circle cx="${r(x)}" cy="${r(y)}" r="${r(d / 2)}" fill="${fill}"${ring} />`);
    parts.push(iconSvg(kind, x, y, d * 0.56, iconColor(kind, PAGE)));

    const style = NODE_FONT[role] ?? NODE_FONT.answer;
    const font = `${style.weight} ${style.size}px ${FONT}`;
    const text = (node.data("display") as string | undefined) ?? (node.data("label") as string);
    const lineHeight = style.size * 1.2;
    // where the canvas puts the label: left of an answer and right of the
    // question in the layered view, below the node otherwise.
    const side = node.hasClass("lr") && role === "answer" ? "left" : node.hasClass("lr") && role === "query" ? "right" : "below";
    const lines = wrapText(text, side === "below" ? LABEL_MAX_WIDTH : side === "right" ? QUERY_LABEL_MAX : 200, font);
    const h = lines.length * lineHeight + 3;
    const anchor = side === "left" ? "end" : side === "right" ? "start" : "middle";
    const tx = side === "left" ? x - d / 2 - 9 : side === "right" ? x + d / 2 + 11 : x;
    const top = side === "below" ? y + d / 2 + 7 : y - h / 2 + 1;
    parts.push(
      `<text text-anchor="${anchor}" font-size="${style.size}" font-weight="${style.weight}" fill="${style.color}"${HALO}>` +
        lines
          .map((line, i) => `<tspan x="${r(tx)}" y="${r(top + style.size * 0.9 + i * lineHeight)}">${esc(line)}</tspan>`)
          .join("") +
        "</text>",
    );
  });

  const graphWidth = box.w + 2 * PAD;
  const legend = legendSvg(cy, Math.max(graphWidth, 480) - 2 * PAD);
  const width = Math.max(graphWidth, 480);
  const height = box.h + 2 * PAD + legend.height;

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${r(width)}" height="${r(height)}" viewBox="0 0 ${r(width)} ${r(height)}" font-family="${FONT}">` +
    `<rect width="100%" height="100%" fill="${PAGE.surface}" />` +
    `<g transform="translate(${r(dx)} ${r(dy)})">${parts.join("")}${labels.join("")}</g>` +
    `<g transform="translate(${PAD} ${r(box.h + 2 * PAD)})">${legend.body}</g>` +
    "</svg>"
  );
}

// the kinds and evidence styles present, as one or two flowing rows.
function legendSvg(cy: Core, maxWidth: number): { body: string; height: number } {
  // the column headings are nodes too, of no kind worth a key.
  const kinds = new Set(cy.nodes().not(".heading").map((n) => n.data("kind") as Kind));
  const strengths = new Set(cy.edges().not(".pending").map((e) => e.data("strength") as Strength));
  const items: { width: number; draw: (x: number, y: number) => string }[] = [];
  const font = `11px ${FONT}`;
  const text = (x: number, y: number, label: string) =>
    `<text x="${r(x)}" y="${r(y)}" dy="0.35em" font-size="11" fill="${PAGE.ink2}">${esc(label)}</text>`;

  for (const kind of Object.keys(KIND_LABEL) as Kind[]) {
    if (!kinds.has(kind)) continue;
    const label = KIND_LABEL[kind];
    items.push({
      width: 16 + measureText(label, font),
      draw: (x, y) =>
        `<circle cx="${r(x + 6)}" cy="${r(y)}" r="6" fill="${kindFill(kind, PAGE)}" stroke="${kind === "other" ? PAGE.neutral : "none"}" stroke-width="1.25" />` +
        iconSvg(kind, x + 6, y, 7.5, iconColor(kind, PAGE)) +
        text(x + 16, y, label),
    });
  }
  const queryLabel = "question entity: the largest node, in bold";
  items.push({
    width: measureText(queryLabel, font),
    draw: (x, y) => text(x, y, queryLabel),
  });
  for (const strength of STRENGTH_ORDER) {
    if (!strengths.has(strength)) continue;
    const { dash, label } = EVIDENCE_STYLE[strength];
    items.push({
      width: 30 + measureText(label, font),
      draw: (x, y) =>
        `<line x1="${r(x)}" y1="${r(y)}" x2="${r(x + 24)}" y2="${r(y)}" stroke="${PAGE.edge}" stroke-width="1.5"` +
        (dash ? ` stroke-dasharray="${dash.join(" ")}"` : "") +
        " />" +
        text(x + 30, y, label),
    });
  }
  const widthLabel = "line width: number of facts";
  items.push({
    width: 30 + measureText(widthLabel, font),
    draw: (x, y) =>
      `<line x1="${r(x)}" y1="${r(y - 3)}" x2="${r(x + 24)}" y2="${r(y - 3)}" stroke="${PAGE.edge}" stroke-width="${linkWidth(1)}" />` +
      `<line x1="${r(x)}" y1="${r(y + 3)}" x2="${r(x + 24)}" y2="${r(y + 3)}" stroke="${PAGE.edge}" stroke-width="${r(linkWidth(9))}" />` +
      text(x + 30, y, widthLabel),
  });

  const rowHeight = 20;
  const gap = 18;
  let x = 0;
  let row = 0;
  const body = items
    .map((item) => {
      if (x > 0 && x + item.width > maxWidth) {
        x = 0;
        row += 1;
      }
      const out = item.draw(x, row * rowHeight + 6);
      x += item.width + gap;
      return out;
    })
    .join("");
  return { body, height: (row + 1) * rowHeight + 10 };
}

let measureContext: CanvasRenderingContext2D | null = null;

export function measureText(text: string, font: string): number {
  measureContext ??= document.createElement("canvas").getContext("2d");
  if (!measureContext) return text.length * 6;
  measureContext.font = font;
  return measureContext.measureText(text).width;
}

// greedy word wrap, the way Cytoscape wraps a label at text-max-width.
export function wrapText(text: string, maxWidth: number, font: string): string[] {
  const lines: string[] = [];
  let line = "";
  for (const word of text.split(/\s+/)) {
    const candidate = line ? `${line} ${word}` : word;
    if (line && measureText(candidate, font) > maxWidth) {
      lines.push(line);
      line = word;
    } else {
      line = candidate;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function r(n: number): string {
  return String(Math.round(n * 100) / 100);
}

function esc(text: string): string {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

// the figures on screen, by the graph they draw, so the PDF export can
// take the live layout (including any nodes the reader dragged).
const liveFigures = new Map<string, Core>();

export function figureKey(graph: ReasoningGraph): string {
  return [graph.query, ...graph.nodes.map((n) => n.id).sort(), graph.edges.length].join("|");
}

export function registerFigure(key: string, cy: Core): () => void {
  liveFigures.set(key, cy);
  return () => {
    if (liveFigures.get(key) === cy) liveFigures.delete(key);
  };
}

export function liveFigureSvg(graph: ReasoningGraph): string | null {
  const cy = liveFigures.get(figureKey(graph));
  return cy && !cy.destroyed() && cy.nodes().length > 0 ? figureSvg(cy, { predicates: true }) : null;
}

export async function svgToPng(svg: string, scale = 4): Promise<Blob> {
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  try {
    const image = new Image();
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("could not render the figure"));
      image.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(image.width * scale);
    canvas.height = Math.ceil(image.height * scale);
    const context = canvas.getContext("2d");
    if (!context) throw new Error("no 2D canvas");
    context.scale(scale, scale);
    context.drawImage(image, 0, 0);
    return await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("PNG encoding failed"))), "image/png"),
    );
  } finally {
    URL.revokeObjectURL(url);
  }
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
