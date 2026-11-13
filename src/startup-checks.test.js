import { describe, it, expect, vi } from 'vitest';
import { checkoutProblem, isFullDiskAccessDenied, runStartupChecks } from './startup-checks.js';

const paths = {
  repoRoot: '/checkout',
  scriptsDir: '/checkout/scripts',
  venvPython: '/checkout/.venv/bin/python3',
};
const allExist = () => true;
const executable = () => {};

describe('checkoutProblem', () => {
  it('returns null when the checkout is complete', () => {
    expect(checkoutProblem(paths, { existsSync: allExist, accessSync: executable })).toBeNull();
  });

  it('reports a build with no baked repo root', () => {
    const problem = checkoutProblem(
      { repoRoot: null, scriptsDir: null, venvPython: null },
      { existsSync: allExist, accessSync: executable }
    );
    expect(problem).toMatch(/no checkout/i);
  });

  it('names a missing repo root', () => {
    const problem = checkoutProblem(paths, { existsSync: () => false, accessSync: executable });
    expect(problem).toContain('/checkout');
  });

  it('names a missing scripts directory', () => {
    const existsSync = (p) => p !== '/checkout/scripts';
    const problem = checkoutProblem(paths, { existsSync, accessSync: executable });
    expect(problem).toContain('/checkout/scripts');
  });

  it('names a Python that is not executable', () => {
    const accessSync = () => {
      throw Object.assign(new Error('nope'), { code: 'EACCES' });
    };
    const problem = checkoutProblem(paths, { existsSync: allExist, accessSync });
    expect(problem).toContain('/checkout/.venv/bin/python3');
  });
});

describe('isFullDiskAccessDenied', () => {
  it('treats EPERM and EACCES as denied', () => {
    expect(isFullDiskAccessDenied({ code: 'EPERM' })).toBe(true);
    expect(isFullDiskAccessDenied({ code: 'EACCES' })).toBe(true);
  });

  it('does not treat a missing database or no error as denied', () => {
    expect(isFullDiskAccessDenied({ code: 'ENOENT' })).toBe(false);
    expect(isFullDiskAccessDenied(null)).toBe(false);
  });
});

describe('runStartupChecks', () => {
  const NOTE_STORE = '/notes/NoteStore.sqlite';
  const SETTINGS_URL = 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles';

  function deps({ exists = true, openError = null, response = 1 } = {}) {
    return {
      paths,
      noteStorePath: NOTE_STORE,
      fsApi: {
        existsSync: () => exists,
        accessSync: () => {},
        openSync: vi.fn(() => {
          if (openError) throw Object.assign(new Error('open'), { code: openError });
          return 7;
        }),
        closeSync: vi.fn(),
      },
      showDialog: vi.fn(async () => ({ response })),
      openSettings: vi.fn(async () => {}),
    };
  }

  it('passes silently and closes the database when all is well', async () => {
    const d = deps();
    expect(await runStartupChecks(d)).toBe(true);
    expect(d.showDialog).not.toHaveBeenCalled();
    expect(d.fsApi.openSync).toHaveBeenCalledWith(NOTE_STORE, 'r');
    expect(d.fsApi.closeSync).toHaveBeenCalledWith(7);
  });

  it('explains a missing checkout and stops', async () => {
    const d = deps({ exists: false });
    expect(await runStartupChecks(d)).toBe(false);
    const [options] = d.showDialog.mock.calls[0];
    expect(options.message).toMatch(/can't find its checkout/);
    expect(options.detail).toContain('/checkout');
    expect(options.detail).toContain('./run ui-app-install');
    expect(options.buttons).toEqual(['Quit']);
    expect(d.fsApi.openSync).not.toHaveBeenCalled();
  });

  it.each(['EPERM', 'EACCES'])('asks for Full Disk Access on %s and stops', async (code) => {
    const d = deps({ openError: code });
    expect(await runStartupChecks(d)).toBe(false);
    const [options] = d.showDialog.mock.calls[0];
    expect(options.message).toMatch(/needs Full Disk Access/);
    expect(options.buttons).toEqual(['Open Privacy Settings', 'Quit']);
  });

  it('opens the right settings page only when asked', async () => {
    const yes = deps({ openError: 'EPERM', response: 0 });
    await runStartupChecks(yes);
    expect(yes.openSettings).toHaveBeenCalledWith(SETTINGS_URL);

    const no = deps({ openError: 'EPERM', response: 1 });
    await runStartupChecks(no);
    expect(no.openSettings).not.toHaveBeenCalled();
  });

  it('leaves a missing database to its own error path', async () => {
    const d = deps({ openError: 'ENOENT' });
    expect(await runStartupChecks(d)).toBe(true);
    expect(d.showDialog).not.toHaveBeenCalled();
  });
});
