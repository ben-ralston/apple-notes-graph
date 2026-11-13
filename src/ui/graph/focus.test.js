import { describe, expect, test } from 'vitest';
import { neighborhood } from './focus.js';

const links = [
  { source: 1, target: 2 }, { source: 2, target: 3 },
  { source: 3, target: 4 }, { source: 5, target: 6 },
];

describe('neighborhood', () => {
  test('1 hop = root + direct neighbors', () => {
    expect(neighborhood(links, 2, 1).noteIds).toEqual(new Set([1, 2, 3]));
  });
  test('2 hops reaches further, disconnected notes never included', () => {
    const ids = neighborhood(links, 2, 2).noteIds;
    expect(ids).toEqual(new Set([1, 2, 3, 4]));
    expect(ids.has(5)).toBe(false);
  });
  test('handles d3-mutated links where source is an object', () => {
    const mutated = [{ source: { id: 1 }, target: { id: 2 } }];
    expect(neighborhood(mutated, 1, 1).noteIds).toEqual(new Set([1, 2]));
  });
});
