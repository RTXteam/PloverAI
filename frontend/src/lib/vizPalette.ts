// the reasoning figure's colours and encodings, shared by the Cytoscape
// canvas (components/ReasoningGraph.tsx), its caption and the SVG / PNG
// export (lib/figureExport.ts). the hex values mirror the --viz-* tokens
// in app/globals.css, which the DOM parts of the figure use; keep the two
// in step.
//
// kind colours: the dataviz reference palette, categorical slots 1-3
// (blue / orange / aqua), validated all-pairs for colour-vision
// deficiency on both surfaces with scripts/validate_palette.js (light:
// CVD ΔE 9.2, normal vision 24.0; dark: 9.4 / 20.9). every other kind is
// neutral grey and told apart by shape, and every node is labelled, which
// is the relief the light-mode aqua (2.7:1) requires. kind is also drawn
// as an icon inside each node (lib/nodeIcons.ts), so it never rests on
// colour alone.

import type { ReasoningNodeRole } from "@/lib/api";
import type { Strength } from "@/lib/reasoningLinks";

export type VizPalette = {
  surface: string;
  rule: string;
  ink: string;
  ink2: string;
  muted: string;
  edge: string;
  neutral: string;
  chemical: string;
  disease: string;
  gene: string;
  // the halo of a selected node or link (the answers table's selection hue).
  focus: string;
};

export const VIZ: Record<"light" | "dark", VizPalette> = {
  light: {
    surface: "#fcfcfb",
    rule: "#e4e3de",
    ink: "#0b0b0b",
    ink2: "#52514e",
    muted: "#8a8984",
    edge: "#a3a29d",
    neutral: "#8a8984",
    chemical: "#2a78d6",
    disease: "#eb6834",
    gene: "#1baf7a",
    focus: "#0284c7",
  },
  dark: {
    surface: "#1a1a19",
    rule: "#33332f",
    ink: "#ffffff",
    ink2: "#c3c2b7",
    muted: "#8f8e87",
    edge: "#6b6a64",
    neutral: "#8f8e87",
    chemical: "#3987e5",
    disease: "#d95926",
    gene: "#199e70",
    focus: "#38bdf8",
  },
};

export type Kind = "chemical" | "disease" | "gene" | "process" | "anatomy" | "other";

const KIND_OF_CATEGORY: Record<string, Kind> = {
  Drug: "chemical", SmallMolecule: "chemical", ChemicalEntity: "chemical", MolecularMixture: "chemical",
  ChemicalMixture: "chemical", ComplexMolecularMixture: "chemical", MolecularEntity: "chemical",
  Disease: "disease", PhenotypicFeature: "disease", DiseaseOrPhenotypicFeature: "disease",
  Gene: "gene", Protein: "gene", GeneFamily: "gene", Polypeptide: "gene", GeneProduct: "gene",
  Pathway: "process", BiologicalProcess: "process", MolecularActivity: "process",
  CellularComponent: "process", PhysiologicalProcess: "process",
  AnatomicalEntity: "anatomy", Cell: "anatomy", GrossAnatomicalStructure: "anatomy",
};

export function kindOf(category: string | null): Kind {
  return KIND_OF_CATEGORY[(category ?? "").replace(/^biolink:/, "")] ?? "other";
}

export const KIND_LABEL: Record<Kind, string> = {
  chemical: "chemical / drug",
  disease: "disease / phenotype",
  gene: "gene / protein",
  process: "pathway / process",
  anatomy: "anatomy / cell",
  other: "other",
};

// "other" is hollow: surface fill, neutral outline.
export function kindFill(kind: Kind, p: VizPalette): string {
  switch (kind) {
    case "chemical":
      return p.chemical;
    case "disease":
      return p.disease;
    case "gene":
      return p.gene;
    case "process":
    case "anatomy":
      return p.neutral;
    case "other":
      return p.surface;
  }
}

// every node is a circle (a graph, not a diagram of boxes); kind is told
// by colour and by the icon inside (lib/nodeIcons.ts). diameter per role:
// the question's entity is the largest, the answers next, the entities
// between them smallest.
export const NODE_DIAMETER: Record<ReasoningNodeRole, number> = {
  query: 36,
  answer: 26,
  intermediate: 22,
};

// the icon inside a node: white on the kind's fill, ink on the hollow
// "other" node.
export function iconColor(kind: Kind, p: VizPalette): string {
  return kind === "other" ? p.ink2 : "#ffffff";
}

// line style per evidence strength; null is a solid line.
export const EVIDENCE_STYLE: Record<Strength, { dash: number[] | null; label: string }> = {
  curated: { dash: null, label: "curated assertion" },
  inferred: { dash: [7, 3], label: "inferred / entailed" },
  statistical: { dash: [3, 3], label: "statistical / co-occurrence" },
  weak: { dash: [1.5, 2.5], label: "text-mined / not recorded" },
};

// line width grows with the square root of the number of facts on a
// link, kept thin so many links stay readable: one fact 0.9 px, four
// 1.6 px, nine 2 px, never above 2.2 px.
export function linkWidth(nFacts: number): number {
  return Math.min(2.2, 0.9 + 0.4 * Math.sqrt(Math.max(0, nFacts - 1)));
}
