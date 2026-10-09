"use client";

// the reasoning figure for ARAX mode: a knowledge-graph drawing of the
// facts behind the picked answers, rendered with Cytoscape.js. two
// layouts, switched in the toolbar:
//   layered (default) — deterministic, left to right: the answers in
//     ARAX rank order on the left, the question's entity on the right,
//     the entities between them in columns by distance
//     (lib/layeredLayout.ts). the same graph always draws the same way.
//   force — force-directed (fcose), turned so its long axis runs along
//     the frame.
//
// encoding (the caption under the figure carries the legend):
//   every node is a circle; its colour and the icon inside give the kind
//   of entity (lib/vizPalette.ts, lib/nodeIcons.ts);
//   node size = role: the question's entity is the largest, the
//     entities between it and the answers are small;
//   in the layered view a heading names each column, and the answers
//     stand in ARAX rank order, top to bottom. their ranks are not
//     printed: hovering an answer marks its row in the answers table;
//   one link per pair of entities on a reasoning path
//   (lib/reasoningLinks.ts): arrow and label = the most specific statement
//     among its strongest facts (subject → object), line width = number of
//     facts, line style = strongest evidence among them.
// labels wear a thin halo of the page colour instead of a box, so a line
// passing behind one stops just short of its letters; a predicate label
// sits just above its line, and one that would collide with a node or a
// stronger link's label slides along its line, or, with no room left, is
// held back until its link is hovered.
// interaction: hover to isolate an answer's paths or a node's
// neighbours; click for the facts, sources and papers; drag nodes to
// rearrange; ctrl/⌘ + scroll or the buttons to zoom. the toolbar exports
// the figure as SVG or PNG for a paper (lib/figureExport.ts).
//
// while a query runs (live), the figure grows instead of redrawing: the
// question's entity first, then a dashed "ghost" answer standing for
// what ARAX is looking for, then the answers emerging from it, then the
// paths. every step starts from the positions already drawn and eases
// into the new layout, and the finished result reuses the last live
// positions (positionCache), so the figure never jumps.

import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape, {
  type Collection,
  type Core,
  type EdgeSingular,
  type ElementDefinition,
  type EventObject,
  type LayoutOptions,
  type NodeSingular,
  type StylesheetJson,
} from "cytoscape";
import fcose from "cytoscape-fcose";
import type { ReasoningFact, ReasoningGraph as Graph, ReasoningNode } from "@/lib/api";
import { downloadBlob, figureKey, figureSvg, measureText, registerFigure, svgToPng, wrapText } from "@/lib/figureExport";
import { COLUMN_GAP, edgeKey, layeredLayout, type Point, type Route } from "@/lib/layeredLayout";
import { iconDataUri, iconParts } from "@/lib/nodeIcons";
import { STRENGTH_ORDER, buildLinks, humanPredicate, predicateSummary, type LinkSpec, type Strength } from "@/lib/reasoningLinks";
import { useTheme } from "@/lib/theme";
import {
  KIND_LABEL,
  NODE_DIAMETER,
  VIZ,
  iconColor,
  kindFill,
  kindOf,
  linkWidth,
  type Kind,
  type VizPalette,
} from "@/lib/vizPalette";

let fcoseRegistered = false;
function registerFcose() {
  if (fcoseRegistered) return;
  cytoscape.use(fcose);
  fcoseRegistered = true;
}

// "bundle": the shared stretch of links that run as one line.
type Hover = { kind: "node" | "edge" | "bundle"; id: string } | null;
export type Selection = { kind: "node" | "link"; id: string } | null;
type Tip = { x: number; y: number; title: string; lines: string[] } | null;
export type LayoutMode = "layered" | "force";

// what ARAX is looking for, drawn before any answer exists.
export type Ghost = {
  label: string;
  predicate: string | null;
  // whether the answer is the subject of the query edge (drug treats disease).
  answerIsSubject: boolean;
};

type Props = {
  graph: Graph;
  // while the query runs: answers without facts yet get a dotted
  // placeholder link to the question, and the export tools are hidden.
  live?: boolean;
  // live only: the stand-in answer shown until ARAX's answers arrive.
  ghost?: Ghost | null;
  // a fact id hovered in the explanation ([F3]); its link is isolated.
  highlightFactId?: string | null;
  // an answer chosen outside the figure (the answers table); its paths
  // are isolated until another one is chosen.
  focusAnswerId?: string | null;
  // ARAX rank by answer CURIE, which orders the answers in the layered
  // view. without it, answers keep graph order.
  ranks?: ReadonlyMap<string, number>;
  // when given, a clicked node or link is reported here (the page shows
  // its details in a side panel) instead of in a panel over the figure.
  onSelect?: (selection: Selection) => void;
  // an answer hovered outside the figure (the answers table); its paths
  // light up while it is hovered.
  hoverAnswerId?: string | null;
  // when given, the answers on what the pointer is over (an answer, or
  // the answers whose paths pass the node or link) are reported here, for
  // the answers table to mark, and an answer gets no tooltip of its own.
  onHoverAnswers?: (curies: string[]) => void;
  // the facts behind what the pointer is over, for the evidence list.
  onHoverFacts?: (factIds: string[]) => void;
  // fill the parent's height instead of sizing to the graph.
  fill?: boolean;
  height?: number;
};

const PADDING = 28;
const GHOST_ID = "__ghost__";
const LAYOUT_EASE_MS = 450;

// the last positions of every figure drawn, by graph, so the finished
// result opens exactly where the live figure ended.
const positionCache = new Map<string, Record<string, { x: number; y: number }>>();

// a figure grows with the number of entities it shows, within bounds.
function fittedHeight(graph: Graph): number {
  return Math.min(640, Math.max(400, 280 + graph.nodes.length * 26));
}

export function ReasoningGraph({
  graph,
  live = false,
  ghost = null,
  highlightFactId = null,
  focusAnswerId = null,
  ranks,
  onSelect,
  hoverAnswerId = null,
  onHoverAnswers,
  onHoverFacts,
  fill = false,
  height,
}: Props) {
  const { resolvedTheme } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [hover, setHover] = useState<Hover>(null);
  const [tip, setTip] = useState<Tip>(null);
  const [selection, setSelection] = useState<Selection>(null);
  const [predicates, setPredicates] = useState(true);
  const [mode, setMode] = useState<LayoutMode>("layered");

  // the latest onSelect, so the effect below does not re-fire when the
  // parent passes a new function on every render.
  const onSelectRef = useRef(onSelect);
  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);
  useEffect(() => {
    onSelectRef.current?.(selection);
  }, [selection]);
  const onHoverAnswersRef = useRef(onHoverAnswers);
  const onHoverFactsRef = useRef(onHoverFacts);
  useEffect(() => {
    onHoverAnswersRef.current = onHoverAnswers;
    onHoverFactsRef.current = onHoverFacts;
  }, [onHoverAnswers, onHoverFacts]);

  const links = useMemo(() => buildLinks(graph), [graph]);
  const labels = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n.label])), [graph]);
  const pendingAnswers = useMemo(() => {
    if (!live) return [];
    const linked = new Set(links.flatMap((l) => [l.source, l.target]));
    return graph.nodes.filter((n) => n.role === "answer" && !linked.has(n.id)).map((n) => n.id);
  }, [live, links, graph]);
  const elements = useMemo(
    () => toElements(graph, links, pendingAnswers, live ? ghost : null, ranks, mode),
    [graph, links, pendingAnswers, live, ghost, ranks, mode],
  );
  const key = useMemo(() => figureKey(graph), [graph]);

  // one Cytoscape instance per mounted figure.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    registerFcose();
    const cy = cytoscape({
      container,
      elements: [],
      minZoom: 0.25,
      maxZoom: 3,
      userZoomingEnabled: false,
      boxSelectionEnabled: false,
    });
    cyRef.current = cy;
    drawDirect(cy);

    const tipAt = (event: EventObject, title: string, lines: string[]) =>
      setTip({ x: event.renderedPosition.x, y: event.renderedPosition.y, title, lines });

    cy.on("mouseover", "node", (event) => {
      const node = event.target as NodeSingular;
      if (node.hasClass("ghost")) return;
      container.style.cursor = "pointer";
      setHover({ kind: "node", id: node.id() });
      // a node needs no tooltip: its name is on the canvas, its kind in
      // its colour and icon, an answer's rank in the answers table, and a
      // click opens the rest. without the table, it names the kind.
      if (onHoverAnswersRef.current) {
        setTip(null);
        return;
      }
      tipAt(event, node.data("label") as string, [
        `${KIND_LABEL[node.data("kind") as Kind]} · ${(node.data("category") ?? "unknown").replace(/^biolink:/, "")}`,
        "click for its facts and sources",
      ]);
    });
    // over the stretch a bundle shares, the hover names every link in it;
    // over a link's own stretch, that link. re-checked as the pointer
    // moves along the line.
    let edgeHover = "";
    const onEdge = (event: EventObject) => {
      const edge = event.target as EdgeSingular;
      if (edge.hasClass("pending") || edge.hasClass("ghost")) return;
      container.style.cursor = "pointer";
      const from = edge.data("sharedFrom") as number | null | undefined;
      const inBundle =
        edge.hasClass("lr") && ((edge.data("shared") as number | undefined) ?? 1) > 1 && typeof from === "number" && event.position.x >= from - 4;
      const id = inBundle ? (edge.data("bundleId") as string) : edge.id();
      if (edgeHover === `${inBundle}|${id}`) return;
      edgeHover = `${inBundle}|${id}`;
      if (inBundle) {
        const members = cy.edges().filter((e) => e.data("bundleId") === id).toArray() as EdgeSingular[];
        // the links share one end, the target or the source; name the others.
        const intoOne = members.every((e) => e.target().id() === members[0].target().id());
        const shared = intoOne ? members[0].target() : members[0].source();
        setHover({ kind: "bundle", id });
        tipAt(event, edge.data("label") as string, [
          `${members.length} links ${intoOne ? "into" : "from"} ${shared.data("label") as string}, ${intoOne ? "from" : "to"}:`,
          ...members.map((e) => {
            const n = e.data("nFacts") as number;
            return `${(intoOne ? e.source() : e.target()).data("display") as string} · ${n} fact${n === 1 ? "" : "s"}`;
          }),
        ]);
        return;
      }
      setHover({ kind: "edge", id });
      const n = edge.data("nFacts") as number;
      tipAt(event, `${edge.source().data("label")} → ${edge.target().data("label")}`, [
        ...(edge.data("summary") as string[]),
        `${n} fact${n === 1 ? "" : "s"} · click for sources`,
      ]);
    };
    cy.on("mouseover mousemove", "edge", onEdge);
    cy.on("mouseout", "node, edge", () => {
      edgeHover = "";
      container.style.cursor = "";
      setHover(null);
      setTip(null);
    });
    cy.on("grab viewport", () => setTip(null));
    cy.on("layoutstop", () => {
      // a fresh force layout is turned to the frame at once; a layered
      // one is already left to right; an incremental one keeps its
      // orientation and eases the viewport over.
      if (cy.scratch("orientNext")) {
        if (cy.scratch("layoutMode") === "force") orientAndFit(cy);
        else fitFigure(cy);
      } else easeToFit(cy);
      shapeLinks(cy);
      declutterLabels(cy);
      rememberPositions(cy);
    });
    // in the layered view a node moves only up and down its column, so
    // the columns, and the links' routes between them, hold.
    cy.on("grab", "node", (event) => {
      const node = event.target as NodeSingular;
      node.scratch("columnX", node.position("x"));
    });
    cy.on("drag", "node", (event) => {
      const node = event.target as NodeSingular;
      const x = node.scratch("columnX") as number | undefined;
      if (cy.scratch("layoutMode") === "layered" && x !== undefined && node.position("x") !== x) node.position("x", x);
    });
    cy.on("dragfree", () => {
      shapeLinks(cy);
      declutterLabels(cy);
      rememberPositions(cy);
    });
    // while nodes move (an eased layout, a drag), the curved links follow
    // them, once per frame.
    let shaping = false;
    cy.on("position", "node", () => {
      if (shaping || cy.scratch("layoutMode") !== "layered") return;
      shaping = true;
      window.requestAnimationFrame(() => {
        shaping = false;
        if (!cy.destroyed()) shapeLinks(cy);
      });
    });
    cy.on("tap", (event) => {
      if (event.target === cy) setSelection(null);
    });
    cy.on("tap", "node", (event) => {
      if (!event.target.hasClass("ghost")) setSelection({ kind: "node", id: event.target.id() });
    });
    cy.on("tap", "edge", (event) => {
      if (!event.target.hasClass("pending") && !event.target.hasClass("ghost")) {
        setSelection({ kind: "link", id: event.target.id() });
      }
    });

    // the page scrolls on a plain wheel; ctrl/⌘ + wheel (and a trackpad
    // pinch, which arrives as ctrl + wheel) zooms the figure.
    const onWheel = (event: WheelEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      event.preventDefault();
      const rect = container.getBoundingClientRect();
      zoomBy(cy, Math.exp(-event.deltaY * 0.01), { x: event.clientX - rect.left, y: event.clientY - rect.top });
    };
    container.addEventListener("wheel", onWheel, { passive: false });
    // leaving the canvas ends any hover, however fast the pointer leaves.
    const onLeave = () => {
      edgeHover = "";
      container.style.cursor = "";
      setHover(null);
      setTip(null);
    };
    container.addEventListener("mouseleave", onLeave);

    const resize = new ResizeObserver(() => {
      cy.resize();
      fitFigure(cy);
    });
    resize.observe(container);

    return () => {
      resize.disconnect();
      container.removeEventListener("wheel", onWheel);
      container.removeEventListener("mouseleave", onLeave);
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  useEffect(() => {
    cyRef.current?.scratch("maxFitZoom", fill ? 1.2 : 1.25);
  }, [fill]);

  // style follows the theme and the predicate-label toggle.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const font = getComputedStyle(document.body).fontFamily;
    cy.scratch("figureFont", font);
    cy.scratch("predicates", predicates);
    cy.style(figureStyle(VIZ[resolvedTheme], font));
    declutterLabels(cy);
    void document.fonts?.ready.then(() => {
      if (cy.destroyed()) return;
      cy.style().update();
      declutterLabels(cy);
    });
  }, [resolvedTheme, predicates]);

  // elements: add what is new, drop what is gone, and lay the figure out
  // again when its shape or the layout mode changed (live mode adds
  // answers, then paths).
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.scratch("figureKey", key);
    const fresh = cy.nodes().length === 0;
    const modeChanged = cy.scratch("layoutMode") !== mode;
    if (!syncElements(cy, elements) && !modeChanged) return;
    // a layered drawing is recomputed, never cached: it is the same
    // every time. a force drawing reopens where the live figure ended.
    const cached = fresh && mode === "force" ? positionCache.get(key) : undefined;
    if (cached && cy.nodes().toArray().every((n) => cached[n.id()] !== undefined)) {
      cy.scratch("layoutMode", mode);
      cy.nodes().forEach((n) => {
        n.position(cached[n.id()]);
      });
      fitFigure(cy);
      declutterLabels(cy);
      return;
    }
    runLayout(cy, fresh || modeChanged, mode);
  }, [elements, key, mode]);

  // the PDF export reads the live figure (its layout, as dragged).
  useEffect(() => {
    const cy = cyRef.current;
    return cy ? registerFigure(key, cy) : undefined;
  }, [key]);

  // the answers and facts on what the pointer is over, for the answers
  // table and the evidence list.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const answersOut = onHoverAnswersRef.current;
    const factsOut = onHoverFactsRef.current;
    if (!hover) {
      answersOut?.([]);
      factsOut?.([]);
      return;
    }
    const node = hover.kind === "node" ? cy.getElementById(hover.id) : null;
    const isAnswer = node?.data("role") === "answer";
    const edges = node
      ? isAnswer
        ? cy.edges().filter((e) => ((e.data("answers") as string[] | undefined) ?? []).includes(hover.id)).toArray()
        : node.connectedEdges().toArray()
      : hover.kind === "edge"
        ? cy.getElementById(hover.id).toArray()
        : cy.edges().filter((e) => e.data("bundleId") === hover.id).toArray();
    const gather = (key: string) => [...new Set(edges.flatMap((e) => (e.data(key) as string[] | undefined) ?? []))];
    answersOut?.(isAnswer ? [hover.id] : gather("answers"));
    factsOut?.(gather("factIds"));
  }, [hover]);

  // isolation, first match wins: a hovered node or link, an answer
  // hovered in the answers table, a fact hovered in the text, an answer
  // chosen in the answers table.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const pathsOf = (answerId: string): Collection => {
      const edges = cy.edges().filter((e) => ((e.data("answers") as string[] | undefined) ?? []).includes(answerId));
      return edges.union(edges.connectedNodes()).union(cy.getElementById(answerId));
    };
    const factEdges = highlightFactId
      ? cy.edges().filter((e) => ((e.data("factIds") as string[] | undefined) ?? []).includes(highlightFactId))
      : null;
    let active: Collection | null = null;
    if (hover?.kind === "node") {
      const node = cy.getElementById(hover.id);
      active = node.data("role") === "answer" ? pathsOf(hover.id) : node.closedNeighborhood();
    } else if (hover?.kind === "edge") {
      const edge = cy.getElementById(hover.id);
      active = edge.union(edge.connectedNodes());
    } else if (hover?.kind === "bundle") {
      const edges = cy.edges().filter((e) => e.data("bundleId") === hover.id);
      active = edges.union(edges.connectedNodes());
    } else if (hoverAnswerId && cy.getElementById(hoverAnswerId).nonempty()) {
      active = pathsOf(hoverAnswerId);
    } else if (factEdges && factEdges.nonempty()) {
      active = factEdges.union(factEdges.connectedNodes());
    } else if (focusAnswerId && cy.getElementById(focusAnswerId).nonempty()) {
      active = pathsOf(focusAnswerId);
    }
    const leads = new Map<string, EdgeSingular>();
    cy.edges(".lr").forEach((e) => {
      if (!e.data("follower")) leads.set(e.data("bundleId") as string, e);
    });
    cy.batch(() => {
      cy.elements().removeClass("faded active hush");
      if (!active) return;
      cy.elements().not(active).not(".heading").addClass("faded");
      active.addClass("active");
      // a bundle shows one label: its first link's, or, when only other
      // links of it light up, the first of those.
      const spoken = new Set<string>();
      cy.edges(".lr.active").forEach((e) => {
        if (!e.data("follower")) return;
        const bundle = e.data("bundleId") as string;
        if (hover?.kind === "bundle" || leads.get(bundle)?.hasClass("active") || spoken.has(bundle)) e.addClass("hush");
        else spoken.add(bundle);
      });
    });
    // while part of the figure is lit, only its links are labelled, laid
    // out again for that part alone, the hovered link's label first.
    const first = hover?.kind === "edge" ? hover.id : hover?.kind === "bundle" ? (leads.get(hover.id)?.id() ?? null) : null;
    cy.scratch("labelFocus", active ? { ids: new Set(active.edges().map((e) => e.id())), first } : null);
    declutterLabels(cy);
  }, [hover, hoverAnswerId, highlightFactId, focusAnswerId, elements]);

  const closePanel = () => {
    setSelection(null);
    cyRef.current?.elements().unselect();
  };

  const exportFigure = async (format: "svg" | "png") => {
    const cy = cyRef.current;
    if (!cy) return;
    const svg = figureSvg(cy, { predicates });
    const base = `reasoning-${slug(labels.get(graph.query) ?? "figure")}`;
    if (format === "svg") downloadBlob(new Blob([svg], { type: "image/svg+xml" }), `${base}.svg`);
    else downloadBlob(await svgToPng(svg), `${base}.png`);
  };

  const nAnswers = graph.nodes.filter((n) => n.role === "answer").length;
  const stats = [
    `${nAnswers} answer${nAnswers === 1 ? "" : "s"}`,
    `${graph.nodes.length} entities`,
    `${links.length} links`,
    `${graph.edges.length} facts`,
  ].join(" · ");

  return (
    <div
      className={`reasoning-figure flex w-full flex-col overflow-hidden ${fill ? "h-full" : "rounded-md border"}`}
      style={{ background: "var(--viz-surface)", borderColor: "var(--viz-rule)" }}
    >
      <div
        className="scroll-thin flex h-8 shrink-0 items-center justify-between gap-4 overflow-x-auto whitespace-nowrap border-b border-zinc-200 px-3 text-[11px] dark:border-zinc-800"
        style={{ color: "var(--viz-muted)" }}
      >
        <span className="tabular-nums">{stats}</span>
        <div className="flex items-center gap-3">
          <Segmented
            label="Layout"
            value={mode}
            onChange={setMode}
            options={[
              { value: "layered", label: "Layered", title: "Answers left in ARAX rank order, the question right; the same graph always draws the same way" },
              { value: "force", label: "Force", title: "Force-directed layout" },
            ]}
          />
          {!live && <Switch checked={predicates} onChange={setPredicates} label="Labels" title="Show the predicate on each link where it fits" />}
          <span className="h-4 w-px" style={{ background: "var(--viz-rule)" }} aria-hidden />
          <span className="inline-flex items-center gap-1">
            <ButtonGroup label="Zoom">
              <ToolButton onClick={() => cyRef.current && zoomBy(cyRef.current, 1 / 1.25)} title="Zoom out" aria="Zoom out">
                <Icon d="M3.5 8h9" />
              </ToolButton>
              <ToolButton onClick={() => cyRef.current && fitFigure(cyRef.current)} title="Fit the figure to the frame">
                Fit
              </ToolButton>
              <ToolButton onClick={() => cyRef.current && zoomBy(cyRef.current, 1.25)} title="Zoom in (or ctrl/⌘ + scroll)" aria="Zoom in">
                <Icon d="M3.5 8h9M8 3.5v9" />
              </ToolButton>
            </ButtonGroup>
            <ToolButton onClick={() => cyRef.current && runLayout(cyRef.current, true, mode)} title="Lay the figure out again (undoes dragging)" aria="Re-layout" bordered>
              <Icon d="M12.8 6.2A5 5 0 1 0 13 9.5M13 3v3.4H9.6" />
            </ToolButton>
          </span>
          {!live && (
            <ButtonGroup label="Download">
              <ToolButton onClick={() => void exportFigure("svg")} title="Download the figure as SVG (vector, for papers)">
                SVG
              </ToolButton>
              <ToolButton onClick={() => void exportFigure("png")} title="Download the figure as PNG (4× resolution)">
                PNG
              </ToolButton>
            </ButtonGroup>
          )}
        </div>
      </div>

      <div
        className={fill ? "relative min-h-0 flex-1" : "relative"}
        style={fill ? undefined : { height: height ?? fittedHeight(graph) }}
      >
        {/* sized by height, not by absolute insets: Cytoscape makes its
            container position: relative. */}
        <div
          ref={containerRef}
          className="h-full w-full"
          role="img"
          aria-label={`Knowledge graph of ${graph.nodes.length} entities joined by ${links.length} links; the facts are listed in the explanation.`}
        />
        {tip && <Tooltip tip={tip} />}
        {selection && !onSelect && (
          <DetailPanel selection={selection} graph={graph} links={links} labels={labels} onClose={closePanel} />
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------ cytoscape

function toElements(
  graph: Graph,
  links: LinkSpec[],
  pending: string[],
  ghost: Ghost | null,
  ranks: ReadonlyMap<string, number> | undefined,
  mode: LayoutMode,
): ElementDefinition[] {
  const known = new Set(graph.nodes.map((n) => n.id));
  const answers = graph.nodes.filter((n) => n.role === "answer").map((n) => n.id);
  // "lr": labels placed for a left-to-right drawing (style below).
  const layered = mode === "layered" ? " lr" : "";
  const nodes: ElementDefinition[] = graph.nodes.map((node) => {
    const kind = kindOf(node.category);
    const order = answers.indexOf(node.id);
    const rank = order < 0 ? null : (ranks?.get(node.id) ?? order + 1);
    return {
      group: "nodes",
      data: {
        id: node.id,
        label: node.label,
        display: node.label,
        role: node.role,
        kind,
        category: node.category,
        rank,
      },
      classes: `${node.role} ${kind}${layered}`,
    };
  });
  const edges: ElementDefinition[] = links
    .filter((link) => known.has(link.source) && known.has(link.target))
    .map((link) => ({
      group: "edges",
      data: {
        id: link.id,
        source: link.source,
        target: link.target,
        label: humanPredicate(link.predicate),
        strength: link.strength,
        nFacts: link.facts.length,
        width: linkWidth(link.facts.length),
        answers: link.answers,
        factIds: link.facts.map((f) => f.fact_id),
        summary: predicateSummary(link.facts),
      },
      classes: `${link.strength}${layered ? " lr" : ""}`,
    }));
  const placeholders: ElementDefinition[] = known.has(graph.query)
    ? pending.map((id) => ({
        group: "edges",
        data: { id: `pending:${id}`, source: id, target: graph.query, answers: [id], factIds: [], nFacts: 0 },
        classes: `pending${layered ? " lr" : ""}`,
      }))
    : [];
  const hasAnswers = graph.nodes.some((n) => n.role === "answer");
  const standIn: ElementDefinition[] =
    ghost && !hasAnswers && known.has(graph.query)
      ? [
          { group: "nodes", data: { id: GHOST_ID, label: ghost.label, display: ghost.label, role: "ghost", kind: "other", rank: null }, classes: `ghost${layered}` },
          {
            group: "edges",
            data: {
              id: `${GHOST_ID}:edge`,
              source: ghost.answerIsSubject ? GHOST_ID : graph.query,
              target: ghost.answerIsSubject ? graph.query : GHOST_ID,
              label: ghost.predicate ?? "",
              answers: [],
              factIds: [],
              nFacts: 0,
            },
            classes: `ghost${layered ? " lr" : ""}`,
          },
        ]
      : [];
  // the layered view names its columns; runLayout puts the headings
  // above them. they take no part in hover, selection or isolation.
  const headings: ElementDefinition[] =
    mode === "layered"
      ? COLUMN_HEADINGS.map((h) => ({ group: "nodes", data: { id: h.id, label: h.label, display: h.label, role: "heading", kind: "other" }, classes: "heading" }))
      : [];
  return [...nodes, ...standIn, ...edges, ...placeholders, ...headings];
}

const COLUMN_HEADINGS = [
  { id: "__heading:answers__", label: "ANSWERS" },
  { id: "__heading:between__", label: "LINKED THROUGH" },
  { id: "__heading:query__", label: "QUESTION" },
];

// brings the instance in line with the wanted elements; true when the
// graph's shape changed and it needs laying out again.
function syncElements(cy: Core, wanted: ElementDefinition[]): boolean {
  const byId = new Map(wanted.map((el) => [el.data.id as string, el]));
  // answers replacing the ghost grow out of the spot it held.
  const ghostNode = cy.getElementById(GHOST_ID);
  const ghostAt = ghostNode.nonempty() && !byId.has(GHOST_ID) ? { ...ghostNode.position() } : null;
  const neighbours = new Map<string, string[]>();
  for (const el of wanted) {
    if (el.group !== "edges") continue;
    const source = el.data.source as string;
    const target = el.data.target as string;
    neighbours.set(source, [...(neighbours.get(source) ?? []), target]);
    neighbours.set(target, [...(neighbours.get(target) ?? []), source]);
  }
  let changed = false;
  cy.batch(() => {
    const gone = cy.elements().filter((el) => {
      const next = byId.get(el.id());
      if (!next) return true;
      // a link whose representative fact now runs the other way is redrawn.
      return el.isEdge() && (el.data("source") !== next.data.source || el.data("target") !== next.data.target);
    });
    if (gone.nonempty()) {
      gone.remove();
      changed = true;
    }
    const center = cy.nodes().nonempty() ? cy.nodes().boundingBox({}) : { x1: 0, y1: 0, w: 0, h: 0 };
    let added = 0;
    for (const def of wanted) {
      const id = def.data.id as string;
      const existing = cy.getElementById(id);
      if (existing.nonempty()) {
        // id, source and target are fixed once an element exists.
        const rest: Record<string, unknown> = { ...def.data };
        delete rest.id;
        delete rest.source;
        delete rest.target;
        existing.data(rest);
        existing.classes(def.classes ?? "");
        continue;
      }
      changed = true;
      if (def.group === "nodes") {
        // a new node starts where it belongs: out of the ghost it
        // replaces, next to a node it links to that is already drawn,
        // or on a ring around the drawing, so the incremental layout
        // keeps the old nodes where they were.
        const angle = (2 * Math.PI * added++) / 8;
        const anchor =
          (def.data.role === "answer" ? ghostAt : null) ??
          (neighbours.get(id) ?? []).map((n) => cy.getElementById(n)).find((n) => n.nonempty())?.position() ??
          null;
        const reach = anchor ? (anchor === ghostAt ? 24 : 80) : 140;
        const origin = anchor ?? { x: center.x1 + center.w / 2, y: center.y1 + center.h / 2 };
        cy.add({ ...def, position: { x: origin.x + reach * Math.cos(angle), y: origin.y + reach * Math.sin(angle) } });
      } else {
        cy.add(def);
      }
    }
  });
  return changed;
}

function runLayout(cy: Core, fresh: boolean, mode: LayoutMode) {
  cy.scratch("orientNext", fresh);
  cy.scratch("layoutMode", mode);
  if (mode === "layered") {
    const drawn = cy.nodes().not(".heading");
    const font = (cy.scratch("figureFont") as string | undefined) ?? "sans-serif";
    const { positions, routes, top } = layeredLayout(
      drawn.map((n) => ({
        id: n.id(),
        role: n.data("role") as string,
        label: (n.data("label") as string | undefined) ?? n.id(),
        rank: (n.data("rank") as number | null | undefined) ?? null,
        extent: n.data("role") === "intermediate" ? labelBelowExtent(n as NodeSingular, font) : undefined,
      })),
      // links that make the same statement about the same entity run as
      // one line where they can.
      cy.edges().map((e) => ({
        source: e.data("source") as string,
        target: e.data("target") as string,
        bundle: e.hasClass("ghost") ? null : `${(e.data("label") as string | undefined) ?? ""}|${(e.data("strength") as string | undefined) ?? "pending"}`,
      })),
    );
    cy.scratch("routes", routes);
    placeHeadings(cy, drawn, positions, top);
    const options = {
      name: "preset",
      positions: (node: NodeSingular) => positions.get(node.id()) ?? node.position(),
      animate: !fresh,
      animationDuration: LAYOUT_EASE_MS,
      animationEasing: "ease-out-cubic",
      fit: false,
    };
    cy.layout(options as unknown as LayoutOptions).run();
    return;
  }
  const options = {
    name: "fcose",
    quality: "proof",
    // a fresh layout starts from a spectral embedding (deterministic with
    // greedy sampling, so a run always draws the same figure); an update
    // starts from where the nodes already are and eases into place.
    randomize: fresh,
    samplingType: false,
    animate: !fresh,
    animationDuration: LAYOUT_EASE_MS,
    animationEasing: "ease-out-cubic",
    // fitted after orientAndFit turns the drawing to the frame.
    fit: false,
    nodeDimensionsIncludeLabels: true,
    idealEdgeLength: 150,
    nodeRepulsion: 20000,
    edgeElasticity: 0.35,
    gravity: 0.15,
    numIter: 5000,
  };
  cy.layout(options as unknown as LayoutOptions).run();
}

// the links of the layered view as smooth curves, the way layered
// drawings route them: each leaves its source and enters its target
// horizontally, passes through its waypoints (lib/layeredLayout.ts)
// horizontally too, and bends between them; a link between two entities
// of one column bows out to the right. the links leaving one side of a
// node fan out a little, ordered by where they head, and links heading
// for the same waypoint leave from the same point, so a bundle merges
// into one line. the curve is a Cytoscape unbundled bezier: control
// points given as weights along, and distances across, the line between
// its endpoints, joined by quadratic pieces through the midpoints between
// them, so the curve passes exactly through the middle of every stretch
// between two anchors. the label goes there, on the stretch the layout
// chose, by offsetting it from the curve's middle, where Cytoscape puts it.
const CURVE_K = 0.38;
const FAN_STEP_DEG = 12;
// a label runs along its line, like a road's name on a map. it may sit
// at these places along a stretch (the middle of an S-curve is where it
// runs straightest), on a line no steeper than this.
const LABEL_SPOTS = [0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.1, 0.9, 0.04, 0.96];
const MAX_LABEL_ANGLE = (60 * Math.PI) / 180;
const PATH_SAMPLES = 40;
// the clearance a predicate label keeps from a node and its label, and
// the depth of the patch beside a circle where arrowheads arrive.
const NODE_CLEARANCE = 4;
const ARROW_ZONE = 18;
type LabelPath = { room: number; pts: Point[] };
type LabelChoice = { path: number; at: number; lift: number };

function shapeLinks(cy: Core) {
  if (cy.scratch("layoutMode") !== "layered") return;
  const routes = (cy.scratch("routes") as Map<string, Route> | undefined) ?? new Map<string, Route>();
  const routeOf = (edge: EdgeSingular) => routes.get(edgeKey(edge.data("source") as string, edge.data("target") as string));
  const edges = cy.edges();
  type End = { edgeId: string; end: "s" | "t"; side: "right" | "left"; toward: Point };
  const sides = new Map<string, End[]>();
  const inColumn = new Set<string>();
  const push = (key: string, end: End) => sides.set(key, [...(sides.get(key) ?? []), end]);
  edges.forEach((edge) => {
    const s = edge.source().position();
    const t = edge.target().position();
    const way = routeOf(edge)?.through ?? [];
    const level = way.length === 0 && Math.abs(t.x - s.x) < 1;
    if (level) inColumn.add(edge.id());
    const next = way[0] ?? t;
    const previous = way[way.length - 1] ?? s;
    const out = level || next.x >= s.x ? "right" : "left";
    const into = level ? "right" : previous.x <= t.x ? "left" : "right";
    push(`${edge.source().id()}|${out}`, { edgeId: edge.id(), end: "s", side: out, toward: next });
    push(`${edge.target().id()}|${into}`, { edgeId: edge.id(), end: "t", side: into, toward: previous });
  });
  // angles clockwise from 12 o'clock: 90 is the right side, 270 the left.
  const angle = new Map<string, number>();
  const spot = (p: Point) => `${Math.round(p.x)},${Math.round(p.y)}`;
  for (const list of sides.values()) {
    const spots = [...new Map(list.map((end) => [spot(end.toward), end.toward])).entries()]
      .sort(([, a], [, b]) => a.y - b.y || a.x - b.x)
      .map(([key]) => key);
    const step = spots.length > 1 ? Math.min(FAN_STEP_DEG, 60 / (spots.length - 1)) : 0;
    for (const end of list) {
      const offset = (spots.indexOf(spot(end.toward)) - (spots.length - 1) / 2) * step;
      angle.set(`${end.edgeId}|${end.end}`, end.side === "right" ? 90 + offset : 270 - offset);
    }
  }
  const border = (node: NodeSingular, deg: number): Point => {
    const p = node.position();
    const r = node.outerWidth() / 2;
    const a = (deg * Math.PI) / 180;
    return { x: p.x + r * Math.sin(a), y: p.y - r * Math.cos(a) };
  };
  const round = (v: number, digits: number) => Number(v.toFixed(digits));
  const middle = (p: Point, q: Point): Point => ({ x: (p.x + q.x) / 2, y: (p.y + q.y) / 2 });
  cy.batch(() => {
    edges.forEach((edge) => {
      const route = routeOf(edge);
      const sa = angle.get(`${edge.id()}|s`) ?? 90;
      const ta = angle.get(`${edge.id()}|t`) ?? 270;
      const e1 = border(edge.source(), sa);
      const e2 = border(edge.target(), ta);
      const lx = e2.x - e1.x;
      const ly = e2.y - e1.y;
      const length = Math.hypot(lx, ly);
      const shape = {
        sep: `${round(sa, 1)}deg`,
        tep: `${round(ta, 1)}deg`,
        bundleId: route?.bundle ?? edge.id(),
        shared: route?.shared ?? 1,
        follower: route ? !route.lead : false,
        trunk: route?.trunk ?? false,
        sharedFrom: route?.sharedFrom ?? null,
      };
      if (length < 1) {
        const mid = middle(e1, e2);
        edge.removeScratch("labelSpots");
        edge.data({ ...shape, cpd: "0", cpw: "0.5", lmx: 0, lmy: 0, lx: mid.x, ly: mid.y });
        return;
      }
      // two control points per stretch between anchors, and the room
      // each stretch gives a label (its width).
      const controls: Point[] = [];
      const rooms: number[] = [];
      if (inColumn.has(edge.id())) {
        const bow = Math.max(e1.x, e2.x) + Math.min(0.4 * COLUMN_GAP, 24 + 0.3 * Math.abs(ly));
        controls.push({ x: bow, y: e1.y }, { x: bow, y: e2.y });
        rooms.push(Math.abs(ly));
      } else {
        const anchors = [e1, ...(route?.through ?? []), e2];
        for (let i = 0; i + 1 < anchors.length; i++) {
          const a = anchors[i];
          const b = anchors[i + 1];
          const dx = b.x - a.x;
          controls.push({ x: a.x + CURVE_K * dx, y: a.y }, { x: b.x - CURVE_K * dx, y: b.y });
          rooms.push(Math.abs(dx));
        }
      }
      const stretches = controls.length / 2;
      const k = Math.min(route?.labelAt ?? 0, stretches - 1);
      // the curve as Cytoscape draws it: quadratic pieces from joint to
      // joint, the joints being the endpoints and the midpoints between
      // control points. a stretch is two pieces, its middle the joint
      // between them; the label may also slide along it (declutterLabels).
      const joints = [e1, ...controls.slice(1).map((c, i) => middle(controls[i], c)), e2];
      const along = (j: number, f: number): Point => {
        const piece = 2 * j + (f <= 0.5 ? 0 : 1);
        const t = f <= 0.5 ? 2 * f : 2 * f - 1;
        const [p0, p1, p2] = [joints[piece], controls[piece], joints[piece + 1]];
        const u = 1 - t;
        return { x: u * u * p0.x + 2 * u * t * p1.x + t * t * p2.x, y: u * u * p0.y + 2 * u * t * p1.y + t * t * p2.y };
      };
      // every stretch as a polyline, for declutterLabels to lay a label
      // along. the label stretch the layout chose comes first; a bundle's
      // first link at rest labels the whole bundle and keeps to it.
      const paths: LabelPath[] = rooms.map((room, j) => ({
        room,
        pts: Array.from({ length: PATH_SAMPLES + 1 }, (_, i) => along(j, i / PATH_SAMPLES)),
      }));
      const order = [k, ...rooms.map((_, j) => j).filter((j) => j !== k)];
      const forBundle = Boolean(route && route.shared > 1 && route.lead);
      const centre = middle(controls[stretches - 1], controls[stretches]);
      edge.scratch("labelPaths", { paths, order, forBundle, centre });
      // the place declutterLabels chose last, kept while the figure moves
      // (an eased layout, a drag) until it chooses again.
      const choice = edge.scratch("labelChoice") as LabelChoice | undefined;
      const pose = choice && paths[choice.path] ? poseAt(paths[choice.path].pts, choice.at, choice.lift) : { ...along(k, 0.5), angle: 0 };
      const label = pose;
      const nx = -ly / length;
      const ny = lx / length;
      const weights: number[] = [];
      const distances: number[] = [];
      for (const c of controls) {
        const vx = c.x - e1.x;
        const vy = c.y - e1.y;
        weights.push(round((vx * lx + vy * ly) / (length * length), 4));
        distances.push(round(vx * nx + vy * ny, 2));
      }
      edge.data({
        ...shape,
        cpd: distances.join(" "),
        cpw: weights.join(" "),
        lmx: round(label.x - centre.x, 2),
        lmy: round(label.y - centre.y, 2),
        lx: label.x,
        ly: label.y,
        lrot: round((label.angle * 180) / Math.PI, 1),
      });
    });
  });
}

// a label laid along a curve: its centre and the direction of its
// baseline (radians, kept between -90° and 90° so it never reads upside
// down), at sample `at` of a polyline, `lift` above the line.
type Pose = Point & { angle: number };

function tangentAt(pts: Point[], at: number): number {
  const a = pts[Math.max(0, at - 1)];
  const b = pts[Math.min(pts.length - 1, at + 1)];
  let angle = Math.atan2(b.y - a.y, b.x - a.x);
  if (angle > Math.PI / 2) angle -= Math.PI;
  else if (angle <= -Math.PI / 2) angle += Math.PI;
  return angle;
}

function poseAt(pts: Point[], at: number, lift: number): Pose {
  const angle = tangentAt(pts, at);
  const p = pts[at];
  // the side of the line a label sits on: up, turned with the line.
  return { x: p.x + Math.sin(angle) * lift, y: p.y - Math.cos(angle) * lift, angle };
}

// how a straight label of half width `half`, laid along the tangent at
// sample `at`, sits against its curved line: the most the line climbs
// toward the label's side within its width, and the most it falls away.
function fitAlong(pts: Point[], at: number, half: number): { rise: number; fall: number } {
  const angle = tangentAt(pts, at);
  const p = pts[at];
  const ux = Math.cos(angle);
  const uy = Math.sin(angle);
  let rise = 0;
  let fall = 0;
  for (const q of pts) {
    const along = (q.x - p.x) * ux + (q.y - p.y) * uy;
    if (Math.abs(along) > half) continue;
    const up = (q.x - p.x) * uy - (q.y - p.y) * ux;
    rise = Math.max(rise, up);
    fall = Math.min(fall, up);
  }
  return { rise, fall };
}

// an entity between the columns carries its label below it: the room it
// needs is the circle above its centre, and below it the circle, the gap
// and the wrapped label.
const BETWEEN_LABEL_MAX = 130;
const BETWEEN_FONT_SIZE = 11;

function labelBelowExtent(node: NodeSingular, font: string): { above: number; below: number } {
  const r = NODE_DIAMETER.intermediate / 2;
  const text = (node.data("display") as string | undefined) ?? (node.data("label") as string | undefined) ?? "";
  const lines = wrapText(text, BETWEEN_LABEL_MAX, `${BETWEEN_FONT_SIZE}px ${font}`).length;
  return { above: r + 12, below: r + 5 + lines * BETWEEN_FONT_SIZE * 1.3 + 12 };
}

// each column heading sits above its column, a little higher than the
// highest slot of any column; the middle one only when there are
// entities between the answers and the question.
function placeHeadings(cy: Core, drawn: Collection, positions: Map<string, { x: number; y: number }>, highest: number) {
  const at = (n: NodeSingular) => positions.get(n.id()) ?? n.position();
  const nodes = drawn.toArray() as NodeSingular[];
  if (nodes.length === 0) return;
  const top = highest - 14;
  const xsOf = (role: string) => nodes.filter((n) => n.data("role") === role).map((n) => at(n).x);
  const answers = xsOf("answer").concat(xsOf("ghost"));
  const between = xsOf("intermediate");
  const query = xsOf("query");
  const mean = (xs: number[]) => xs.reduce((sum, x) => sum + x, 0) / xs.length;
  const spots: Record<string, number | null> = {
    "__heading:answers__": answers.length > 0 ? mean(answers) : null,
    "__heading:between__": between.length > 0 ? (Math.min(...between) + Math.max(...between)) / 2 : null,
    "__heading:query__": query.length > 0 ? mean(query) : answers.length > 0 ? COLUMN_GAP : null,
  };
  cy.nodes(".heading").forEach((heading) => {
    const x = spots[heading.id()];
    heading.data("empty", x === null || x === undefined);
    positions.set(heading.id(), { x: x ?? 0, y: top });
  });
}

// a force-directed drawing has no preferred direction, so it is turned
// until its long axis runs along the frame's (principal axes of the node
// positions, as Graphviz does). a rotation keeps every distance and
// every crossing; it only lets the figure fill a wide frame.
function orientAndFit(cy: Core) {
  const nodes = cy.nodes();
  if (nodes.length > 2) {
    const points = nodes.map((n) => n.position());
    const mx = points.reduce((sum, p) => sum + p.x, 0) / points.length;
    const my = points.reduce((sum, p) => sum + p.y, 0) / points.length;
    let sxx = 0;
    let syy = 0;
    let sxy = 0;
    for (const p of points) {
      sxx += (p.x - mx) ** 2;
      syy += (p.y - my) ** 2;
      sxy += (p.x - mx) * (p.y - my);
    }
    const wide = cy.width() >= cy.height();
    const angle = 0.5 * Math.atan2(2 * sxy, sxx - syy) - (wide ? 0 : Math.PI / 2);
    const cos = Math.cos(-angle);
    const sin = Math.sin(-angle);
    nodes.positions((node) => {
      const { x, y } = node.position();
      return { x: mx + (x - mx) * cos - (y - my) * sin, y: my + (x - mx) * sin + (y - my) * cos };
    });
  }
  fitFigure(cy);
}

// fit the frame, but never blow a small figure up past maxFitZoom
// (1.25x in a page, 1.2x when the figure fills its own pane).
function fitFigure(cy: Core) {
  cy.fit(undefined, PADDING);
  const max = maxFitZoom(cy);
  if (cy.zoom() > max) {
    cy.zoom(max);
    cy.center();
  }
}

function maxFitZoom(cy: Core): number {
  return (cy.scratch("maxFitZoom") as number | undefined) ?? 1.25;
}

// the same fit, eased, after an incremental layout.
function easeToFit(cy: Core) {
  const box = cy.elements().boundingBox({});
  const zoom = Math.min(
    maxFitZoom(cy),
    (cy.width() - 2 * PADDING) / Math.max(box.w, 1),
    (cy.height() - 2 * PADDING) / Math.max(box.h, 1),
  );
  const pan = {
    x: cy.width() / 2 - zoom * (box.x1 + box.w / 2),
    y: cy.height() / 2 - zoom * (box.y1 + box.h / 2),
  };
  cy.animate({ zoom, pan }, { duration: 300, easing: "ease-out-cubic" });
}

function rememberPositions(cy: Core) {
  const key = cy.scratch("figureKey") as string | undefined;
  if (!key) return;
  const positions: Record<string, { x: number; y: number }> = {};
  cy.nodes().forEach((n) => {
    positions[n.id()] = { ...n.position() };
  });
  positionCache.set(key, positions);
}

const EDGE_FONT_SIZE = 10;
// edge labels wrap past this width (px, model coordinates).
const EDGE_LABEL_MAX = 150;

type Corners = [number, number][];

// which predicate labels show, and where. at rest, with the toggle on:
// every link's but the text-mined ones' and, in a bundle, all but its
// first link's. while part of the figure is lit (a hover, an answer
// chosen in the table): only the lit links', laid out for that part
// alone, the hovered link first, so the lit paths read on their own.
// strongest links go first; each label takes the first free spot along
// its stretch, clear of every node and node label and of the labels
// placed before it, or does not show at all (a hovered link's label
// always shows). runs in model coordinates, so zooming does not change
// the outcome.
function declutterLabels(cy: Core) {
  const font = `italic ${EDGE_FONT_SIZE}px ${(cy.scratch("figureFont") as string | undefined) ?? "sans-serif"}`;
  const focus = cy.scratch("labelFocus") as { ids: Set<string>; first: string | null } | null | undefined;
  // every node with its label, a little apart, and the patch either
  // side of its circle where arrowheads arrive.
  const taken: Corners[] = [];
  cy.nodes()
    .filter((n) => !n.data("empty"))
    .forEach((node) => {
      const b = node.boundingBox({ includeLabels: true, includeOverlays: false });
      if (node.hasClass("heading")) {
        taken.push(boxCorners(b));
        return;
      }
      const m = NODE_CLEARANCE;
      taken.push(boxCorners({ x1: b.x1 - m, y1: b.y1 - m, x2: b.x2 + m, y2: b.y2 + m }));
      const { x, y } = node.position();
      const r = node.outerWidth() / 2;
      taken.push(boxCorners({ x1: x - r - ARROW_ZONE, y1: y - r, x2: x - r, y2: y + r }));
      taken.push(boxCorners({ x1: x + r, y1: y - r, x2: x + r + ARROW_ZONE, y2: y + r }));
    });
  const rank = (s: Strength) => STRENGTH_ORDER.indexOf(s);
  const candidates = focus
    ? cy.edges().filter((e) => focus.ids.has(e.id()) && !e.hasClass("pending") && !e.hasClass("ghost") && !e.hasClass("hush"))
    : cy.scratch("predicates") === false
      ? cy.collection()
      : cy
          .edges()
          .not(".pending, .ghost, .weak")
          .filter((e) => !(e.hasClass("lr") && e.data("follower")));
  const firstId = focus?.first ?? null;
  const ordered = candidates.sort(
    (a, b) =>
      Number(b.id() === firstId) - Number(a.id() === firstId) ||
      rank(a.data("strength")) - rank(b.data("strength")) ||
      ((b.data("shared") as number | undefined) ?? 1) - ((a.data("shared") as number | undefined) ?? 1) ||
      b.data("nFacts") - a.data("nFacts"),
  );
  const round = (v: number) => Number(v.toFixed(2));
  cy.batch(() => {
    cy.edges().removeClass("speak");
    ordered.forEach((edge) => {
      const forced = edge.id() === firstId;
      // in the layered view the label runs along its curve, on one of the
      // stretches shapeLinks sampled; a straight link has it level, centred.
      const placed = edge.scratch("labelPaths") as
        | { paths: LabelPath[]; order: number[]; forBundle: boolean; centre: Point }
        | undefined;
      const shaped = edge.hasClass("lr") && placed !== undefined;
      const label = (edge.data("label") as string | undefined) ?? "";
      if (!label) return;
      const lines = wrapText(label, EDGE_LABEL_MAX, font);
      const width = Math.max(...lines.map((line) => measureText(line, font))) + 6;
      const height = lines.length * EDGE_FONT_SIZE * 1.25 + 3;
      // a rotated box: centre, baseline direction, size.
      const boxOf = (c: Pose) => {
        const ux = Math.cos(c.angle) * (width / 2);
        const uy = Math.sin(c.angle) * (width / 2);
        const vx = -Math.sin(c.angle) * (height / 2);
        const vy = Math.cos(c.angle) * (height / 2);
        return [
          [c.x - ux - vx, c.y - uy - vy],
          [c.x + ux - vx, c.y + uy - vy],
          [c.x + ux + vx, c.y + uy + vy],
          [c.x - ux + vx, c.y - uy + vy],
        ] as Corners;
      };
      const free = (box: Corners) => !taken.some((other) => overlaps(other, box));
      if (!shaped) {
        // a straight link: a level label at its middle.
        const s = edge.sourceEndpoint();
        const t = edge.targetEndpoint();
        const box = boxOf({ ...edge.midpoint(), angle: 0 });
        if (!forced && (Math.hypot(t.x - s.x, t.y - s.y) < 0.7 * Math.min(width, 60) || !free(box))) return;
        taken.push(box);
        edge.addClass("speak");
        return;
      }
      // every place along the allowed stretches, best first: straight and
      // gentle, near the middle, on the stretch the layout chose.
      const stretches = focus || !placed.forBundle ? placed.order : placed.order.slice(0, 1);
      type Option = LabelChoice & { pose: Pose; box: Corners; score: number };
      const options: Option[] = [];
      stretches.forEach((path, rankOfPath) => {
        const { pts, room } = placed.paths[path];
        if (room < 0.7 * Math.min(width, 60) && !forced) return;
        for (const f of LABEL_SPOTS) {
          const at = Math.round(f * PATH_SAMPLES);
          const angle = tangentAt(pts, at);
          if (Math.abs(angle) > MAX_LABEL_ANGLE) continue;
          const { rise, fall } = fitAlong(pts, at, width / 2);
          const lift = height / 2 + 1.5 + rise;
          const pose = poseAt(pts, at, lift);
          const score =
            Math.max(0, Math.abs(angle) - 0.2) * 1.5 + Math.abs(f - 0.5) * 1.6 + rise / height + Math.max(0, -fall - height) / height + rankOfPath * 0.6;
          options.push({ path, at, lift, pose, box: boxOf(pose), score });
        }
      });
      options.sort((a, b) => a.score - b.score);
      const pick = options.find((o) => free(o.box)) ?? (forced ? options[0] : undefined);
      if (!pick) return;
      taken.push(pick.box);
      edge.addClass("speak");
      edge.scratch("labelChoice", { path: pick.path, at: pick.at, lift: pick.lift });
      edge.data({
        lx: pick.pose.x,
        ly: pick.pose.y,
        lmx: round(pick.pose.x - placed.centre.x),
        lmy: round(pick.pose.y - placed.centre.y),
        lrot: Number(((pick.pose.angle * 180) / Math.PI).toFixed(1)),
      });
    });
  });
}

function boxCorners(box: { x1: number; y1: number; x2: number; y2: number }): Corners {
  return [
    [box.x1, box.y1],
    [box.x2, box.y1],
    [box.x2, box.y2],
    [box.x1, box.y2],
  ];
}

// separating-axis test for two convex quadrilaterals.
function overlaps(a: Corners, b: Corners): boolean {
  for (const shape of [a, b]) {
    for (let i = 0; i < shape.length; i++) {
      const [x1, y1] = shape[i];
      const [x2, y2] = shape[(i + 1) % shape.length];
      const nx = y2 - y1;
      const ny = x1 - x2;
      const project = (pts: Corners) => pts.map(([x, y]) => x * nx + y * ny);
      const pa = project(a);
      const pb = project(b);
      if (Math.max(...pa) < Math.min(...pb) || Math.max(...pb) < Math.min(...pa)) return false;
    }
  }
  return true;
}

// Cytoscape's texture caches, made for graphs of thousands of elements,
// redraw cached bitmaps of nodes and labels at other scales, which shows
// as jagged edges on circles and text. a figure of a few dozen elements
// is drawn straight to the canvas every frame instead: with no cached
// texture, Cytoscape falls back to drawing each element directly. this
// reaches into the renderer (cytoscape 3.34), and does nothing if its
// insides change.
type TextureCache = { getElement?: () => null; getLayers?: () => null };

function drawDirect(cy: Core) {
  const renderer = (cy as unknown as { renderer?: () => { data?: Record<string, TextureCache | undefined> } }).renderer?.();
  const data = renderer?.data;
  if (!data) return;
  for (const name of ["eleTxrCache", "lblTxrCache", "slbTxrCache", "tlbTxrCache"]) {
    const cache = data[name];
    if (cache && typeof cache.getElement === "function") cache.getElement = () => null;
  }
  const layers = data.lyrTxrCache;
  if (layers && typeof layers.getLayers === "function") layers.getLayers = () => null;
}

function zoomBy(cy: Core, factor: number, at?: { x: number; y: number }) {
  const level = Math.min(cy.maxZoom(), Math.max(cy.minZoom(), cy.zoom() * factor));
  cy.zoom({ level, renderedPosition: at ?? { x: cy.width() / 2, y: cy.height() / 2 } });
}

type StyleBlock = { selector: string; style: Record<string, unknown> };

function figureStyle(p: VizPalette, font: string): StylesheetJson {
  const kind = (node: NodeSingular) => node.data("kind") as Kind;
  const diameter = (n: NodeSingular) => NODE_DIAMETER[n.data("role") as keyof typeof NODE_DIAMETER] ?? NODE_DIAMETER.answer;
  const label = {
    "font-family": font,
    "text-outline-color": p.surface,
    "text-outline-width": 2.5,
    "text-outline-opacity": 1,
    "overlay-opacity": 0,
    "transition-property": "opacity",
    "transition-duration": 150,
  };
  const blocks: StyleBlock[] = [
    { selector: "core", style: { "active-bg-opacity": 0, "selection-box-opacity": 0 } },
    {
      selector: "node",
      style: {
        ...label,
        width: diameter,
        height: diameter,
        shape: "ellipse",
        "background-color": (n: NodeSingular) => kindFill(kind(n), p),
        "background-image": (n: NodeSingular) => iconDataUri(kind(n), iconColor(kind(n), p)),
        "background-width": "56%",
        "background-height": "56%",
        "background-fit": "none",
        // no ring: the circle's own edge, drawn smooth, meets the page.
        "border-width": 0,
        label: "data(display)",
        "font-size": 12,
        color: p.ink,
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 7,
        "text-wrap": "wrap",
        "text-max-width": "140px",
        "min-zoomed-font-size": 6,
      },
    },
    { selector: "node.other", style: { "border-width": 1.25, "border-color": p.neutral } },
    { selector: "node.intermediate", style: { "font-size": BETWEEN_FONT_SIZE, color: p.ink2, "text-max-width": `${BETWEEN_LABEL_MAX}px` } },
    { selector: "node.query", style: { "font-size": 13, "font-weight": 600 } },
    // a selection is a soft halo in the selection hue, not a heavy ring.
    {
      selector: "node:selected",
      style: { "underlay-color": p.focus, "underlay-opacity": 0.2, "underlay-padding": 5, "underlay-shape": "ellipse" },
    },
    {
      selector: "edge",
      style: {
        ...label,
        width: "data(width)",
        "curve-style": "straight",
        "line-color": p.edge,
        "target-arrow-shape": "triangle",
        "target-arrow-color": p.edge,
        "arrow-scale": 0.72,
        "source-distance-from-node": 2,
        "target-distance-from-node": 2,
        // a casing of the page colour, so a line crossing another passes
        // over it, as on a map, instead of merging into one dark knot.
        "line-outline-width": 2.5,
        "line-outline-color": p.surface,
        // shown where declutterLabels finds room (class "speak").
        label: "",
        "font-size": EDGE_FONT_SIZE,
        "font-style": "italic",
        color: p.ink2,
        // labels stay level and wrap, so they read without tilting the head.
        "text-rotation": "none",
        "text-wrap": "wrap",
        "text-max-width": `${EDGE_LABEL_MAX}px`,
        "min-zoomed-font-size": 5,
      },
    },
    {
      selector: "edge.lr",
      style: {
        "curve-style": "unbundled-bezier",
        "edge-distances": "endpoints",
        "control-point-distances": (e: EdgeSingular) => (e.data("cpd") as string | undefined) ?? "0",
        "control-point-weights": (e: EdgeSingular) => (e.data("cpw") as string | undefined) ?? "0.5",
        "source-endpoint": (e: EdgeSingular) => (e.data("sep") as string | undefined) ?? "90deg",
        "target-endpoint": (e: EdgeSingular) => (e.data("tep") as string | undefined) ?? "270deg",
        "text-margin-x": (e: EdgeSingular) => (e.data("lmx") as number | undefined) ?? 0,
        "text-margin-y": (e: EdgeSingular) => (e.data("lmy") as number | undefined) ?? 0,
        // along its line (declutterLabels).
        "text-rotation": (e: EdgeSingular) => `${(e.data("lrot") as number | undefined) ?? 0}deg`,
      },
    },
    { selector: "edge.inferred", style: { "line-style": "dashed", "line-dash-pattern": [7, 3] } },
    { selector: "edge.statistical", style: { "line-style": "dashed", "line-dash-pattern": [3, 3] } },
    // text-mined links are the least certain and the most numerous: their
    // predicate shows on hover only.
    { selector: "edge.weak", style: { "line-style": "dashed", "line-dash-pattern": [1.5, 2.5] } },
    {
      selector: "node.ghost",
      style: {
        width: NODE_DIAMETER.answer,
        height: NODE_DIAMETER.answer,
        "background-opacity": 0,
        "background-image-opacity": 0,
        "border-width": 1.5,
        "border-style": "dashed",
        "border-color": p.muted,
        color: p.ink2,
        "font-style": "italic",
      },
    },
    // left-to-right drawing: an answer's label sits to its left and the
    // question's to its right, outside the drawing, so links leave both
    // columns without crossing text. entities between keep theirs below.
    {
      selector: "node.answer.lr, node.ghost.lr",
      style: { "text-halign": "left", "text-valign": "center", "text-margin-x": -9, "text-margin-y": 0, "text-max-width": "200px" },
    },
    {
      selector: "node.query.lr",
      style: { "text-halign": "right", "text-valign": "center", "text-margin-x": 11, "text-margin-y": 0, "text-max-width": "160px" },
    },
    // the column headings of the layered view: text only, not interactive.
    {
      selector: "node.heading",
      style: {
        width: 1,
        height: 1,
        "background-opacity": 0,
        "background-image-opacity": 0,
        "border-width": 0,
        "font-size": 10,
        "font-weight": 600,
        color: p.muted,
        "text-valign": "center",
        "text-margin-y": 0,
        "text-outline-width": 0,
        events: "no",
      },
    },
    // data, not a class: syncElements resets classes on every update.
    { selector: "node.heading[?empty]", style: { display: "none" } },
    {
      selector: "edge.ghost",
      style: {
        width: 1.25,
        "line-style": "dashed",
        "line-dash-pattern": [4, 3],
        "line-color": p.muted,
        "target-arrow-color": p.muted,
        label: "data(label)",
      },
    },
    {
      selector: "edge.pending",
      style: { width: 1, "line-style": "dashed", "line-dash-pattern": [1.5, 3], "target-arrow-shape": "none", label: "" },
    },
    // links whose label shows are drawn above those without one, so no
    // line runs over a label (a label is drawn with its own link).
    { selector: "edge", style: { "z-index": 1 } },
    { selector: "edge.speak", style: { label: "data(label)", "z-index": 2 } },

    // a lit link turns from the rest grey to a dark grey, not black;
    // its casing keeps crossing lit links apart.
    {
      selector: "edge.active, edge:selected",
      style: { "line-color": p.ink2, "target-arrow-color": p.ink2, color: p.ink, "z-index": 10 },
    },
    { selector: "edge.active.speak", style: { "z-index": 11 } },
    { selector: "edge:selected", style: { "underlay-color": p.focus, "underlay-opacity": 0.18, "underlay-padding": 3 } },
    { selector: "node.faded", style: { opacity: 0.2 } },
    { selector: "edge.faded", style: { opacity: 0.1 } },
  ];
  return blocks as unknown as StylesheetJson;
}

// --------------------------------------------------------------- chrome

// the figure's toolbar controls, one height and one vocabulary: a
// segmented control picks one of a few views, a switch turns a setting
// on or off, plain buttons act, and buttons that belong together sit in
// one bordered group.
const CONTROL = "h-[22px] text-[11px] leading-none";

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string; title: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <span
      role="radiogroup"
      aria-label={label}
      className={`${CONTROL} inline-flex items-stretch gap-px rounded-md border border-zinc-200 bg-zinc-100 p-px dark:border-zinc-700 dark:bg-zinc-800/80`}
    >
      {options.map((option) => {
        const chosen = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={chosen}
            title={option.title}
            onClick={() => onChange(option.value)}
            className={`rounded-[5px] px-2 transition-colors ${
              chosen
                ? "bg-white text-zinc-900 shadow-[0_1px_1.5px_rgba(0,0,0,0.08)] dark:bg-zinc-950 dark:text-zinc-100"
                : "text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200"
            }`}
          >
            {option.label}
          </button>
        );
      })}
    </span>
  );
}

function Switch({ checked, onChange, label, title }: { checked: boolean; onChange: (on: boolean) => void; label: string; title: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      title={title}
      onClick={() => onChange(!checked)}
      className={`${CONTROL} group inline-flex items-center gap-1.5 text-zinc-600 hover:text-zinc-900 dark:text-zinc-300 dark:hover:text-zinc-100`}
    >
      <span
        aria-hidden
        className={`relative inline-block h-[14px] w-[24px] rounded-full transition-colors ${
          checked ? "bg-zinc-800 dark:bg-zinc-200" : "bg-zinc-300 dark:bg-zinc-600"
        }`}
      >
        <span
          className={`absolute top-[2px] h-[10px] w-[10px] rounded-full bg-white shadow-[0_1px_1px_rgba(0,0,0,0.2)] transition-[left] dark:bg-zinc-900 ${
            checked ? "left-[12px]" : "left-[2px]"
          }`}
        />
      </span>
      {label}
    </button>
  );
}

function ButtonGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span
      role="group"
      aria-label={label}
      className={`${CONTROL} inline-flex items-stretch divide-x divide-zinc-200 overflow-hidden rounded-md border border-zinc-200 dark:divide-zinc-700 dark:border-zinc-700`}
    >
      {children}
    </span>
  );
}

function ToolButton({
  children,
  onClick,
  title,
  aria,
  bordered = false,
}: {
  children: React.ReactNode;
  onClick: () => void;
  title: string;
  // the accessible name, for a button that shows an icon.
  aria?: string;
  // a button standing on its own rather than in a group.
  bordered?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={aria}
      className={`inline-flex min-w-[22px] items-center justify-center px-1.5 text-zinc-600 transition-colors hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800 dark:hover:text-zinc-100 ${
        bordered ? `${CONTROL} rounded-md border border-zinc-200 dark:border-zinc-700` : ""
      }`}
    >
      {children}
    </button>
  );
}

function Icon({ d }: { d: string }) {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

function Tooltip({ tip }: { tip: NonNullable<Tip> }) {
  return (
    <div
      className="pointer-events-none absolute z-10 max-w-[280px] rounded border px-2 py-1.5 text-[11px] leading-snug"
      style={{
        left: tip.x + 14,
        top: tip.y + 14,
        background: "var(--viz-surface)",
        borderColor: "var(--viz-rule)",
        color: "var(--viz-ink-2)",
      }}
    >
      <div className="font-semibold" style={{ color: "var(--viz-ink)" }}>
        {tip.title}
      </div>
      {tip.lines.map((line) => (
        <div key={line}>{line}</div>
      ))}
    </div>
  );
}

// the node symbol for the legend, the query diagram and the detail
// panel: the canvas's circle, colour and icon.
const KIND_FILL_VAR: Record<Kind, string> = {
  chemical: "var(--viz-chemical)",
  disease: "var(--viz-disease)",
  gene: "var(--viz-gene)",
  process: "var(--viz-neutral)",
  anatomy: "var(--viz-neutral)",
  other: "var(--viz-surface)",
};

export function Glyph({ kind, size = 14 }: { kind: Kind; size?: number }) {
  const ring = kind === "other" ? 1.25 : 0;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden className="shrink-0 overflow-visible">
      <circle
        cx="12"
        cy="12"
        r={12 - ring}
        fill={KIND_FILL_VAR[kind]}
        stroke={kind === "other" ? "var(--viz-neutral)" : "none"}
        strokeWidth={ring * (24 / size) * 1.4}
      />
      <g
        transform="translate(4.8 4.8) scale(0.6)"
        fill="none"
        stroke={kind === "other" ? "var(--viz-ink-2)" : "#ffffff"}
        strokeWidth={2.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {iconParts(kind).map((part, i) =>
          "d" in part ? <path key={i} d={part.d} /> : <circle key={i} cx={part.cx} cy={part.cy} r={part.r} />,
        )}
      </g>
    </svg>
  );
}

// ---------------------------------------------------------- detail panel

function DetailPanel({
  selection,
  graph,
  links,
  labels,
  onClose,
}: {
  selection: NonNullable<Selection>;
  graph: Graph;
  links: LinkSpec[];
  labels: Map<string, string>;
  onClose: () => void;
}) {
  return (
    <aside
      className="absolute bottom-2 right-2 top-2 z-20 w-80 overflow-y-auto rounded-md border p-4 text-[12px]"
      style={{ background: "var(--viz-surface)", borderColor: "var(--viz-rule)", color: "var(--viz-ink)" }}
    >
      <button
        type="button"
        onClick={onClose}
        className="absolute right-2 top-2 px-1.5 text-[12px] hover:underline"
        style={{ color: "var(--viz-muted)" }}
        aria-label="Close details"
      >
        close
      </button>
      {selection.kind === "link" ? (
        <LinkDetails link={links.find((l) => l.id === selection.id)} labels={labels} />
      ) : (
        <NodeDetails node={graph.nodes.find((n) => n.id === selection.id)} graph={graph} labels={labels} />
      )}
    </aside>
  );
}

// the same details as the panel over the figure, for a page that shows
// them beside the figure (the onSelect prop).
export function SelectionDetails({ selection, graph }: { selection: NonNullable<Selection>; graph: Graph }) {
  const links = useMemo(() => buildLinks(graph), [graph]);
  const labels = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n.label])), [graph]);
  return (
    <div className="text-[12px]" style={{ color: "var(--viz-ink)" }}>
      {selection.kind === "link" ? (
        <LinkDetails link={links.find((l) => l.id === selection.id)} labels={labels} />
      ) : (
        <NodeDetails node={graph.nodes.find((n) => n.id === selection.id)} graph={graph} labels={labels} />
      )}
    </div>
  );
}

function Heading({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-wide" style={{ color: "var(--viz-muted)" }}>
      {children}
    </div>
  );
}

function LinkDetails({ link, labels }: { link: LinkSpec | undefined; labels: Map<string, string> }) {
  if (!link) return null;
  return (
    <div className="flex flex-col gap-3 pr-8">
      <div>
        <Heading>link</Heading>
        <div className="mt-1 text-[13px] font-semibold">
          {labels.get(link.source)} → {labels.get(link.target)}
        </div>
        <div className="mt-0.5" style={{ color: "var(--viz-ink-2)" }}>
          {link.facts.length} fact{link.facts.length === 1 ? "" : "s"} join these two entities
        </div>
      </div>
      <table className="w-full border-collapse text-left">
        <tbody>
          {link.facts.map((fact) => (
            <FactRow key={fact.id} fact={fact} labels={labels} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FactRow({ fact, labels }: { fact: ReasoningFact; labels: Map<string, string> }) {
  return (
    <tr className="border-t align-top" style={{ borderColor: "var(--viz-rule)" }}>
      <td className="py-2 pr-2 font-mono tabular-nums" style={{ color: "var(--viz-muted)" }}>
        {fact.fact_id}
      </td>
      <td className="py-2">
        <div>
          <span style={{ color: "var(--viz-ink-2)" }}>{labels.get(fact.source) ?? fact.source}</span>{" "}
          <em>{humanPredicate(fact.predicate)}</em>{" "}
          <span style={{ color: "var(--viz-ink-2)" }}>{labels.get(fact.target) ?? fact.target}</span>
        </div>
        <div className="mt-0.5" style={{ color: "var(--viz-muted)" }}>
          {prettySource(fact.primary_source)} · {(fact.knowledge_level ?? "not recorded").replaceAll("_", " ")}
          {fact.agent_type ? ` · ${fact.agent_type.replaceAll("_", " ")}` : ""}
        </div>
        {fact.publications.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-x-2">
            {fact.publications.map((pub) => (
              <a
                key={pub}
                href={publicationUrl(pub)}
                target="_blank"
                rel="noopener noreferrer"
                className="font-mono underline decoration-dotted underline-offset-2"
                style={{ color: "var(--viz-ink-2)" }}
              >
                {pub}
              </a>
            ))}
          </div>
        )}
      </td>
    </tr>
  );
}

function NodeDetails({
  node,
  graph,
  labels,
}: {
  node: ReasoningNode | undefined;
  graph: Graph;
  labels: Map<string, string>;
}) {
  if (!node) return null;
  const paths = node.role === "answer" ? graph.paths[node.id] ?? [] : [];
  const onPathsOf =
    node.role === "intermediate"
      ? Object.entries(graph.paths).filter(([, ps]) => ps.some((p) => p.includes(node.id))).map(([a]) => a)
      : [];
  return (
    <div className="flex flex-col gap-3 pr-8">
      <div>
        <Heading>{node.role === "query" ? "question entity" : node.role}</Heading>
        <div className="mt-1 flex items-center gap-2 text-[13px] font-semibold">
          <Glyph kind={kindOf(node.category)} />
          {node.label}
        </div>
        <div className="mt-0.5" style={{ color: "var(--viz-ink-2)" }}>
          {(node.category ?? "unknown").replace(/^biolink:/, "")} ·{" "}
          <a
            href={`https://bioregistry.io/${node.id}`}
            target="_blank"
            rel="noreferrer"
            className="font-mono underline decoration-dotted underline-offset-2"
          >
            {node.id}
          </a>
        </div>
      </div>
      {paths.length > 0 && (
        <div>
          <Heading>reasoning paths shown</Heading>
          <ol className="mt-1 list-decimal pl-4" style={{ color: "var(--viz-ink-2)" }}>
            {paths.map((path) => (
              <li key={path.join(">")} className="py-0.5">
                {path.map((id) => labels.get(id) ?? id).join(" – ")}
              </li>
            ))}
          </ol>
        </div>
      )}
      {onPathsOf.length > 0 && (
        <div style={{ color: "var(--viz-ink-2)" }}>
          On the reasoning paths of {onPathsOf.map((a) => labels.get(a) ?? a).join(", ")}.
        </div>
      )}
    </div>
  );
}

function prettySource(source: string | null): string {
  return source ? source.replace(/^infores:/, "") : "source not recorded";
}

function publicationUrl(pub: string): string {
  const pmid = pub.match(/^PMID:(\d+)$/i);
  if (pmid) return `https://pubmed.ncbi.nlm.nih.gov/${pmid[1]}/`;
  const pmc = pub.match(/^PMC:?(\d+)$/i);
  if (pmc) return `https://www.ncbi.nlm.nih.gov/pmc/articles/PMC${pmc[1]}/`;
  return `https://www.google.com/search?q=${encodeURIComponent(pub)}`;
}

function slug(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "figure";
}
