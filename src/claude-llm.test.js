import { describe, expect, test } from 'vitest';
import {
  buildClusterPrompt, buildRankPrompt, extractJson,
  parseClusterResponse, parseRankResponse,
} from './claude-llm.js';

describe('extractJson', () => {
  test('parses clean JSON', () => {
    expect(extractJson('{"a": 1}')).toEqual({ a: 1 });
  });
  test('parses fenced JSON', () => {
    expect(extractJson('```json\n{"a": 1}\n```')).toEqual({ a: 1 });
  });
  test('parses JSON wrapped in prose and trailing noise', () => {
    // Observed live: session hooks can append tags like <phase>adhoc</phase>.
    expect(extractJson('Here you go:\n{"a": {"b": 2}}\n<phase>adhoc</phase>')).toEqual({ a: { b: 2 } });
  });
  test('handles braces inside strings', () => {
    expect(extractJson('{"a": "curly } brace"} trailing')).toEqual({ a: 'curly } brace' });
  });
  test('returns null on garbage', () => {
    expect(extractJson('no json here')).toBe(null);
    expect(extractJson('{broken')).toBe(null);
    expect(extractJson(undefined)).toBe(null);
  });
});

describe('rank prompt/response', () => {
  const note = { noteName: 'Subject', noteFolder: 'Ideas', noteContent: 'body text' };
  const candidates = [
    { name: 'Alpha', folder: 'Ideas', reasons: [{ type: 'reverse_mention' }] },
    { name: 'Beta', folder: 'Work', reasons: [{ type: 'title_overlap' }] },
  ];
  test('prompt includes subject, candidates, and strict-JSON instruction', () => {
    const p = buildRankPrompt(note, candidates);
    expect(p).toContain('Subject');
    expect(p).toContain('Alpha');
    expect(p).toContain('"picks"');
    expect(p).toContain('ONLY');
  });
  test('parse keeps valid picks in order, drops hallucinated names and dupes, caps at 5', () => {
    const names = ['A', 'B', 'C', 'D', 'E', 'F', 'G'];
    const text = JSON.stringify({
      picks: [
        { name: 'B', reason: 'r1' }, { name: 'Nope', reason: 'x' },
        { name: 'B', reason: 'dupe' }, { name: 'A', reason: 'r2' },
        { name: 'C' }, { name: 'D', reason: 'r4' },
        { name: 'E', reason: 'r5' }, { name: 'F', reason: 'r6' },
      ],
    });
    const out = parseRankResponse(text, names);
    expect(out.picks.map((p) => p.name)).toEqual(['B', 'A', 'C', 'D', 'E']);
    expect(out.picks[2].reason).toBe('');
  });
  test('parse returns null on garbage or missing picks', () => {
    expect(parseRankResponse('nonsense', ['A'])).toBe(null);
    expect(parseRankResponse('{"other": []}', ['A'])).toBe(null);
  });
});

describe('cluster prompt/response', () => {
  const clusters = [
    { key: 'aaaa1111', members: ['Kitchen Remodel', 'Contractor Quotes'] },
    { key: 'bbbb2222', members: ['AI Safety', 'Alignment Reading'] },
  ];
  test('prompt includes keys, members, and strict-JSON instruction', () => {
    const p = buildClusterPrompt(clusters);
    expect(p).toContain('aaaa1111');
    expect(p).toContain('Kitchen Remodel');
    expect(p).toContain('"names"');
  });
  test('parse keeps only valid keys, trims and truncates labels', () => {
    const text = JSON.stringify({ names: {
      aaaa1111: '  Home Renovation  ',
      bbbb2222: 'X'.repeat(60),
      cccc3333: 'Hallucinated',
    } });
    const out = parseClusterResponse(text, ['aaaa1111', 'bbbb2222']);
    expect(out.names.aaaa1111).toBe('Home Renovation');
    expect(out.names.bbbb2222).toHaveLength(40);
    expect(out.names.cccc3333).toBeUndefined();
  });
  test('parse returns null on garbage, skips empty labels', () => {
    expect(parseClusterResponse('junk', ['a'])).toBe(null);
    const out = parseClusterResponse('{"names": {"a": "  "}}', ['a']);
    expect(out.names.a).toBeUndefined();
  });
});
