// Midnight Observatory node styling (spec: Global Constraints palette).

export const CLUSTER_PALETTE = [
  '#a78bfa', '#38bdf8', '#34d399', '#f472b6', '#fbbf24', '#60a5fa',
  '#c084fc', '#4ade80', '#fb923c', '#2dd4bf', '#e879f9', '#f87171',
];
export const ORPHAN_COLOR = '#4b5563';
export const GHOST_COLOR = '#f87171'; // broken-link ghosts (health lens)

const RADIUS_GHOST = 4;
const GHOST_ALPHA = 0.75;

const RADIUS_ORPHAN_MIN = 1.8;
const RADIUS_ORPHAN_STEP = 0.3; // deterministic per-node variation, starfield depth
const RADIUS_BASE = 5;
const RADIUS_MAX_BONUS = 9;

export function nodeColor(node) {
  if (node.ghost) return GHOST_COLOR;
  if (node.cluster === -1) return ORPHAN_COLOR;
  return CLUSTER_PALETTE[node.cluster % CLUSTER_PALETTE.length];
}

export function nodeRadius(node) {
  if (node.ghost) return RADIUS_GHOST;
  // Orphans are a starfield: small, with deterministic size variation (keyed
  // on pk) so the 500+ gray dots read as depth, not uniform dust.
  if (node.cluster === -1) return RADIUS_ORPHAN_MIN + ((node.id ?? 0) % 4) * RADIUS_ORPHAN_STEP;
  return RADIUS_BASE + Math.min(Math.sqrt(node.connections) * 1.6, RADIUS_MAX_BONUS);
}

// On-screen growth per unit of zoom above 1, and the smallest on-screen radius.
// Nodes that scale with the zoom dwarf their labels up close and vanish far out.
const ZOOM_GROWTH = 0.35;
const MIN_SCREEN_RADIUS = 2.5;
const MIN_SCREEN_RADIUS_ORPHAN = 1.2;

// Radius in graph units to draw a node at zoom k.
export function drawRadius(node, k) {
  const r = nodeRadius(node);
  const screen = k <= 1 ? r * k : r * (1 + (k - 1) * ZOOM_GROWTH);
  const floor = node.cluster === -1 ? MIN_SCREEN_RADIUS_ORPHAN : MIN_SCREEN_RADIUS;
  return Math.max(screen, floor) / k;
}

// Alpha is earned like labels are: leaves sit slightly translucent so hubs and
// structure carry the eye; orphans recede far back with per-node variation.
export function nodeBaseAlpha(node) {
  if (node.ghost) return GHOST_ALPHA;
  if (node.cluster === -1) return 0.3 + ((node.id ?? 0) % 4) * 0.08; // 0.30–0.54
  return Math.min(0.7 + Math.sqrt(node.connections) * 0.09, 1);
}
