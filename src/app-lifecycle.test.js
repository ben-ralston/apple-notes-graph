import { describe, it, expect, vi } from 'vitest';
import {
  blockNavigation,
  claimSingleInstance,
  focusOrCreateWindow,
  startOrQuit,
  devUserDataPath,
} from './app-lifecycle.js';

function fakeWebContents(currentUrl) {
  const handlers = {};
  return {
    handlers,
    openHandler: null,
    on(name, handler) { handlers[name] = handler; },
    getURL: () => currentUrl,
    setWindowOpenHandler(handler) { this.openHandler = handler; },
  };
}

function fakeWindow({ minimized = false } = {}) {
  return {
    isMinimized: () => minimized,
    restore: vi.fn(),
    show: vi.fn(),
    focus: vi.fn(),
  };
}

describe('devUserDataPath', () => {
  it('keeps dev out of the installed app profile', () => {
    expect(devUserDataPath('/support')).toBe('/support/Notes Graph Dev');
  });
});

describe('blockNavigation', () => {
  it('blocks navigating to another page', () => {
    const webContents = fakeWebContents('file:///app/index.html');
    blockNavigation(webContents);
    const event = { preventDefault: vi.fn() };
    webContents.handlers['will-navigate'](event, 'https://example.com/');
    expect(event.preventDefault).toHaveBeenCalledOnce();
  });

  it('blocks a dropped local file', () => {
    const webContents = fakeWebContents('file:///app/index.html');
    blockNavigation(webContents);
    const event = { preventDefault: vi.fn() };
    webContents.handlers['will-navigate'](event, 'file:///Users/x/evil.html');
    expect(event.preventDefault).toHaveBeenCalledOnce();
  });

  it('allows reloading the same page, which dev HMR relies on', () => {
    const webContents = fakeWebContents('http://localhost:5173/');
    blockNavigation(webContents);
    const event = { preventDefault: vi.fn() };
    webContents.handlers['will-navigate'](event, 'http://localhost:5173/');
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it('denies new windows', () => {
    const webContents = fakeWebContents('file:///app/index.html');
    blockNavigation(webContents);
    expect(webContents.openHandler({ url: 'https://example.com/' })).toEqual({ action: 'deny' });
  });
});

describe('claimSingleInstance', () => {
  it('takes the lock when packaged', () => {
    const requestLock = vi.fn(() => true);
    expect(claimSingleInstance({ isPackaged: true, requestLock })).toBe(true);
    expect(requestLock).toHaveBeenCalledOnce();
  });

  it('reports a second instance when packaged', () => {
    expect(claimSingleInstance({ isPackaged: true, requestLock: () => false })).toBe(false);
  });

  it('never takes the lock in dev, so dev can run beside the installed app', () => {
    const requestLock = vi.fn(() => false);
    expect(claimSingleInstance({ isPackaged: false, requestLock })).toBe(true);
    expect(requestLock).not.toHaveBeenCalled();
  });
});

describe('focusOrCreateWindow', () => {
  it('does nothing until the startup checks have passed', () => {
    const createWindow = vi.fn();
    focusOrCreateWindow({ ready: false, windows: [], createWindow });
    expect(createWindow).not.toHaveBeenCalled();
  });

  it('creates a window when all are closed', () => {
    const createWindow = vi.fn();
    focusOrCreateWindow({ ready: true, windows: [], createWindow });
    expect(createWindow).toHaveBeenCalledOnce();
  });

  it('focuses an existing window without creating another', () => {
    const createWindow = vi.fn();
    const win = fakeWindow();
    focusOrCreateWindow({ ready: true, windows: [win], createWindow });
    expect(win.focus).toHaveBeenCalledOnce();
    expect(win.restore).not.toHaveBeenCalled();
    expect(createWindow).not.toHaveBeenCalled();
  });

  it('restores a minimized window before focusing it', () => {
    const win = fakeWindow({ minimized: true });
    focusOrCreateWindow({ ready: true, windows: [win], createWindow: vi.fn() });
    expect(win.restore).toHaveBeenCalledOnce();
    expect(win.show).toHaveBeenCalledOnce();
    expect(win.focus).toHaveBeenCalledOnce();
  });
});

describe('startOrQuit', () => {
  it('runs start once the app is ready and does not quit', async () => {
    const start = vi.fn(async () => {});
    const quit = vi.fn();
    await startOrQuit({ whenReady: async () => {}, start, quit, logError: vi.fn() });
    expect(start).toHaveBeenCalledOnce();
    expect(quit).not.toHaveBeenCalled();
  });

  it('quits and logs when start fails, so no windowless process keeps the lock', async () => {
    const quit = vi.fn();
    const logError = vi.fn();
    const failure = new Error('settings URL failed');
    await startOrQuit({
      whenReady: async () => {},
      start: async () => { throw failure; },
      quit,
      logError,
    });
    expect(quit).toHaveBeenCalledOnce();
    expect(logError).toHaveBeenCalledWith('Failed to start:', failure);
  });
});
