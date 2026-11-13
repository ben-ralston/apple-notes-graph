import { expect, test } from 'vitest';
import { escapeAppleScriptString } from './applescript-escape.js';

test('escapes double quotes', () => {
  expect(escapeAppleScriptString('say "hi"')).toBe('say \\"hi\\"');
});

test('escapes backslashes before quotes (NTG-001)', () => {
  // Input: a\"b — quote-only escaping would produce a\\"b, where the
  // backslash swallows the escape and the quote terminates the string.
  expect(escapeAppleScriptString('a\\"b')).toBe('a\\\\\\"b');
});

test('trailing backslash cannot swallow the closing quote', () => {
  expect(escapeAppleScriptString('title\\')).toBe('title\\\\');
});

test('plain titles pass through unchanged', () => {
  expect(escapeAppleScriptString('Weekly Plan')).toBe('Weekly Plan');
});
