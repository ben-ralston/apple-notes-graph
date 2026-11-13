import { describe, expect, test } from 'vitest';
import { deriveGhosts, duplicateCount, ghostId, healthSets, visibleHealthCounts } from './health.js';

const NOTES = [
  { id: 1, name: 'Alpha', folder: 'Keep' },
  { id: 2, name: 'Beta', folder: 'Keep' },
  { id: 3, name: 'Beta', folder: 'Other' }, // duplicate title
];

describe('healthSets', () => {
  test('null health yields empty sets', () => {
    const s = healthSets(null);
    expect(s.orphans.size).toBe(0);
    expect(s.stale.size).toBe(0);
    expect(s.dupCounts.size).toBe(0);
  });

  test('maps payload health block to lookup sets', () => {
    const s = healthSets({
      orphans: [1, 2],
      stale: [3],
      duplicate_titles: [{ name: 'Beta', count: 2 }],
      broken_links: [],
    });
    expect(s.orphans.has(1)).toBe(true);
    expect(s.orphans.has(3)).toBe(false);
    expect(s.stale.has(3)).toBe(true);
    expect(s.dupCounts.get('Beta')).toBe(2);
  });

  test('duplicate lookup matches display names against the lowercased payload', () => {
    // find_duplicate_names lowercases titles; nodes carry the display casing.
    const s = healthSets({
      orphans: [], stale: [], broken_links: [],
      duplicate_titles: [{ name: 'call recording', count: 4 }],
    });
    expect(duplicateCount(s.dupCounts, 'Call Recording')).toBe(4);
    expect(duplicateCount(s.dupCounts, 'Unrelated')).toBe(0);
  });
});

describe('deriveGhosts', () => {
  test('one ghost node per missing title, edges from resolved sources', () => {
    const { ghostNodes, ghostLinks } = deriveGhosts(NOTES, [
      { source_name: 'Alpha', intended_target_title: 'Missing', occurrences: 2 },
      { source_name: 'Beta', intended_target_title: 'Missing', occurrences: 1 },
    ]);
    expect(ghostNodes).toHaveLength(1);
    const ghost = ghostNodes[0];
    expect(ghost.id).toBe(ghostId('Missing'));
    expect(ghost.name).toBe('Missing');
    expect(ghost.ghost).toBe(true);
    expect(ghost.sources).toEqual([1, 2]); // 'Beta' resolves to first pk match
    expect(ghost.connections).toBe(2);
    expect(ghostLinks).toEqual([
      { source: 1, target: ghost.id, ghost: true },
      { source: 2, target: ghost.id, ghost: true },
    ]);
  });

  test('distinct missing titles get distinct ghosts', () => {
    const { ghostNodes } = deriveGhosts(NOTES, [
      { source_name: 'Alpha', intended_target_title: 'One', occurrences: 1 },
      { source_name: 'Alpha', intended_target_title: 'Two', occurrences: 1 },
    ]);
    expect(ghostNodes.map((g) => g.name).sort()).toEqual(['One', 'Two']);
  });

  test('source_pk (when present) wins over name resolution', () => {
    // Two notes share the title 'Beta' (pks 2 and 3); the payload's pk must
    // attach the ghost edge to the right one, not the first name match.
    const { ghostNodes, ghostLinks } = deriveGhosts(NOTES, [
      { source_pk: 3, source_name: 'Beta', intended_target_title: 'Missing', occurrences: 1 },
    ]);
    expect(ghostNodes[0].sources).toEqual([3]);
    expect(ghostLinks).toEqual([{ source: 3, target: ghostId('Missing'), ghost: true }]);
  });

  test('broken link whose source cannot be resolved is dropped', () => {
    const { ghostNodes, ghostLinks } = deriveGhosts(NOTES, [
      { source_name: 'Nope', intended_target_title: 'Missing', occurrences: 1 },
    ]);
    expect(ghostNodes).toHaveLength(0);
    expect(ghostLinks).toHaveLength(0);
  });

  test('same source repeated for one title yields a single edge', () => {
    const { ghostNodes, ghostLinks } = deriveGhosts(NOTES, [
      { source_name: 'Alpha', intended_target_title: 'Missing', occurrences: 1 },
      { source_name: 'Alpha', intended_target_title: 'Missing', occurrences: 3 },
    ]);
    expect(ghostNodes[0].sources).toEqual([1]);
    expect(ghostLinks).toHaveLength(1);
  });

  test('untitled broken targets stay distinct, keyed by target_uuid', () => {
    // Links created without alt text have intended_target_title: null —
    // they must not all collapse into one "ghost:null" node.
    const { ghostNodes } = deriveGhosts(NOTES, [
      { source_pk: 1, source_name: 'Alpha', intended_target_title: null, target_uuid: 'AAAA-1111', occurrences: 1 },
      { source_pk: 2, source_name: 'Beta', intended_target_title: null, target_uuid: 'BBBB-2222', occurrences: 1 },
    ]);
    expect(ghostNodes).toHaveLength(2);
    expect(ghostNodes[0].id).not.toBe(ghostNodes[1].id);
    expect(ghostNodes[0].name).not.toBe(ghostNodes[1].name);
    for (const g of ghostNodes) expect(typeof g.name).toBe('string');
  });

  test('empty inputs are safe', () => {
    const { ghostNodes, ghostLinks } = deriveGhosts([], []);
    expect(ghostNodes).toEqual([]);
    expect(ghostLinks).toEqual([]);
  });
});

describe('visibleHealthCounts', () => {
  test('counts only health findings within the visible scope', () => {
    const overlay = {
      orphans: new Set([1, 2, 99]),
      stale: new Set([2, 98]),
      dupCounts: new Map(),
      ghostNodes: [],
      ghostLinks: [
        { source: 1, target: ghostId('Missing'), ghost: true },
        { source: 99, target: ghostId('Elsewhere'), ghost: true }, // source filtered out
      ],
    };
    const visible = [{ id: 1 }, { id: 2 }];
    expect(visibleHealthCounts(visible, overlay)).toEqual({ orphans: 2, broken: 1, stale: 1 });
  });

  test('handles d3-mutated ghost link endpoints', () => {
    const overlay = {
      orphans: new Set(),
      stale: new Set(),
      dupCounts: new Map(),
      ghostNodes: [],
      ghostLinks: [{ source: { id: 1 }, target: { id: ghostId('Missing') }, ghost: true }],
    };
    expect(visibleHealthCounts([{ id: 1 }], overlay).broken).toBe(1);
  });
});
