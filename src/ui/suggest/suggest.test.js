import { describe, expect, test } from 'vitest';
import { mergeRanking, reasonChips } from './suggest.js';

describe('reasonChips', () => {
  test('maps known reason types to short labels', () => {
    const chips = reasonChips([
      { type: 'forward_mention' }, { type: 'reverse_mention' }, { type: 'co_citation' },
      { type: 'title_overlap' }, { type: 'same_folder' }, { type: 'backlink_not_reciprocated' },
    ]);
    expect(chips).toEqual([
      'you mention it', 'mentions you', 'co-cited', 'title overlap', 'same folder', 'links to you',
    ]);
  });
  test('unknown types pass through, empty input is empty', () => {
    expect(reasonChips([{ type: 'novel_signal' }])).toEqual(['novel_signal']);
    expect(reasonChips(undefined)).toEqual([]);
  });
});

describe('mergeRanking', () => {
  const candidates = [{ name: 'A' }, { name: 'B' }, { name: 'C' }];
  test('picked cards first in pick order with reasons, rest keep recall order', () => {
    const out = mergeRanking(candidates, [
      { name: 'C', reason: 'why c' }, { name: 'A', reason: 'why a' },
    ]);
    expect(out.map((c) => c.name)).toEqual(['C', 'A', 'B']);
    expect(out[0]).toMatchObject({ ranked: true, reason: 'why c' });
    expect(out[2]).toMatchObject({ ranked: false });
  });
  test('unknown pick names ignored; no picks = recall order unranked', () => {
    expect(mergeRanking(candidates, [{ name: 'Zed', reason: 'x' }]).map((c) => c.name))
      .toEqual(['A', 'B', 'C']);
    expect(mergeRanking(candidates, undefined).every((c) => c.ranked === false)).toBe(true);
  });
});
