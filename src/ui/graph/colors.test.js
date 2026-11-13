import { describe, expect, test } from 'vitest';
import { CLUSTER_PALETTE, GHOST_COLOR, ORPHAN_COLOR, nodeBaseAlpha, nodeColor, nodeRadius, drawRadius } from './colors.js';

describe('nodeColor', () => {
  test('orphan cluster -1 is gray', () => {
    expect(nodeColor({ cluster: -1, connections: 0 })).toBe(ORPHAN_COLOR);
  });
  test('cluster id indexes the palette, wrapping', () => {
    expect(nodeColor({ cluster: 0, connections: 3 })).toBe(CLUSTER_PALETTE[0]);
    expect(nodeColor({ cluster: 12, connections: 3 })).toBe(CLUSTER_PALETTE[0]);
    expect(nodeColor({ cluster: 13, connections: 3 })).toBe(CLUSTER_PALETTE[1]);
  });
});

describe('ghost nodes', () => {
  // Ghosts have a string id and no cluster; the base formulas would yield
  // NaN on them, so each styling function needs an explicit ghost branch.
  const ghost = { id: 'ghost:Missing', name: 'Missing', ghost: true, connections: 2 };
  test('ghost color is the dedicated broken-link red', () => {
    expect(GHOST_COLOR).toMatch(/^#/);
    expect(nodeColor(ghost)).toBe(GHOST_COLOR);
  });
  test('ghosts render smaller than a real note of equal degree', () => {
    expect(nodeRadius(ghost)).toBeLessThan(nodeRadius({ cluster: 0, connections: 2 }));
    expect(Number.isFinite(nodeBaseAlpha(ghost))).toBe(true);
    expect(nodeBaseAlpha(ghost)).toBeLessThan(1);
  });
});

describe('nodeRadius', () => {
  test('orphans are smallest, radius grows with connections and caps', () => {
    const orphan = nodeRadius({ cluster: -1, connections: 0 });
    const small = nodeRadius({ cluster: 0, connections: 1 });
    const hub = nodeRadius({ cluster: 0, connections: 82 });
    const megahub = nodeRadius({ cluster: 0, connections: 300 });
    expect(orphan).toBeLessThan(small);
    expect(small).toBeLessThan(hub);
    expect(hub).toBe(megahub); // capped
  });
});

describe('drawRadius', () => {
  const hub = { id: 1, cluster: 0, connections: 25 };
  const leaf = { id: 2, cluster: 0, connections: 1 };
  const orphan = { id: 4, cluster: -1, connections: 0 };

  test('matches nodeRadius at zoom 1', () => {
    expect(drawRadius(hub, 1)).toBeCloseTo(nodeRadius(hub));
    expect(drawRadius(leaf, 1)).toBeCloseTo(nodeRadius(leaf));
  });

  test('grows far more slowly than the zoom when zoomed in', () => {
    const screen = drawRadius(hub, 5) * 5;
    expect(screen).toBeLessThan(nodeRadius(hub) * 2.8);
    expect(screen).toBeGreaterThan(nodeRadius(hub));
  });

  test('keeps notes visible when zoomed far out', () => {
    expect(drawRadius(leaf, 0.15) * 0.15).toBeGreaterThanOrEqual(2.5);
  });

  test('keeps unlinked dots smaller than linked notes when zoomed far out', () => {
    expect(drawRadius(orphan, 0.15)).toBeLessThan(drawRadius(leaf, 0.15));
  });
});
