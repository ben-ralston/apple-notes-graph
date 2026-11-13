import { describe, expect, it } from 'vitest';
import { JUMP_ZOOM, centerTransform, fitTransform } from './view.js';

describe('centerTransform', () => {
  it('centers the node at JUMP_ZOOM when zoomed out', () => {
    const t = centerTransform({ x: 100, y: 50 }, 0.4, 1200, 800);
    expect(t.k).toBe(JUMP_ZOOM);
    // node position maps to the canvas center: node * k + translate = center
    expect(100 * t.k + t.x).toBeCloseTo(600);
    expect(50 * t.k + t.y).toBeCloseTo(400);
  });

  it('keeps the current zoom when already closer than JUMP_ZOOM', () => {
    const t = centerTransform({ x: -20, y: 300 }, 3, 1000, 600);
    expect(t.k).toBe(3);
    expect(-20 * t.k + t.x).toBeCloseTo(500);
    expect(300 * t.k + t.y).toBeCloseTo(300);
  });

  it('handles a node at the origin', () => {
    const t = centerTransform({ x: 0, y: 0 }, 1, 800, 600);
    expect(t.x).toBeCloseTo(400);
    expect(t.y).toBeCloseTo(300);
  });
});

describe('centerTransform with a right inset', () => {
  it('centres the node in the area left of the panel', () => {
    const t = centerTransform({ x: 100, y: 50 }, 1, 1200, 800, 420);
    expect(100 * t.k + t.x).toBeCloseTo((1200 - 420) / 2);
    expect(50 * t.k + t.y).toBeCloseTo(400);
  });
});

describe('fitTransform', () => {
  const screen = (n, t) => ({ x: n.x * t.k + t.x, y: n.y * t.k + t.y });
  const spread = [{ x: -500, y: -200 }, { x: 900, y: 100 }, { x: 200, y: 700 }];

  it('contains every node within the padding', () => {
    const t = fitTransform(spread, 1166, 723, { padding: 64 });
    for (const n of spread) {
      const p = screen(n, t);
      expect(p.x).toBeGreaterThanOrEqual(64 - 1e-6);
      expect(p.x).toBeLessThanOrEqual(1166 - 64 + 1e-6);
      expect(p.y).toBeGreaterThanOrEqual(64 - 1e-6);
      expect(p.y).toBeLessThanOrEqual(723 - 64 + 1e-6);
    }
  });

  it('centres the graph in the window', () => {
    const t = fitTransform(spread, 1166, 723);
    const xs = spread.map((n) => screen(n, t).x);
    const ys = spread.map((n) => screen(n, t).y);
    expect((Math.min(...xs) + Math.max(...xs)) / 2).toBeCloseTo(1166 / 2);
    expect((Math.min(...ys) + Math.max(...ys)) / 2).toBeCloseTo(723 / 2);
  });

  it('caps k at maxK for a tiny graph', () => {
    const t = fitTransform([{ x: 0, y: 0 }, { x: 10, y: 10 }], 1166, 723, { maxK: 1.5 });
    expect(t.k).toBe(1.5);
  });

  it('floors k at minK for a huge graph', () => {
    const t = fitTransform([{ x: -1e6, y: 0 }, { x: 1e6, y: 0 }], 1166, 723, { minK: 0.15 });
    expect(t.k).toBe(0.15);
  });

  it('gives finite values for a single node', () => {
    const t = fitTransform([{ x: 40, y: 40 }], 1166, 723);
    expect(Number.isFinite(t.k)).toBe(true);
    const p = screen({ x: 40, y: 40 }, t);
    expect(p.x).toBeCloseTo(1166 / 2);
    expect(p.y).toBeCloseTo(723 / 2);
  });

  it('gives finite values when the canvas has no size', () => {
    const t = fitTransform(spread, 0, 0);
    expect(Number.isFinite(t.k) && Number.isFinite(t.x) && Number.isFinite(t.y)).toBe(true);
    expect(t.k).toBeGreaterThan(0);
  });

  it('returns identity for no nodes', () => {
    expect(fitTransform([], 1166, 723)).toEqual({ k: 1, x: 0, y: 0 });
  });

  it('skips nodes without a position yet', () => {
    const t = fitTransform([{ x: 0, y: 0 }, { x: 100, y: 100 }, {}], 1166, 723);
    expect(Number.isFinite(t.k) && Number.isFinite(t.x) && Number.isFinite(t.y)).toBe(true);
  });

  it('fits into the area left of the panel with a right inset', () => {
    const t = fitTransform(spread, 1166, 723, { padding: 64, rightInset: 420 });
    for (const n of spread) expect(screen(n, t).x).toBeLessThanOrEqual(1166 - 420 - 64 + 1e-6);
  });
});
