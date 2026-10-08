// deterministic left-to-right layout for the reasoning figure, in the
// manner of layered graph drawing (Sugiyama et al. 1981; Graphviz dot):
//   columns — the answers on the left in ARAX rank order, the question's
//     entity alone on the right, every entity between them in a column
//     by its distance (in links) to the question;
//   waypoints — a link that skips columns gets a waypoint in each column
//     it crosses, holding a slot there like a node, so the link passes
//     between that column's entities instead of through them. links that
//     end at the same entity with the same statement share their
//     waypoints and run as one line from the first column they share (a
//     confluent bundle), under one label;
//   order — within a column, by the mean position of each slot's
//     neighbours (the barycenter heuristic), swept left to right and back,
//     keeping the order with the fewest crossings;
//   rows — the answers stacked evenly in rank order; every other slot
//     pulled toward the height of its neighbours, so long links run
//     level, without changing the order or crowding a column (a weighted
//     least-squares fit under spacing constraints).
// nothing is random: the same graph always gets the same picture.

export type LayeredNode = {
  id: string;
  // "query" | "answer" | "intermediate", or "ghost" for the live
  // stand-in answer, which sits in the answers' column.
  role: string;
  label: string;
  // ARAX rank for answers; null for everything else.
  rank: number | null;
  // the room the node and its label need above and below its centre;
  // half a row each way when not given.
  extent?: { above: number; below: number };
};

export type LayeredEdge = {
  source: string;
  target: string;
  // links with the same bundle key (the caller passes the statement) that
  // end at the same entity share their waypoints; null keeps a link on
  // its own.
  bundle?: string | null;
};

export type Point = { x: number; y: number };

export type Route = {
  // the points the link passes through between its ends, source to target.
  through: Point[];
  // the label sits halfway along this stretch of [source, ...through, target].
  labelAt: number;
  // the bundle the link runs in, and how many links run in it (1 = alone).
  bundle: string;
  shared: number;
  // the one link of a bundle that carries the label.
  lead: boolean;
  // whether the link runs along its bundle's trunk, the stretch shared by
  // the bundle's links that skip columns, where the label usually sits.
  trunk: boolean;
  // for a bundle of two or more: x from which its links run as one.
  sharedFrom: number | null;
};

export type LayeredResult = {
  positions: Map<string, Point>;
  // per link (edgeKey).
  routes: Map<string, Route>;
  // the highest point any slot needs, for the column headings.
  top: number;
};

// the gap between columns: wider when there are fewer, so a short figure
// keeps room for its labels.
export const COLUMN_GAP = 170;
export function columnGapFor(columns: number): number {
  return columns <= 2 ? 270 : columns === 3 ? 200 : COLUMN_GAP;
}
export const ROW_GAP = 60;
// a waypoint's slot only holds a line and perhaps its label.
export const WAYPOINT_GAP = 30;
// a waypoint resists being moved off its line more than a node does, so
// long links straighten first (the priority method).
const WAYPOINT_WEIGHT = 3;
const ORDER_SWEEPS = 8;
// a bundle's merge point: this far before its target, in column gaps,
// and at least this far from another merge point into the same target.
const MERGE_AT = 0.42;
const MERGE_GAP = 16;
const ROW_PASSES = 16;

export function edgeKey(source: string, target: string): string {
  return `${source}→${target}`;
}

type Slot = { id: string; label: string; above: number; below: number; waypoint: boolean };

type Span = {
  key: string;
  // the end in the lower and in the higher column.
  lo: string;
  hi: string;
  loCol: number;
  hiCol: number;
  bundle: string;
  // lo, the waypoints, hi.
  chain: string[];
  // whether the link runs from lo to hi.
  forward: boolean;
};

export function layeredLayout(
  nodes: LayeredNode[],
  edges: LayeredEdge[],
  { columnGap: fixedGap, rowGap = ROW_GAP, waypointGap = WAYPOINT_GAP }: { columnGap?: number; rowGap?: number; waypointGap?: number } = {},
): LayeredResult {
  const known = new Set(nodes.map((n) => n.id));
  const links = edges.filter((e) => known.has(e.source) && known.has(e.target) && e.source !== e.target);
  const neighbours = new Map<string, string[]>(nodes.map((n) => [n.id, []]));
  for (const link of links) {
    neighbours.get(link.source)?.push(link.target);
    neighbours.get(link.target)?.push(link.source);
  }

  const byLabel = (a: { label: string; id: string }, b: { label: string; id: string }) =>
    a.label.localeCompare(b.label) || a.id.localeCompare(b.id);
  const query = nodes.find((n) => n.role === "query") ?? null;
  const isAnswer = (n: LayeredNode) => n.role === "answer" || n.role === "ghost";
  const answers = nodes
    .filter(isAnswer)
    .sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity) || byLabel(a, b));
  const between = nodes.filter((n) => n !== query && !isAnswer(n));

  // hops from the question's entity, ignoring direction.
  const distance = new Map<string, number>();
  if (query) {
    distance.set(query.id, 0);
    const queue = [query.id];
    for (let head = 0; head < queue.length; head++) {
      const id = queue[head];
      for (const next of neighbours.get(id) ?? []) {
        if (distance.has(next)) continue;
        distance.set(next, (distance.get(id) ?? 0) + 1);
        queue.push(next);
      }
    }
  }
  const far = (n: LayeredNode) => distance.get(n.id) ?? 1;
  const last = Math.max(1, ...answers.map(far), ...between.map((n) => far(n) + 1));

  // the column of every node.
  const column = new Map<string, number>();
  for (const node of answers) column.set(node.id, 0);
  for (const node of between) {
    const d = distance.get(node.id);
    const c = d === undefined ? Math.ceil(last / 2) : last - d;
    column.set(node.id, Math.min(last - 1, Math.max(1, c)));
  }
  if (query) column.set(query.id, last);
  const columnGap = fixedGap ?? columnGapFor(last + 1);

  // slots: the nodes, then the waypoints of every link that skips columns.
  const slotOf = new Map<string, Slot>();
  const columnOf = new Map<string, number>();
  const order: string[][] = Array.from({ length: last + 1 }, () => []);
  const addSlot = (slot: Slot, c: number) => {
    slotOf.set(slot.id, slot);
    columnOf.set(slot.id, c);
    order[c].push(slot.id);
  };
  for (const node of nodes) {
    const half = rowGap / 2;
    const { above, below } = node.extent ?? { above: half, below: half };
    addSlot({ id: node.id, label: node.label, above, below, waypoint: false }, column.get(node.id) ?? 0);
  }
  const spans: Span[] = [];
  for (const link of links) {
    const cs = column.get(link.source) ?? 0;
    const ct = column.get(link.target) ?? 0;
    const forward = cs <= ct;
    const [lo, hi] = forward ? [link.source, link.target] : [link.target, link.source];
    const [loCol, hiCol] = forward ? [cs, ct] : [ct, cs];
    const key = edgeKey(link.source, link.target);
    // links within one column stay on their own.
    const bundle = link.bundle && hiCol > loCol ? `${link.bundle}|${hi}|${forward ? ">" : "<"}` : key;
    const chain = [lo];
    for (let c = loCol + 1; c < hiCol; c++) {
      const id = `~${bundle}~${c}`;
      if (!slotOf.has(id)) addSlot({ id, label: "￿", above: waypointGap / 2, below: waypointGap / 2, waypoint: true }, c);
      chain.push(id);
    }
    chain.push(hi);
    spans.push({ key, lo, hi, loCol, hiCol, bundle, chain, forward });
  }

  // the stretches between neighbouring columns, each once (a bundle's
  // shared stretch is one line), and every slot's neighbours on each side.
  const segments: [string, string][][] = Array.from({ length: last }, () => []);
  const leftOf = new Map<string, string[]>();
  const rightOf = new Map<string, string[]>();
  const seen = new Set<string>();
  for (const span of spans) {
    if (span.loCol === span.hiCol) continue;
    for (let i = 0; i + 1 < span.chain.length; i++) {
      const [a, b] = [span.chain[i], span.chain[i + 1]];
      if (seen.has(`${a}|${b}`)) continue;
      seen.add(`${a}|${b}`);
      segments[columnOf.get(a) ?? 0].push([a, b]);
      rightOf.set(a, [...(rightOf.get(a) ?? []), b]);
      leftOf.set(b, [...(leftOf.get(b) ?? []), a]);
    }
  }

  // order. column 0 holds the answers by rank and the last the question;
  // the columns between start by the barycenter of their left neighbours.
  const answerOrder = new Map(answers.map((a, i) => [a.id, i]));
  order[0].sort((a, b) => (answerOrder.get(a) ?? Infinity) - (answerOrder.get(b) ?? Infinity));
  const sweep = (c: number, side: "left" | "right") => {
    const other = order[side === "left" ? c - 1 : c + 1];
    const near = side === "left" ? leftOf : rightOf;
    const at = new Map(other.map((id, i) => [id, other.length > 1 ? i / (other.length - 1) : 0.5]));
    const col = order[c];
    const keyed = col.map((id, i) => {
      const rows = (near.get(id) ?? []).filter((n) => at.has(n)).map((n) => at.get(n) as number);
      const k = rows.length > 0 ? rows.reduce((s, r) => s + r, 0) / rows.length : col.length > 1 ? i / (col.length - 1) : 0.5;
      return { id, k, i };
    });
    order[c] = keyed.sort((a, b) => a.k - b.k || a.i - b.i).map((e) => e.id);
  };
  for (let c = 1; c < last; c++) {
    order[c].sort((a, b) => {
      const sa = slotOf.get(a) as Slot;
      const sb = slotOf.get(b) as Slot;
      return byLabel(sa, sb);
    });
    sweep(c, "left");
  }
  const crossings = () => {
    const pos = new Map<string, number>();
    order.forEach((col) => col.forEach((id, i) => pos.set(id, i)));
    let total = 0;
    for (const list of segments) {
      for (let i = 0; i < list.length; i++) {
        for (let j = i + 1; j < list.length; j++) {
          const da = (pos.get(list[i][0]) ?? 0) - (pos.get(list[j][0]) ?? 0);
          const db = (pos.get(list[i][1]) ?? 0) - (pos.get(list[j][1]) ?? 0);
          if (da * db < 0) total++;
        }
      }
    }
    return total;
  };
  let best = order.map((col) => [...col]);
  let fewest = crossings();
  for (let pass = 0; pass < ORDER_SWEEPS && fewest > 0; pass++) {
    if (pass % 2 === 0) for (let c = last - 1; c >= 1; c--) sweep(c, "right");
    else for (let c = 1; c < last; c++) sweep(c, "left");
    const now = crossings();
    if (now < fewest) {
      fewest = now;
      best = order.map((col) => [...col]);
    }
  }
  best.forEach((col, c) => {
    order[c] = col;
  });

  // rows: every column stacked and centred, then every column but the
  // answers' fitted to its neighbours, sweeping right and back.
  const y = new Map<string, number>();
  const gapsOf = (col: string[]) =>
    col.slice(1).map((id, i) => (slotOf.get(col[i]) as Slot).below + (slotOf.get(id) as Slot).above);
  order.forEach((col) => {
    const gaps = gapsOf(col);
    const first = col.length > 0 ? (slotOf.get(col[0]) as Slot).above : 0;
    const lastBelow = col.length > 0 ? (slotOf.get(col[col.length - 1]) as Slot).below : 0;
    const total = first + gaps.reduce((s, g) => s + g, 0) + lastBelow;
    let at = -total / 2 + first;
    col.forEach((id, i) => {
      if (i > 0) at += gaps[i - 1];
      y.set(id, at);
    });
  });
  for (let pass = 0; pass < ROW_PASSES; pass++) {
    const columns = Array.from({ length: last }, (_, i) => (pass % 2 === 0 ? i + 1 : last - i));
    for (const c of columns) {
      const col = order[c];
      const want = col.map((id) => {
        const near = [...(leftOf.get(id) ?? []), ...(rightOf.get(id) ?? [])];
        return near.length > 0 ? near.reduce((s, n) => s + (y.get(n) ?? 0), 0) / near.length : (y.get(id) ?? 0);
      });
      const weights = col.map((id) => ((slotOf.get(id) as Slot).waypoint ? WAYPOINT_WEIGHT : 1));
      fitInOrder(want, weights, gapsOf(col)).forEach((v, i) => y.set(col[i], v));
    }
  }

  const at = (id: string): Point => ({ x: (columnOf.get(id) ?? 0) * columnGap, y: y.get(id) ?? 0 });
  const positions = new Map<string, Point>();
  for (const node of nodes) positions.set(node.id, at(node.id));
  let top = Infinity;
  for (const [id, slot] of slotOf) top = Math.min(top, (y.get(id) ?? 0) - slot.above);

  // routes: waypoints, the stretch that carries the label, the bundle.
  const members = new Map<string, Span[]>();
  for (const span of spans) members.set(span.bundle, [...(members.get(span.bundle) ?? []), span]);

  // merge points: the links of a bundle that reach their target from
  // different places (an entity next to it, or the bundle's waypoint)
  // join a little before it, so the target takes one arrow and one label
  // per statement. a merge point sits halfway between the height its
  // links arrive at and the target's; merge points into one target keep
  // apart.
  const merges = new Map<string, Point>();
  const intoTarget = new Map<string, { bundle: string; want: number }[]>();
  for (const [bundle, group] of members) {
    if (group.length < 2) continue;
    const arrivals = new Set(group.map((g) => g.chain[g.chain.length - 2]));
    if (arrivals.size < 2) continue;
    const hiY = y.get(group[0].hi) ?? 0;
    const mean = [...arrivals].reduce((sum, id) => sum + (y.get(id) ?? 0), 0) / arrivals.size;
    const list = intoTarget.get(group[0].hi) ?? [];
    list.push({ bundle, want: hiY + 0.5 * (mean - hiY) });
    intoTarget.set(group[0].hi, list);
  }
  for (const [hi, list] of intoTarget) {
    list.sort((a, b) => a.want - b.want);
    const placed = fitInOrder(
      list.map((m) => m.want),
      list.map(() => 1),
      list.slice(1).map(() => MERGE_GAP),
    );
    const x = ((columnOf.get(hi) ?? 0) - MERGE_AT) * columnGap;
    list.forEach((m, i) => merges.set(m.bundle, { x, y: placed[i] }));
  }

  const routes = new Map<string, Route>();
  const level = (chain: string[], from: number, to: number, preferInner: boolean) => {
    // the most level stretch between columns from..to (indices into the
    // chain), between two waypoints when there is one; ties go left.
    let pick = from;
    let score = Infinity;
    for (let i = from; i < to; i++) {
      const inner = i > 0 && i + 1 < chain.length - 1;
      const s = Math.abs((y.get(chain[i + 1]) ?? 0) - (y.get(chain[i]) ?? 0)) + (preferInner && !inner ? 1e6 : 0);
      if (s < score - 0.5) {
        score = s;
        pick = i;
      }
    }
    return pick;
  };
  for (const span of spans) {
    const group = members.get(span.bundle) ?? [span];
    const merge = merges.get(span.bundle) ?? null;
    const { forward } = span;
    const steps = span.hiCol - span.loCol;
    // the links of the bundle that skip columns run together from the
    // first column they share; when there are two or more, their trunk
    // carries the label, else the stretch after the merge point does.
    const long = group.filter((g) => g.hiCol - g.loCol > 1);
    const trunk = long.length > 1 && long.includes(span);
    const lead = long.length > 1 ? long[0] : group[0];
    let labelAt = 0;
    let sharedFrom: number | null = null;
    if (trunk) {
      const start = Math.max(...long.map((g) => g.loCol)) + 1;
      sharedFrom = start * columnGap;
      labelAt = level(span.chain, start - span.loCol, steps, true);
    } else if (merge) {
      sharedFrom = merge.x;
      labelAt = steps;
    } else if (steps > 1) {
      labelAt = level(span.chain, 0, steps, false);
    }
    const inner = [...span.chain.slice(1, -1).map(at), ...(merge ? [merge] : [])];
    const stretches = inner.length + 1;
    routes.set(span.key, {
      through: forward ? inner : [...inner].reverse(),
      labelAt: forward ? labelAt : stretches - 1 - labelAt,
      bundle: span.bundle,
      shared: group.length,
      lead: lead === span,
      trunk,
      sharedFrom,
    });
  }
  return { positions, routes, top: Number.isFinite(top) ? top : 0 };
}

// the positions closest to `want` (weighted least squares) that keep the
// order and at least `gap[i]` between slot i and slot i + 1: shifting
// each slot by the gaps above it turns the spacing into a plain order
// constraint, solved by pooling adjacent violators.
function fitInOrder(want: number[], weight: number[], gap: number[]): number[] {
  const offset = [0];
  for (let i = 1; i < want.length; i++) offset.push(offset[i - 1] + gap[i - 1]);
  const blocks: { sum: number; weight: number; size: number }[] = [];
  for (let i = 0; i < want.length; i++) {
    blocks.push({ sum: weight[i] * (want[i] - offset[i]), weight: weight[i], size: 1 });
    while (blocks.length > 1) {
      const b = blocks[blocks.length - 1];
      const a = blocks[blocks.length - 2];
      if (a.sum / a.weight <= b.sum / b.weight) break;
      blocks.pop();
      a.sum += b.sum;
      a.weight += b.weight;
      a.size += b.size;
    }
  }
  const out: number[] = [];
  for (const block of blocks) {
    for (let k = 0; k < block.size; k++) out.push(block.sum / block.weight + offset[out.length]);
  }
  return out;
}
