import { describe, expect, test } from 'vitest';
import { isEditableTarget } from './keyboard.js';

describe('isEditableTarget: where single-key shortcuts (F) must not fire', () => {
  test('text fields and contenteditable are editable', () => {
    expect(isEditableTarget({ tagName: 'INPUT' })).toBe(true);
    expect(isEditableTarget({ tagName: 'TEXTAREA' })).toBe(true);
    expect(isEditableTarget({ tagName: 'DIV', isContentEditable: true })).toBe(true);
  });

  test('a select takes type-ahead letters, so F picks an option there (#146)', () => {
    expect(isEditableTarget({ tagName: 'SELECT' })).toBe(true);
  });

  test('other elements and non-elements are not', () => {
    expect(isEditableTarget({ tagName: 'BUTTON' })).toBe(false);
    expect(isEditableTarget({ tagName: 'CANVAS', isContentEditable: false })).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
    expect(isEditableTarget(window_like())).toBe(false);
  });
});

function window_like() {
  return { document: {} };
}
