import { describe, expect, test } from 'vitest';
import { nodeRadius } from './colors.js';
import { clusterSeparation, createSimulation, placeUnlinked } from './simulation.js';
import { makeGraph } from './testGraph.js';

const W = 1166;
const H = 723;

function layout(options) {
  const { nodes, links } = makeGraph(options);
  const sim = createSimulation(nodes, links, W, H);
  return { nodes, links, sim };
}

function linkedOf(nodes, links) {
  const ids = new Set();
  for (const l of links) {
    ids.add(l.source.id ?? l.source);
    ids.add(l.target.id ?? l.target);
  }
  return {
    linked: nodes.filter((n) => ids.has(n.id)),
    unlinked: nodes.filter((n) => !ids.has(n.id)),
  };
}

function extent(nodes) {
  const xs = nodes.map((n) => n.x);
  const ys = nodes.map((n) => n.y);
  return { width: Math.max(...xs) - Math.min(...xs), height: Math.max(...ys) - Math.min(...ys) };
}

// Rounded-rectangle distance from the linked centroid, normalised by the linked
// bounding box, so "outside the footprint" holds for a landscape layout.
function footprint(linked) {
  const cx = linked.reduce((s, n) => s + n.x, 0) / linked.length;
  const cy = linked.reduce((s, n) => s + n.y, 0) / linked.length;
  const a = Math.max(...linked.map((n) => Math.abs(n.x - cx)));
  const b = Math.max(...linked.map((n) => Math.abs(n.y - cy)));
  const scaled = (n) => (((n.x - cx) / a) ** 4 + ((n.y - cy) / b) ** 4) ** 0.25;
  return { scaled, reach: Math.max(...linked.map(scaled)) };
}

const finite = (n) => Number.isFinite(n.x) && Number.isFinite(n.y);

describe('createSimulation layout', () => {
  test('linked nodes do not overlap', () => {
    const { nodes, links } = layout();
    const { linked } = linkedOf(nodes, links);
    let pairs = 0;
    let overlapping = 0;
    for (let i = 0; i < linked.length; i++) {
      for (let j = i + 1; j < linked.length; j++) {
        pairs += 1;
        const d = Math.hypot(linked[i].x - linked[j].x, linked[i].y - linked[j].y);
        if (d < nodeRadius(linked[i]) + nodeRadius(linked[j])) overlapping += 1;
      }
    }
    expect(overlapping / pairs).toBeLessThanOrEqual(0.01);
  });

  test('clusters read as separate groups', () => {
    const { nodes } = layout();
    expect(clusterSeparation(nodes)).toBeGreaterThanOrEqual(1);
  });

  test('every unlinked node sits outside the linked footprint', () => {
    const { nodes, links } = layout({ unlinked: 80 });
    const { linked, unlinked } = linkedOf(nodes, links);
    const { scaled, reach } = footprint(linked);
    for (const n of unlinked) expect(scaled(n)).toBeGreaterThan(reach);
  });

  test('linked notes follow a landscape window: wider than tall', () => {
    const { nodes, links } = layout();
    const { linked } = linkedOf(nodes, links);
    const { width, height } = extent(linked);
    expect(width / height).toBeGreaterThan(1.25);
  });

  test('the ring costs at most 15% of the fit zoom', () => {
    const { nodes, links } = layout();
    const { linked } = linkedOf(nodes, links);
    const fit = ({ width, height }) => Math.min((W - 128) / width, (H - 128) / height);
    expect(fit(extent(nodes)) / fit(extent(linked))).toBeGreaterThan(0.85);
  });

  test('a square window keeps a roughly round layout', () => {
    const { nodes, links } = makeGraph();
    createSimulation(nodes, links, 800, 800);
    const { width, height } = extent(linkedOf(nodes, links).linked);
    const ratio = width / height;
    expect(ratio).toBeGreaterThan(0.75);
    expect(ratio).toBeLessThan(1.33);
  });

  test('laying out an already-settled graph barely moves it', () => {
    const { nodes, links } = layout();
    const before = new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y }]));
    createSimulation(nodes, links, W, H);
    const { width } = extent(nodes);
    const moved = Math.max(...nodes.map((n) => Math.hypot(n.x - before.get(n.id).x, n.y - before.get(n.id).y)));
    expect(moved / width).toBeLessThan(0.05);
  });

  test('a note revealed into a settled graph lands beside its neighbour', () => {
    const { nodes, links } = makeGraph({ clusters: 10, size: 20, bridges: 20, unlinked: 0 });
    createSimulation(nodes, links, W, H);
    const hub = nodes.find((n) => n.name === 'Hub 3');
    const revealed = { id: 9100, name: 'Unhidden', cluster: 3, connections: 1 };
    nodes.push(revealed);
    links.push({ source: hub.id, target: revealed.id });
    createSimulation(nodes, links, W, H);
    expect(Math.hypot(revealed.x - hub.x, revealed.y - hub.y)).toBeLessThan(120);
  });

  test('a ring note that gains a link moves in beside its new neighbour', () => {
    const { nodes, links } = makeGraph({ unlinked: 20 });
    createSimulation(nodes, links, W, H);
    const orphan = nodes.find((n) => n.cluster === -1);
    const distanceTo = (n) => Math.hypot(n.x - orphan.x, n.y - orphan.y);
    const hub = nodes.filter((n) => n.name.startsWith('Hub'))
      .reduce((far, n) => (distanceTo(n) > distanceTo(far) ? n : far));
    links.push({ source: hub.id, target: orphan.id });
    createSimulation(nodes, links, W, H);
    expect(Math.hypot(orphan.x - hub.x, orphan.y - hub.y)).toBeLessThan(150);
  });

  test('changing one unlinked note leaves the rest of the ring in place', () => {
    const { nodes, links } = layout({ unlinked: 40 });
    const orphans = nodes.filter((n) => n.cluster === -1);
    const before = new Map(orphans.map((n) => [n.id, { x: n.x, y: n.y }]));
    const removed = orphans[5];
    createSimulation(nodes.filter((n) => n !== removed), links, W, H);
    const moved = orphans.filter((n) => n !== removed)
      .map((n) => Math.hypot(n.x - before.get(n.id).x, n.y - before.get(n.id).y));
    expect(Math.max(...moved)).toBeLessThan(25);
  });

  test('a graph where most notes are new gets a full layout', () => {
    const { nodes, links } = makeGraph();
    createSimulation(nodes, links, W, H);
    const fresh = makeGraph();
    fresh.nodes.slice(0, 20).forEach((n, i) => { n.x = nodes[i].x; n.y = nodes[i].y; });
    createSimulation(fresh.nodes, fresh.links, W, H);
    expect(clusterSeparation(fresh.nodes)).toBeGreaterThanOrEqual(1);
  });

  test('unlinked ring placement is deterministic across runs', () => {
    const first = layout({ unlinked: 80 }).nodes.map((n) => [n.x, n.y]);
    const second = layout({ unlinked: 80 }).nodes.map((n) => [n.x, n.y]);
    expect(second).toEqual(first);
  });

  test('a large graph lays out in under 1500ms', () => {
    const { nodes, links } = makeGraph({ clusters: 14, size: 20, bridges: 40, unlinked: 490 });
    const start = performance.now();
    createSimulation(nodes, links, W, H);
    expect(performance.now() - start).toBeLessThan(1500);
  });

  test('a dragged-away unlinked node springs back to the ring', () => {
    const { nodes, sim } = layout();
    const n = nodes.find((x) => x.cluster === -1);
    const home = { x: n.x, y: n.y };
    n.x += 200;
    n.y += 200;
    sim.alpha(0.6);
    for (let i = 0; i < 120; i++) sim.tick();
    expect(Math.hypot(n.x - home.x, n.y - home.y)).toBeLessThan(40);
  });
});

describe('createSimulation edge cases', () => {
  test('all nodes unlinked: finite positions', () => {
    const { nodes, links } = layout({ clusters: 0, bridges: 0, unlinked: 25 });
    expect(links).toHaveLength(0);
    expect(nodes.every(finite)).toBe(true);
    expect(new Set(nodes.map((n) => `${n.x},${n.y}`)).size).toBe(25);
  });

  test('a node with a cluster id but no visible links goes to the ring', () => {
    const { nodes, links } = makeGraph({ unlinked: 0 });
    const filteredOut = { id: 9001, name: 'Links hidden by a filter', cluster: 3, connections: 5 };
    nodes.push(filteredOut);
    createSimulation(nodes, links, W, H);
    const { linked } = linkedOf(nodes, links);
    const { scaled, reach } = footprint(linked);
    expect(scaled(filteredOut)).toBeGreaterThan(reach);
  });

  test('a single node: finite position at the canvas centre', () => {
    const only = { id: 1, name: 'Only', cluster: -1, connections: 0 };
    createSimulation([only], [], W, H);
    expect(finite(only)).toBe(true);
    expect(only.x).toBeCloseTo(W / 2);
    expect(only.y).toBeCloseTo(H / 2);
  });

  test('a ghost with a link stays beside its source, not on the ring', () => {
    const { nodes, links } = makeGraph({ unlinked: 10 });
    const source = nodes[5];
    const ghost = { id: 'ghost:x', name: 'Missing', ghost: true, sources: [source.id], connections: 1 };
    nodes.push(ghost);
    links.push({ source: source.id, target: ghost.id, ghost: true });
    createSimulation(nodes, links, W, H);
    expect(Math.hypot(ghost.x - source.x, ghost.y - source.y)).toBeLessThan(200);
  });

  test('an empty graph does not throw', () => {
    expect(() => createSimulation([], [], W, H)).not.toThrow();
  });
});

describe('placeUnlinked', () => {
  test('keeps every node at or beyond the inner radius, stretched wider than tall', () => {
    const nodes = Array.from({ length: 60 }, (_, i) => ({ id: i + 1 }));
    const homes = placeUnlinked(nodes, 300, { x: 0, y: 0 }, 1.5);
    for (const { x, y } of homes.values()) expect(Math.hypot(x, y)).toBeGreaterThanOrEqual(300);
    expect(Math.max(...[...homes.values()].map((p) => Math.abs(p.x))))
      .toBeGreaterThan(Math.max(...[...homes.values()].map((p) => Math.abs(p.y))));
  });

  test('keeps notes from landing on top of each other', () => {
    const nodes = Array.from({ length: 300 }, (_, i) => ({ id: 300 + i }));
    const homes = [...placeUnlinked(nodes, 400, { x: 0, y: 0 }, 1.5).values()];
    let closest = Infinity;
    for (let i = 0; i < homes.length; i++) {
      for (let j = i + 1; j < homes.length; j++) {
        closest = Math.min(closest, Math.hypot(homes[i].x - homes[j].x, homes[i].y - homes[j].y));
      }
    }
    expect(closest).toBeGreaterThanOrEqual(8);
  });

  test('is independent of input order', () => {
    const nodes = [{ id: 3 }, { id: 1 }, { id: 2 }];
    const a = placeUnlinked(nodes, 100, { x: 0, y: 0 }, 1);
    const b = placeUnlinked([...nodes].reverse(), 100, { x: 0, y: 0 }, 1);
    expect([...a.keys()].sort()).toEqual([...b.keys()].sort());
    for (const id of a.keys()) expect(b.get(id)).toEqual(a.get(id));
  });
});

describe('clusterSeparation', () => {
  const blob = (cluster, cx) =>
    Array.from({ length: 10 }, (_, i) => ({
      id: `${cluster}-${i}`,
      cluster,
      x: cx + Math.cos(i) * 10,
      y: Math.sin(i) * 10,
    }));

  test('is high for distant clusters and low for overlapping ones', () => {
    expect(clusterSeparation([...blob(0, 0), ...blob(1, 500)])).toBeGreaterThan(5);
    expect(clusterSeparation([...blob(0, 0), ...blob(1, 2)])).toBeLessThan(1);
  });

  test('ignores unlinked nodes, ghosts and tiny clusters, and copes with one cluster', () => {
    const nodes = [
      ...blob(0, 0),
      { id: 'u', cluster: -1, x: 1, y: 1 },
      { id: 'g', ghost: true, x: 1, y: 1 },
      { id: 't', cluster: 5, x: 2, y: 2 },
    ];
    expect(Number.isFinite(clusterSeparation(nodes))).toBe(true);
  });
});
