import { describe, expect, test } from 'vitest';
import {
  assembleClusters, clusterCentroids, clusterKey, pruneCache, splitCached,
} from './clusterNames.js';

const note = (id, cluster, connections = 1, name = `N${id}`) => ({ id, cluster, connections, name });

describe('clusterKey', () => {
  test('stable and order-insensitive', () => {
    expect(clusterKey([3, 1, 2])).toBe(clusterKey([1, 2, 3]));
    expect(clusterKey([1, 2, 3])).toMatch(/^[0-9a-f]{8}$/);
  });
  test('membership change changes the key', () => {
    expect(clusterKey([1, 2, 3])).not.toBe(clusterKey([1, 2, 4]));
    expect(clusterKey([1, 2, 3])).not.toBe(clusterKey([1, 2]));
  });
});

describe('assembleClusters', () => {
  test('excludes orphans, applies minSize, sorts members by connections', () => {
    const notes = [
      note(1, 0, 5, 'Hub'), note(2, 0, 1, 'Leaf'), note(3, 0, 3, 'Mid'),
      note(4, 1, 1), note(5, 1, 1),          // size 2 < minSize
      note(6, -1, 0), note(7, null, 0),      // orphans / missing cluster
    ];
    const out = assembleClusters(notes, { minSize: 3 });
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe(0);
    expect(out[0].size).toBe(3);
    expect(out[0].members).toEqual(['Hub', 'Mid', 'Leaf']);
    expect(out[0].key).toBe(clusterKey([1, 2, 3]));
  });
  test('caps cluster count (largest first) and member list length', () => {
    const notes = [];
    for (let c = 0; c < 15; c++) {
      for (let m = 0; m < 3 + (14 - c); m++) notes.push(note(c * 100 + m, c, m));
    }
    const out = assembleClusters(notes, { minSize: 3, maxClusters: 12, maxMembers: 4 });
    expect(out).toHaveLength(12);
    expect(out[0].size).toBeGreaterThanOrEqual(out[11].size);
    expect(out.every((c) => c.members.length <= 4)).toBe(true);
  });
});

describe('clusterCentroids', () => {
  test('averages member positions, skips ghosts/orphans/unpositioned', () => {
    const nodes = [
      { id: 1, cluster: 0, x: 0, y: 0 }, { id: 2, cluster: 0, x: 10, y: 20 },
      { id: 3, cluster: -1, x: 5, y: 5 }, { id: 4, cluster: 0, ghost: true, x: 99, y: 99 },
      { id: 5, cluster: 1 }, // no position yet
    ];
    const map = clusterCentroids(nodes);
    expect(map.get(0)).toEqual({ x: 5, y: 10, count: 2 });
    expect(map.has(1)).toBe(false);
    expect(map.has(-1)).toBe(false);
  });
});

describe('cache helpers', () => {
  test('splitCached separates named from unnamed', () => {
    const clusters = [
      { id: 0, key: 'k0' }, { id: 1, key: 'k1' },
    ];
    const { named, unnamed } = splitCached(clusters, { k0: 'Zero' });
    expect(named).toEqual({ 0: 'Zero' });
    expect(unnamed).toEqual([{ id: 1, key: 'k1' }]);
  });
  test('pruneCache keeps only present keys', () => {
    expect(pruneCache({ a: 'A', b: 'B' }, ['a', 'c'])).toEqual({ a: 'A' });
  });
});
