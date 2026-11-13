import { describe, it, expect } from 'vitest';
import { formatModified } from './time.js';

const NOW = new Date('2026-08-09T12:00:00Z');

describe('formatModified', () => {
  it('returns "today" for a same-day timestamp', () => {
    expect(formatModified('2026-08-09T08:00:00Z', NOW)).toBe('today');
  });

  it('returns "yesterday" for one day ago', () => {
    expect(formatModified('2026-08-08T10:00:00Z', NOW)).toBe('yesterday');
  });

  it('buckets by local calendar day, not elapsed 24h windows', () => {
    // Local-time fixtures (no Z): modified yesterday 8 PM, viewed today
    // 10 AM — 14h elapsed but a different calendar day.
    const now = new Date('2026-08-09T10:00:00');
    expect(formatModified('2026-08-08T20:00:00', now)).toBe('yesterday');
    // Modified two calendar days ago but only 35h elapsed.
    expect(formatModified('2026-08-07T23:00:00', now)).toBe('2 days ago');
  });

  it('returns "N days ago" under 30 days', () => {
    expect(formatModified('2026-08-04T12:00:00Z', NOW)).toBe('5 days ago');
    expect(formatModified('2026-07-11T12:00:00Z', NOW)).toBe('29 days ago');
  });

  it('returns an absolute date at 30 days and beyond', () => {
    expect(formatModified('2026-07-10T12:00:00Z', NOW)).toBe('Jul 10, 2026');
    expect(formatModified('2024-03-15T12:00:00Z', NOW)).toBe('Mar 15, 2024');
  });

  it('treats future timestamps as "today"', () => {
    expect(formatModified('2026-08-10T00:00:00Z', NOW)).toBe('today');
  });

  it('returns null for null or missing input', () => {
    expect(formatModified(null, NOW)).toBeNull();
    expect(formatModified(undefined, NOW)).toBeNull();
  });

  it('returns null for unparseable input', () => {
    expect(formatModified('not-a-date', NOW)).toBeNull();
  });
});
