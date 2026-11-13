import { describe, expect, test } from 'vitest';
import { mergeSemantic } from './semantic.js';

const notes = [
  { id: 1, name: 'Alpha' }, { id: 2, name: 'Beta' }, { id: 3, name: 'Gamma' },
];

describe('mergeSemantic', () => {
  test('drops hits already in fuzzy results and hits absent from the graph', () => {
    const fuzzy = [{ item: notes[0], score: 10, positions: [] }];
    const semantic = [
      { pk: 1, name: 'Alpha', score: 0.9 },   // dup of fuzzy hit
      { pk: 2, name: 'Beta', score: 0.8 },
      { pk: 99, name: 'Deleted', score: 0.7 }, // not in graph
    ];
    const out = mergeSemantic(fuzzy, semantic, notes);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ item: notes[1], semantic: true, score: 0.8 });
    expect(out[0].positions).toEqual([]);
  });
  test('empty/undefined semantic input yields empty', () => {
    expect(mergeSemantic([], undefined, notes)).toEqual([]);
  });
});
