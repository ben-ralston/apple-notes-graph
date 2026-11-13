import { describe, expect, test } from 'vitest';
import { fuzzyMatch, fuzzySearch } from './fuzzy.js';

describe('fuzzyMatch', () => {
  test('null when query chars are not a subsequence', () => {
    expect(fuzzyMatch('xyz', 'Daily Briefing')).toBeNull();
  });

  test('case-insensitive subsequence matches', () => {
    expect(fuzzyMatch('dbrief', 'Daily Briefing')).not.toBeNull();
  });

  test('prefix match beats scattered match', () => {
    const prefix = fuzzyMatch('dai', 'Daily Briefing').score;
    const scattered = fuzzyMatch('dai', 'Board game ideas').score; // b-oarD-g-A-me-I
    expect(prefix).toBeGreaterThan(scattered);
  });

  test('consecutive chars beat spread-out chars', () => {
    const consecutive = fuzzyMatch('plan', 'Plans').score;
    const spread = fuzzyMatch('plan', 'P l a n spread').score;
    expect(consecutive).toBeGreaterThan(spread);
  });

  test('word-start chars are rewarded', () => {
    const wordStarts = fuzzyMatch('db', 'Daily Briefing').score;
    const midWord = fuzzyMatch('db', 'sandbox').score;
    expect(wordStarts).toBeGreaterThan(midWord);
  });

  test('returns matched positions for highlighting', () => {
    expect(fuzzyMatch('db', 'Daily Briefing').positions).toEqual([0, 6]);
  });

  test('empty query matches everything with zero score', () => {
    expect(fuzzyMatch('', 'anything')).toEqual({ score: 0, positions: [] });
  });
});

describe('fuzzySearch', () => {
  const notes = [{ name: 'Daily Briefing' }, { name: '★ Lists' }, { name: 'Dan dan noodles' }];

  test('returns sorted matches with items', () => {
    const results = fuzzySearch('d', notes, (n) => n.name);
    expect(results.length).toBe(2);
    expect(results.every((r) => r.item.name.toLowerCase().includes('d'))).toBe(true);
  });

  test('respects limit', () => {
    expect(fuzzySearch('n', notes, (n) => n.name, 1).length).toBe(1);
  });
});
