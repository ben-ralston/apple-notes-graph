import { describe, expect, test } from 'vitest';
import { applyMoves, planMoves, seedPositions } from './tween.js';

describe('seedPositions', () => {
  test('copies positions from the previous notes by id', () => {
    const next = [{ id: 1 }, { id: 2 }];
    seedPositions(next, [{ id: 1, x: 10, y: 20 }]);
    expect(next[0]).toMatchObject({ x: 10, y: 20 });
    expect(next[1].x).toBeUndefined();
  });

  test('does not carry velocity or anything else across', () => {
    const next = [{ id: 1, name: 'new' }];
    seedPositions(next, [{ id: 1, name: 'old', x: 1, y: 2, vx: 9, vy: 9 }]);
    expect(next[0]).toEqual({ id: 1, name: 'new', x: 1, y: 2 });
  });

  test('remembers that a note was on the ring', () => {
    const next = [{ id: 1 }];
    seedPositions(next, [{ id: 1, x: 1, y: 2, onRing: true }]);
    expect(next[0].onRing).toBe(true);
  });

  test('tolerates previous notes that were never laid out', () => {
    const next = [{ id: 1 }];
    seedPositions(next, [{ id: 1 }]);
    expect(next[0].x).toBeUndefined();
  });
});

describe('planMoves and applyMoves', () => {
  test('starts moved nodes at their old position and ends at the new one', () => {
    const n = { id: 1, x: 100, y: 200 };
    const moves = planMoves([n], new Map([[1, { x: 0, y: 0 }]]));
    expect(n).toMatchObject({ x: 0, y: 0 });
    applyMoves(moves, 0.5);
    expect(n.x).toBeCloseTo(50);
    expect(n.y).toBeCloseTo(100);
    applyMoves(moves, 1);
    expect(n).toMatchObject({ x: 100, y: 200 });
  });

  test('a note with no earlier position stays where the layout put it', () => {
    const n = { id: 2, x: 5, y: 6 };
    expect(planMoves([n], new Map())).toEqual([]);
    expect(n).toMatchObject({ x: 5, y: 6 });
  });

  test('a note that did not move needs no animation', () => {
    const n = { id: 3, x: 5, y: 6 };
    expect(planMoves([n], new Map([[3, { x: 5, y: 6 }]]))).toEqual([]);
  });
});
