// Labels are earned: zoom level sets a connection threshold, then greedy
// placement in a fixed priority order keeps them from overlapping each other or
// covering other nodes. A fixed order means zooming doesn't make labels flicker.
import { nodeRadius } from './colors.js';

const GAP_PX = 4;
const LINE_HEIGHT_PX = 14;
const BASELINE_PX = 4;
const CELL_PX = 120;
const MAX_LABEL_CHARS = 32;
// Hubs are the names worth keeping, and their spokes surround them, so their
// labels may cross other nodes (the halo keeps them legible). Never other labels.
const HUB_CONNECTIONS = 8;

const REGION_HEIGHT_PX = 20;
const REGION_GAP_PX = 8;
const REGION_MIN_MEMBERS = 3;

function connectionThreshold(k) {
  if (k < 0.4) return 6;
  if (k < 0.7) return 3;
  if (k < 1.2) return 1;
  return 0;
}

export function truncateLabel(name, max = MAX_LABEL_CHARS) {
  if (!name) return '';
  return name.length <= max ? name : `${name.slice(0, max - 1)}…`;
}

const defaultMeasure = (text) => (text ? text.length * 6 : 48);

const overlaps = (a, b) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;

// Uniform hash grid over rectangles, so a collision check only looks nearby.
function createGrid(cellSize) {
  const cells = new Map();
  const forCells = (rect, visit) => {
    const x1 = Math.floor(rect.x1 / cellSize);
    const y1 = Math.floor(rect.y1 / cellSize);
    for (let cx = Math.floor(rect.x0 / cellSize); cx <= x1; cx++) {
      for (let cy = Math.floor(rect.y0 / cellSize); cy <= y1; cy++) {
        visit(cx * 100003 + cy);
      }
    }
  };
  return {
    add(rect) {
      forCells(rect, (key) => {
        const cell = cells.get(key);
        if (cell) cell.push(rect);
        else cells.set(key, [rect]);
      });
    },
    hits(rect) {
      let hit = false;
      forCells(rect, (key) => {
        if (hit) return;
        const cell = cells.get(key);
        if (cell) hit = cell.some((other) => overlaps(rect, other));
      });
      return hit;
    },
  };
}

// Places labels and returns Map<id, {text, x, y, side, rect}> in graph units.
// `x`/`y` are the text anchor and baseline; `side` is 'right' (text starts at x)
// or 'left' (text ends at x). Pinned labels (hover, selection) always appear, in
// full, even offscreen or colliding.
export function placeLabels(nodes, k, viewport, {
  measure = defaultMeasure,
  pinned = [],
  reserved = [],
  minConnections,
  radius = nodeRadius,
} = {}) {
  const threshold = minConnections ?? connectionThreshold(k);
  const pinnedIds = new Set(pinned);
  const inView = (n) => n.x >= viewport.x0 && n.x <= viewport.x1 && n.y >= viewport.y0 && n.y <= viewport.y1;

  const labelGrid = createGrid(CELL_PX / k);
  const nodeGrid = createGrid(CELL_PX / k);
  for (const rect of reserved) labelGrid.add(rect);
  for (const n of nodes) {
    if (!inView(n)) continue;
    const r = radius(n);
    nodeGrid.add({ x0: n.x - r, y0: n.y - r, x1: n.x + r, y1: n.y + r });
  }

  const halfHeight = LINE_HEIGHT_PX / 2 / k;
  const candidate = (n, side, width) => {
    const offset = radius(n) + GAP_PX / k;
    const x = side === 'right' ? n.x + offset : n.x - offset;
    return {
      x,
      y: n.y + BASELINE_PX / k,
      side,
      rect: {
        x0: side === 'right' ? x : x - width,
        x1: side === 'right' ? x + width : x,
        y0: n.y - halfHeight,
        y1: n.y + halfHeight,
      },
    };
  };

  const placed = new Map();
  const commit = (n, text, spot) => {
    labelGrid.add(spot.rect);
    placed.set(n.id, { text, ...spot });
  };
  const blocked = (n, rect) => rect.x0 < viewport.x0 || rect.x1 > viewport.x1
    || labelGrid.hits(rect)
    || (n.connections < HUB_CONNECTIONS && nodeGrid.hits(rect));

  for (const n of nodes) {
    if (!pinnedIds.has(n.id)) continue;
    const text = n.name ?? '';
    const width = measure(text) / k;
    const right = candidate(n, 'right', width);
    const left = candidate(n, 'left', width);
    commit(n, text, blocked(n, right.rect) && !blocked(n, left.rect) ? left : right);
  }

  const queue = nodes
    .filter((n) => !pinnedIds.has(n.id) && n.connections >= threshold && inView(n))
    .sort((a, b) => (b.connections - a.connections) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  for (const n of queue) {
    const text = truncateLabel(n.name);
    const width = measure(text) / k;
    const right = candidate(n, 'right', width);
    if (!blocked(n, right.rect)) {
      commit(n, text, right);
      continue;
    }
    const left = candidate(n, 'left', width);
    if (!blocked(n, left.rect)) commit(n, text, left);
  }
  return placed;
}

export function visibleLabels(nodes, k, viewport, options) {
  return new Set(placeLabels(nodes, k, viewport, options).keys());
}

// One name per named group of 3+ notes, above the group or below it, preferring
// a spot clear of other groups' notes. Larger groups choose first; a label that
// overlaps another label in both spots is dropped. Returns [{clusterId, name, x, y, rect}], y being the vertical centre.
export function placeRegionLabels(nodes, names, k, { measure = defaultMeasure } = {}) {
  const groups = new Map();
  for (const n of nodes) {
    if (n.ghost || !Number.isInteger(n.cluster) || n.cluster < 0 || !names[n.cluster]) continue;
    const g = groups.get(n.cluster) ?? { sumX: 0, top: Infinity, bottom: -Infinity, count: 0 };
    g.sumX += n.x;
    g.top = Math.min(g.top, n.y);
    g.bottom = Math.max(g.bottom, n.y);
    g.count += 1;
    groups.set(n.cluster, g);
  }

  const height = REGION_HEIGHT_PX / k;
  const gap = REGION_GAP_PX / k;
  const taken = [];
  const out = [];
  const ordered = [...groups.entries()]
    .filter(([, g]) => g.count >= REGION_MIN_MEMBERS)
    .sort((a, b) => (b[1].count - a[1].count) || (a[0] - b[0]));

  for (const [clusterId, g] of ordered) {
    const name = names[clusterId];
    const halfWidth = measure(name) / k / 2;
    const x = g.sumX / g.count;
    const spotAt = (y0) => ({ x0: x - halfWidth, x1: x + halfWidth, y0, y1: y0 + height });
    const open = [spotAt(g.top - gap - height), spotAt(g.bottom + gap)]
      .filter((spot) => !taken.some((other) => overlaps(spot, other)));
    if (open.length === 0) continue;
    const coversOtherGroup = (spot) => nodes.some((n) => n.cluster !== clusterId
      && n.x >= spot.x0 && n.x <= spot.x1 && n.y >= spot.y0 && n.y <= spot.y1);
    const rect = open.find((spot) => !coversOtherGroup(spot)) ?? open[0];
    taken.push(rect);
    out.push({ clusterId, name, x, y: (rect.y0 + rect.y1) / 2, rect });
  }
  return out;
}
