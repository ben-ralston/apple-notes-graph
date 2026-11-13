import {
  forceCollide, forceLink, forceManyBody,
  forceSimulation, forceX, forceY,
} from 'd3-force';
import { nodeRadius } from './colors.js';

const PRETICK_MAX = 300;
const ALPHA_MIN = 0.01;
const ALPHA_DECAY = 0.05;
// A graph that's already laid out only settles, so a refresh, filter or lens
// toggle doesn't rearrange the map.
const SETTLE_ALPHA = 0.06;
const SETTLED_SHARE = 0.9;

// Same-cluster links are short and firm; links between clusters are long and
// weak, so shared hubs stop dragging whole groups into each other.
const LINK_DISTANCE_SAME = 46;
const LINK_DISTANCE_CROSS = 70;
const LINK_STRENGTH_SAME = 0.5;
const LINK_STRENGTH_CROSS = 0.025;

const CHARGE = -70;
const CHARGE_REACH = 600;
const COLLIDE_PADDING = 7;
// Split by window shape so a landscape window gets a landscape layout.
const CENTER_PULL = 0.05;

// Each cluster gets a soft pull toward its own centroid, which keeps a group
// compact once the weak cross-links stop holding it together.
const CLUSTER_PULL = 0.3;

// Unlinked notes sit on a sparse ring outside everything else.
const RING_MARGIN = 24;
const RING_SPACING = 34;
const WINDOW_ASPECT_MAX = 1.6;
const HOME_PULL = 0.35;

const RING_MIN_GAP = 8;
const RING_NUDGE = 0.05; // radians per retry
const RING_NUDGE_TRIES = 40;
const SEED_DISTANCE = 40;

const endpointId = (end) => end?.id ?? end;

// Stable pseudo-random value in [0, 1) for a note, so its place doesn't depend on
// which other notes exist.
function idHash(id, salt) {
  const text = `${salt}:${id}`;
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  // FNV's low bits vary little between similar short strings; mix before scaling.
  h ^= h >>> 15;
  h = Math.imul(h, 0x2c1b3c6d) >>> 0;
  h ^= h >>> 13;
  return (h >>> 0) / 2 ** 32;
}

function isCrossCluster(link) {
  const a = link.source.cluster;
  const b = link.target.cluster;
  // Ghosts have no cluster and stay close to the note that references them.
  return Number.isInteger(a) && Number.isInteger(b) && a >= 0 && b >= 0 && a !== b;
}

// Limits a force to linked notes: repulsion must not reach the ring, or every
// re-warm (a drag anywhere) would push the whole ring outward.
function forLinkedOnly(force, isLinked) {
  const initialize = force.initialize;
  force.initialize = (all, ...rest) => initialize(all.filter(isLinked), ...rest);
  return force;
}

function forceClusterPull(strength) {
  let nodes = [];
  const force = (alpha) => {
    const totals = new Map();
    for (const n of nodes) {
      if (!Number.isInteger(n.cluster) || n.cluster < 0) continue;
      const t = totals.get(n.cluster) ?? { x: 0, y: 0, count: 0 };
      t.x += n.x;
      t.y += n.y;
      t.count += 1;
      totals.set(n.cluster, t);
    }
    for (const n of nodes) {
      const t = totals.get(n.cluster);
      if (!t || t.count < 2) continue;
      n.vx += (t.x / t.count - n.x) * strength * alpha;
      n.vy += (t.y / t.count - n.y) * strength * alpha;
    }
  };
  force.initialize = (all) => { nodes = all; };
  return force;
}

// Positions on a band of rounded rectangles (superellipses, exponent 4) outside
// `innerRadius`. Each note's place comes from a hash of its id, so adding or removing
// one barely moves the others. A note that would land on an earlier one (by id)
// slides along the band, so only that note moves. The band widens with the count to
// keep dots spread. `aspect` stretches x relative to y.
export function placeUnlinked(nodes, innerRadius, center, aspect = 1) {
  const homes = new Map();
  const spread = ((nodes.length - 1) * RING_SPACING ** 2) / Math.PI;
  const sorted = [...nodes].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const placed = [];
  const clashes = (p) => placed.some((q) => Math.hypot(p.x - q.x, p.y - q.y) < RING_MIN_GAP);
  for (const n of sorted) {
    const radius = Math.sqrt(innerRadius ** 2 + idHash(n.id, 'r') * spread);
    const start = idHash(n.id, 'a') * 2 * Math.PI;
    const at = (angle) => ({
      x: center.x + Math.sign(Math.cos(angle)) * Math.sqrt(Math.abs(Math.cos(angle))) * radius * aspect,
      y: center.y + Math.sign(Math.sin(angle)) * Math.sqrt(Math.abs(Math.sin(angle))) * radius,
    });
    let home = at(start);
    for (let tries = 1; tries <= RING_NUDGE_TRIES && clashes(home); tries++) {
      home = at(start + tries * RING_NUDGE);
    }
    placed.push(home);
    homes.set(n.id, home);
  }
  return homes;
}

// In a settle-only pass the forces are too weak to bring a note in from far away,
// so unplaced notes start beside a placed neighbour. Runs until no more can be
// placed, so a chain of new notes follows its one placed neighbour.
function seedUnplaced(nodes, links, fallback) {
  const isPlaced = (n) => Number.isFinite(n.x) && Number.isFinite(n.y);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const neighbours = new Map();
  const connect = (a, b) => {
    if (!neighbours.has(a)) neighbours.set(a, []);
    neighbours.get(a).push(b);
  };
  for (const l of links) {
    const source = byId.get(endpointId(l.source));
    const target = byId.get(endpointId(l.target));
    if (source && target) {
      connect(source.id, target);
      connect(target.id, source);
    }
  }
  const place = (n, near) => {
    const angle = idHash(n.id, 's') * 2 * Math.PI;
    n.x = near.x + Math.cos(angle) * SEED_DISTANCE;
    n.y = near.y + Math.sin(angle) * SEED_DISTANCE;
  };

  let waiting = nodes.filter((n) => !isPlaced(n));
  while (waiting.length > 0) {
    const stillWaiting = [];
    for (const n of waiting) {
      const anchor = neighbours.get(n.id)?.find(isPlaced);
      if (anchor) place(n, anchor);
      else stillWaiting.push(n);
    }
    if (stillWaiting.length === waiting.length) {
      for (const n of stillWaiting) place(n, fallback);
      return;
    }
    waiting = stillWaiting;
  }
}

// The smallest rounded rectangle with the linked bounding box's proportions that
// encloses every linked note, so the ring hugs the footprint.
function enclosingShape(nodes, fallbackCenter) {
  if (nodes.length === 0) return { center: fallbackCenter, a: 0, b: 0 };
  const center = {
    x: nodes.reduce((s, n) => s + n.x, 0) / nodes.length,
    y: nodes.reduce((s, n) => s + n.y, 0) / nodes.length,
  };
  const halfWidth = Math.max(1, ...nodes.map((n) => Math.abs(n.x - center.x)));
  const halfHeight = Math.max(1, ...nodes.map((n) => Math.abs(n.y - center.y)));
  const scale = Math.max(...nodes.map((n) => (
    ((Math.abs(n.x - center.x) + nodeRadius(n)) / halfWidth) ** 4
    + ((Math.abs(n.y - center.y) + nodeRadius(n)) / halfHeight) ** 4
  ) ** 0.25));
  return { center, a: halfWidth * scale, b: halfHeight * scale };
}

// Pre-tick to a settled layout before first paint: no visible convulsing,
// and the sim is left FROZEN. Interactions (drag) re-warm it directly via
// alphaTarget(...).restart().
export function createSimulation(nodes, links, width, height) {
  const viewCenter = { x: width / 2, y: height / 2 };
  const windowAspect = Math.min(Math.max(width / Math.max(height, 1), 1), WINDOW_ASPECT_MAX);
  const pullX = CENTER_PULL / windowAspect;
  const pullY = CENTER_PULL * windowAspect;
  const linkedIds = new Set();
  for (const l of links) {
    linkedIds.add(endpointId(l.source));
    linkedIds.add(endpointId(l.target));
  }
  const linked = nodes.filter((n) => linkedIds.has(n.id));
  const unlinked = nodes.filter((n) => !linkedIds.has(n.id));

  // A note that just left the ring has a position, but not a meaningful one.
  for (const n of linked) {
    if (n.onRing) {
      delete n.x;
      delete n.y;
      n.onRing = false;
    }
  }
  // Read before forceSimulation, which assigns a position to every node.
  const placedCount = linked.filter((n) => Number.isFinite(n.x) && Number.isFinite(n.y)).length;
  const alreadySettled = linked.length > 0 && placedCount / linked.length >= SETTLED_SHARE;
  if (alreadySettled) seedUnplaced(linked, links, viewCenter);

  const sim = forceSimulation(linked)
    .force('link', forceLink(links)
      .id((d) => d.id)
      .distance((l) => (isCrossCluster(l) ? LINK_DISTANCE_CROSS : LINK_DISTANCE_SAME))
      .strength((l) => (isCrossCluster(l) ? LINK_STRENGTH_CROSS : LINK_STRENGTH_SAME)))
    .force('charge', forceManyBody().strength(CHARGE).distanceMax(CHARGE_REACH))
    .force('collide', forceCollide().radius((d) => nodeRadius(d) + COLLIDE_PADDING).strength(0.9))
    .force('cluster', forceClusterPull(CLUSTER_PULL))
    .force('x', forceX(viewCenter.x).strength(pullX))
    .force('y', forceY(viewCenter.y).strength(pullY))
    .alphaDecay(ALPHA_DECAY)
    .velocityDecay(0.4)
    .alphaMin(ALPHA_MIN)
    .stop();

  if (alreadySettled) sim.alpha(SETTLE_ALPHA);

  for (let i = 0; i < PRETICK_MAX && sim.alpha() > ALPHA_MIN; i++) sim.tick();

  const { center, a, b } = enclosingShape(linked, viewCenter);
  const innerRadius = linked.length > 0 ? b + RING_MARGIN : 0;
  const ringAspect = linked.length > 0 ? (a + RING_MARGIN) / innerRadius : windowAspect;
  const homes = placeUnlinked(unlinked, innerRadius, center, ringAspect);
  for (const n of unlinked) {
    const home = homes.get(n.id);
    n.x = home.x;
    n.y = home.y;
    n.vx = 0;
    n.vy = 0;
    n.onRing = true;
  }

  // Bring unlinked notes into the sim so they can be dragged, outside the
  // repulsion and with a pull back to their ring position.
  const isHomed = (d) => homes.has(d.id);
  const isLinked = (d) => !isHomed(d);
  sim
    .nodes([...linked, ...unlinked])
    .force('charge', forLinkedOnly(forceManyBody().strength(CHARGE).distanceMax(CHARGE_REACH), isLinked))
    .force('cluster', forLinkedOnly(forceClusterPull(CLUSTER_PULL), isLinked))
    .force('x', forceX((d) => homes.get(d.id)?.x ?? viewCenter.x)
      .strength((d) => (isHomed(d) ? HOME_PULL : pullX)))
    .force('y', forceY((d) => homes.get(d.id)?.y ?? viewCenter.y)
      .strength((d) => (isHomed(d) ? HOME_PULL : pullY)));

  return sim;
}

// Mean, over clusters of 3+ notes, of the distance to the nearest cluster
// relative to the two clusters' radii (90th-percentile member distance). At 1
// or above, neighbouring groups stop overlapping. Works on nodes() hook output.
export function clusterSeparation(nodes) {
  const groups = new Map();
  for (const n of nodes) {
    if (n.ghost || !Number.isInteger(n.cluster) || n.cluster < 0) continue;
    if (!groups.has(n.cluster)) groups.set(n.cluster, []);
    groups.get(n.cluster).push(n);
  }
  const clusters = [...groups.values()].filter((g) => g.length >= 3).map((g) => {
    const x = g.reduce((s, n) => s + n.x, 0) / g.length;
    const y = g.reduce((s, n) => s + n.y, 0) / g.length;
    const distances = g.map((n) => Math.hypot(n.x - x, n.y - y)).sort((a, b) => a - b);
    return { x, y, r: distances[Math.floor(distances.length * 0.9)] };
  });
  if (clusters.length < 2) return 0;
  const nearest = clusters.map((a) => Math.min(...clusters
    .filter((b) => b !== a)
    .map((b) => Math.hypot(a.x - b.x, a.y - b.y) / (a.r + b.r || 1))));
  return nearest.reduce((s, v) => s + v, 0) / nearest.length;
}
