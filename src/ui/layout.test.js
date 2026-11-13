import { describe, expect, test } from 'vitest';
import { INSPECTOR_WIDTH, inspectorWidth } from './layout.js';

describe('inspectorWidth', () => {
  test('is the full panel width in a normal window', () => {
    expect(inspectorWidth(1166)).toBe(INSPECTOR_WIDTH);
    expect(INSPECTOR_WIDTH).toBe(420);
  });

  test('leaves some graph visible in a small window', () => {
    expect(inspectorWidth(400)).toBe(304);
  });

  test('never goes negative', () => {
    expect(inspectorWidth(50)).toBe(0);
  });
});
