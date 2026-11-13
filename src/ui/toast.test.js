import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { dismissToast, showToast, toasts, updateToast } from './toast.js';

beforeEach(() => { vi.useFakeTimers(); toasts.set([]); });
afterEach(() => vi.useRealTimers());

describe('toast store', () => {
  test('showToast appends and returns a unique id', () => {
    const a = showToast('one', { spinner: true });
    const b = showToast('two');
    expect(a).not.toBe(b);
    expect(get(toasts).map((t) => t.text)).toEqual(['one', 'two']);
    expect(get(toasts)[0].spinner).toBe(true);
  });
  test('updateToast replaces text/spinner and auto-expires after ttl', () => {
    const id = showToast('working…', { spinner: true });
    updateToast(id, 'done', { ttlMs: 1000 });
    expect(get(toasts)[0]).toMatchObject({ text: 'done', spinner: false });
    vi.advanceTimersByTime(1001);
    expect(get(toasts)).toHaveLength(0);
  });
  test('dismissToast removes only the target', () => {
    const a = showToast('a'); showToast('b');
    dismissToast(a);
    expect(get(toasts).map((t) => t.text)).toEqual(['b']);
  });
});
