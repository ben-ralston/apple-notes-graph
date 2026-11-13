import { describe, expect, test } from 'vitest';
import { demoRefusal, isDemoMode } from './demo-mode.js';

describe('demo mode', () => {
  test('is off unless the switch is set', () => {
    expect(isDemoMode({})).toBe(false);
    expect(isDemoMode({ NOTES_GRAPH_DEMO: '' })).toBe(false);
    expect(isDemoMode({ NOTES_GRAPH_DEMO: '0' })).toBe(false);
    expect(demoRefusal('Opening a note in Notes', {})).toBeNull();
  });

  test('is on for 1, true and yes', () => {
    for (const value of ['1', 'true', 'YES', ' 1 ']) {
      expect(isDemoMode({ NOTES_GRAPH_DEMO: value })).toBe(true);
    }
  });

  test('refuses with a message that names the action', () => {
    const refusal = demoRefusal('Deleting a note', { NOTES_GRAPH_DEMO: '1' });
    expect(refusal.demo).toBe(true);
    expect(refusal.error).toMatch(/^Deleting a note is not available in demo mode/);
  });
});
