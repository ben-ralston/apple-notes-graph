import { describe, expect, test } from 'vitest';
import { placeLabels, placeRegionLabels, truncateLabel, visibleLabels } from './labels.js';
import { createSimulation } from './simulation.js';
import { makeGraph } from './testGraph.js';

const viewport = { x0: 0, y0: 0, x1: 1000, y1: 1000 };
const node = (id, connections, x = 500, y = 500) => ({ id, connections, x, y });

describe('visibleLabels', () => {
  test('far zoom shows only hubs', () => {
    const nodes = [node(1, 10), node(2, 2, 600, 600), node(3, 0, 700, 700)];
    const visible = visibleLabels(nodes, 0.4, viewport);
    expect(visible.has(1)).toBe(true);
    expect(visible.has(2)).toBe(false);
    expect(visible.has(3)).toBe(false);
  });

  test('close zoom shows everything in viewport', () => {
    const nodes = [node(1, 10), node(2, 2, 600, 600), node(3, 0, 700, 700)];
    const visible = visibleLabels(nodes, 2.0, viewport);
    expect(visible.size).toBe(3);
  });

  test('offscreen nodes never get labels', () => {
    const visible = visibleLabels([node(1, 50, -500, -500)], 2.0, viewport);
    expect(visible.size).toBe(0);
  });

  test('crowded spots keep only the best-connected label', () => {
    // Node 1 takes the right side and node 2 the left, leaving node 3 neither.
    const nodes = [node(1, 6, 500, 500), node(2, 5, 500, 505), node(3, 4, 500, 495)];
    const visible = visibleLabels(nodes, 1.0, viewport, { measure: () => 60, radius: () => 2 });
    expect(visible.has(1)).toBe(true);
    expect(visible.has(3)).toBe(false);
  });

  test('minConnections overrides the zoom threshold', () => {
    const nodes = [node(1, 0, 100, 100)];
    expect(visibleLabels(nodes, 0.3, viewport).size).toBe(0);
    expect(visibleLabels(nodes, 0.3, viewport, { minConnections: 0 }).size).toBe(1);
  });
});

function laidOut() {
  const { nodes, links } = makeGraph();
  createSimulation(nodes, links, 1166, 723);
  return nodes;
}
const wide = { x0: -1e5, y0: -1e5, x1: 1e5, y1: 1e5 };
const measure = (name) => name.length * 6;
const intersects = (a, b) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;

describe('placeLabels', () => {
  test.each([0.3, 0.6, 1, 2])('placed label rectangles never intersect at k=%s', (k) => {
    const placed = [...placeLabels(laidOut(), k, wide, { measure }).values()];
    expect(placed.length).toBeGreaterThan(0);
    for (let i = 0; i < placed.length; i++) {
      for (let j = i + 1; j < placed.length; j++) {
        expect(intersects(placed[i].rect, placed[j].rect)).toBe(false);
      }
    }
  });

  test('labels do not cover another node', () => {
    const nodes = laidOut();
    const k = 1;
    const placed = placeLabels(nodes, k, wide, { measure, radius: () => 6 });
    const byId = new Map(nodes.map((n) => [n.id, n]));
    for (const [id, { rect }] of placed) {
      if (byId.get(id).connections >= 8) continue; // hubs may cross nodes, see below
      for (const n of nodes) {
        if (n.id === id) continue;
        const circle = { x0: n.x - 6, x1: n.x + 6, y0: n.y - 6, y1: n.y + 6 };
        expect(intersects(rect, circle)).toBe(false);
      }
    }
  });

  test('a hub surrounded by its spokes still gets a label', () => {
    const spokes = [0, 1, 2, 3, 4, 5].map((i) => node(i + 2, 1, 500 + 30 * Math.cos(i), 500 + 30 * Math.sin(i)));
    const hub = node(1, 12);
    const placed = placeLabels([hub, ...spokes], 0.7, viewport, { measure: () => 60, radius: () => 5 });
    expect(placed.has(1)).toBe(true);
  });

  test('a non-hub surrounded by nodes does not', () => {
    const others = [0, 1, 2, 3, 4, 5].map((i) => node(i + 2, 0, 500 + 30 * Math.cos(i), 500 + 30 * Math.sin(i)));
    const quiet = node(1, 2);
    const placed = placeLabels([quiet, ...others], 0.7, viewport, { measure: () => 60, radius: () => 5 });
    expect(placed.has(1)).toBe(false);
  });

  test('falls back to the left side when the right is blocked', () => {
    const nodes = [node(1, 5, 500, 500), node(2, 9, 560, 500)];
    const placed = placeLabels(nodes, 1, viewport, { measure: () => 60, radius: () => 5 });
    expect(placed.get(1).side).toBe('left');
    expect(placed.get(1).x).toBeLessThan(500);
  });

  test('a label that would run off the left edge uses the right side instead', () => {
    const blocker = node(2, 20, 80, 500);
    const placed = placeLabels([node(1, 5, 20, 500), blocker], 1, viewport, { measure: () => 40, radius: () => 2 });
    expect(placed.get(1).side).toBe('right');
  });

  test('a label that fits on neither side is dropped, not clipped', () => {
    const tight = { x0: 0, y0: 0, x1: 100, y1: 1000 };
    const placed = placeLabels([node(1, 5, 50, 500)], 1, tight, { measure: () => 80, radius: () => 2 });
    expect(placed.has(1)).toBe(false);
  });

  test('a label prefers the left side when the right would run off the viewport', () => {
    const placed = placeLabels([node(1, 5, 960, 500)], 1, viewport, { measure: () => 60, radius: () => 2 });
    expect(placed.get(1).side).toBe('left');
  });

  test('pinned labels always appear, even when colliding', () => {
    const nodes = [node(1, 20, 500, 500), node(2, 0, 503, 500)];
    const placed = placeLabels(nodes, 1, viewport, { measure: () => 60, pinned: [2] });
    expect(placed.has(2)).toBe(true);
  });

  test('pinned offscreen nodes are still returned so a selection keeps its label', () => {
    const placed = placeLabels([node(1, 0, -900, -900)], 1, viewport, { measure, pinned: [1] });
    expect(placed.has(1)).toBe(true);
  });

  test('reserved rectangles block labels', () => {
    const reserved = [{ x0: 0, y0: 0, x1: 1000, y1: 1000 }];
    const placed = placeLabels([node(1, 10, 500, 500)], 1, viewport, { measure, reserved });
    expect(placed.size).toBe(0);
  });

  test('order is stable across calls and input order', () => {
    const nodes = laidOut();
    const ids = (list) => [...placeLabels(list, 0.6, wide, { measure }).keys()].sort((a, b) => a - b);
    expect(ids([...nodes].reverse())).toEqual(ids(nodes));
  });

  test('sizes label rectangles in graph units, so zooming out makes them relatively bigger', () => {
    const near = placeLabels([node(1, 9)], 2, viewport, { measure: () => 60, radius: () => 5 }).get(1);
    const far = placeLabels([node(1, 9)], 0.5, viewport, { measure: () => 60, radius: () => 5 }).get(1);
    expect(far.rect.x1 - far.rect.x0).toBeCloseTo(120);
    expect(near.rect.x1 - near.rect.x0).toBeCloseTo(30);
  });

  test('800 nodes place in under 4ms (median of 20)', () => {
    const { nodes, links } = makeGraph({ clusters: 14, size: 22, bridges: 40, unlinked: 490 });
    createSimulation(nodes, links, 1166, 723);
    const times = [];
    for (let i = 0; i < 20; i++) {
      const t = performance.now();
      placeLabels(nodes, 0.7, wide, { measure });
      times.push(performance.now() - t);
    }
    times.sort((a, b) => a - b);
    expect(times[10]).toBeLessThan(4);
  });
});

describe('truncateLabel', () => {
  test('keeps short names', () => {
    expect(truncateLabel('Short name')).toBe('Short name');
    expect(truncateLabel('x'.repeat(32))).toBe('x'.repeat(32));
  });

  test('ends long names with an ellipsis at the limit', () => {
    const out = truncateLabel('y'.repeat(50));
    expect(out).toHaveLength(32);
    expect(out.endsWith('…')).toBe(true);
  });

  test('tolerates a missing name', () => {
    expect(truncateLabel(undefined)).toBe('');
  });
});

describe('placeRegionLabels', () => {
  const blob = (cluster, cx, cy) => Array.from({ length: 6 }, (_, i) => ({
    id: cluster * 100 + i, cluster, x: cx + i * 5, y: cy + i * 5, connections: 1,
  }));
  const k = 1;
  const options = { measure: () => 100 };

  test('sits above its group', () => {
    const [label] = placeRegionLabels(blob(0, 500, 500), { 0: 'Group' }, k, options);
    expect(label.name).toBe('Group');
    expect(label.rect.y1).toBeLessThanOrEqual(500);
  });

  test('moves below its group when another group\'s notes fill the space above', () => {
    const own = blob(0, 500, 500);
    const neighbour = [0, 1, 2, 3].map((i) => ({ id: 900 + i, cluster: 1, x: 480 + i * 15, y: 480, connections: 1 }));
    const [label] = placeRegionLabels([...own, ...neighbour], { 0: 'Group' }, k, options);
    expect(label.rect.y0).toBeGreaterThanOrEqual(525);
  });

  test('still shows the name when both spots cover other notes, preferring above', () => {
    const own = blob(0, 500, 500);
    const around = [480, 540].flatMap((y) => [0, 1, 2, 3].map((i) => (
      { id: y * 10 + i, cluster: 1, x: 480 + i * 15, y, connections: 1 }
    )));
    const [label] = placeRegionLabels([...own, ...around], { 0: 'Group' }, k, options);
    expect(label.rect.y1).toBeLessThanOrEqual(500);
  });

  test('moves below its group when another label holds the space above', () => {
    const labels = placeRegionLabels(
      [...blob(0, 500, 500), ...blob(1, 510, 505)],
      { 0: 'One', 1: 'Two' }, k, options,
    );
    expect(labels).toHaveLength(2);
    expect(intersects(labels[0].rect, labels[1].rect)).toBe(false);
  });

  test('drops a label when neither position is free, keeping the larger group', () => {
    const big = blob(0, 500, 500);
    const small = blob(1, 500, 500).slice(0, 3);
    const labels = placeRegionLabels([...small, ...big], { 0: 'Big', 1: 'Small' }, k, options);
    expect(labels.map((l) => l.name)).toContain('Big');
    for (let i = 0; i < labels.length; i++) {
      for (let j = i + 1; j < labels.length; j++) {
        expect(intersects(labels[i].rect, labels[j].rect)).toBe(false);
      }
    }
  });

  test('skips clusters without a name, tiny groups, unlinked notes and ghosts', () => {
    const nodes = [
      ...blob(0, 100, 100),
      ...blob(1, 500, 500).slice(0, 2),
      { id: 'u', cluster: -1, x: 1, y: 1 },
      { id: 'g', ghost: true, x: 1, y: 1 },
    ];
    const names = placeRegionLabels(nodes, { 1: 'Tiny', '-1': 'None' }, k, options).map((l) => l.name);
    expect(names).toEqual([]);
  });
});
