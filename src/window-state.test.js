import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { parseWindowState, resolveBounds, trackWindowBounds } from './window-state.js';

const defaults = { width: 1400, height: 900 };
const laptop = { workArea: { x: 0, y: 25, width: 1920, height: 1055 } };

describe('parseWindowState', () => {
  it('reads a valid state', () => {
    const saved = parseWindowState('{"x":10,"y":20,"width":800,"height":600,"isMaximized":true}');
    expect(saved).toEqual({ x: 10, y: 20, width: 800, height: 600, isMaximized: true });
  });

  it('returns null for truncated or non-object JSON', () => {
    expect(parseWindowState('{"x":1')).toBeNull();
    expect(parseWindowState('[]')).toBeNull();
    expect(parseWindowState('null')).toBeNull();
  });

  it('returns null for non-numeric or non-positive bounds', () => {
    expect(parseWindowState('{"x":"a","y":0,"width":800,"height":600}')).toBeNull();
    expect(parseWindowState('{"x":0,"y":0,"width":0,"height":600}')).toBeNull();
  });
});

describe('resolveBounds', () => {
  it('uses the defaults with no position when nothing is saved', () => {
    expect(resolveBounds(null, [laptop], defaults)).toEqual({ ...defaults, isMaximized: false });
  });

  it('falls back to the defaults for a window on an unplugged monitor', () => {
    const saved = { x: 3000, y: 100, width: 800, height: 600, isMaximized: false };
    expect(resolveBounds(saved, [laptop], defaults)).toEqual({ ...defaults, isMaximized: false });
  });

  it('keeps a window that overlaps a screen by at least 100x100', () => {
    const saved = { x: 1820, y: 100, width: 800, height: 600, isMaximized: false };
    expect(resolveBounds(saved, [laptop], defaults)).toEqual(saved);
  });

  it('falls back when the overlap is only 50x50', () => {
    const saved = { x: 1870, y: 100, width: 800, height: 600, isMaximized: false };
    expect(resolveBounds(saved, [laptop], defaults)).toEqual({ ...defaults, isMaximized: false });
  });

  it('falls back when the title bar would be above the screen', () => {
    const saved = { x: 100, y: -1200, width: 800, height: 1400, isMaximized: false };
    expect(resolveBounds(saved, [laptop], defaults)).toEqual({ ...defaults, isMaximized: false });
  });

  it('falls back when the title bar would be below the screen', () => {
    const saved = { x: 100, y: 1060, width: 800, height: 600, isMaximized: false };
    expect(resolveBounds(saved, [laptop], defaults)).toEqual({ ...defaults, isMaximized: false });
  });

  it('checks every display', () => {
    const external = { workArea: { x: 1920, y: 0, width: 2560, height: 1440 } };
    const saved = { x: 3000, y: 100, width: 800, height: 600, isMaximized: false };
    expect(resolveBounds(saved, [laptop, external], defaults)).toEqual(saved);
  });

  it('preserves maximized and treats a missing flag as false', () => {
    const maximized = { x: 10, y: 50, width: 800, height: 600, isMaximized: true };
    expect(resolveBounds(maximized, [laptop], defaults).isMaximized).toBe(true);
    const { isMaximized: _omitted, ...noFlag } = maximized;
    expect(resolveBounds(noFlag, [laptop], defaults).isMaximized).toBe(false);
  });
});

describe('trackWindowBounds', () => {
  function fakeWindow() {
    const handlers = {};
    return {
      handlers,
      destroyed: false,
      on(name, handler) { handlers[name] = handler; },
      isDestroyed() { return this.destroyed; },
      getNormalBounds: () => ({ x: 1, y: 2, width: 300, height: 400 }),
      isMaximized: () => true,
    };
  }

  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('saves once after a burst of moves and resizes', () => {
    const win = fakeWindow();
    const save = vi.fn();
    trackWindowBounds(win, save);
    win.handlers.move();
    win.handlers.resize();
    win.handlers.move();
    expect(save).not.toHaveBeenCalled();
    vi.advanceTimersByTime(500);
    expect(save).toHaveBeenCalledOnce();
    expect(save).toHaveBeenCalledWith({ x: 1, y: 2, width: 300, height: 400, isMaximized: true });
  });

  it('saves straight away on close and cancels the pending save', () => {
    const win = fakeWindow();
    const save = vi.fn();
    trackWindowBounds(win, save);
    win.handlers.move();
    win.handlers.close();
    expect(save).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(1000);
    expect(save).toHaveBeenCalledOnce();
  });

  it('does not touch a window destroyed before the delay ends', () => {
    const win = fakeWindow();
    const save = vi.fn();
    trackWindowBounds(win, save);
    win.handlers.resize();
    win.destroyed = true;
    vi.advanceTimersByTime(1000);
    expect(save).not.toHaveBeenCalled();
  });
});
