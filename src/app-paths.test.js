import { describe, it, expect } from 'vitest';
import path from 'path';
import { resolveAppPaths } from './app-paths.js';

const baseDir = '/repo/src';

describe('resolveAppPaths', () => {
  it('uses the parent of baseDir as the repo root in dev', () => {
    const paths = resolveAppPaths({ isPackaged: false, baseDir, metadata: {} });
    expect(paths.repoRoot).toBe('/repo');
    expect(paths.venvPython).toBe('/repo/.venv/bin/python3');
    expect(paths.scriptsDir).toBe('/repo/scripts');
    expect(paths.script('read_note.py')).toBe('/repo/scripts/read_note.py');
  });

  it('ignores a baked root in dev', () => {
    const paths = resolveAppPaths({
      isPackaged: false,
      baseDir,
      metadata: { notesGraphRepoRoot: '/elsewhere' },
    });
    expect(paths.repoRoot).toBe('/repo');
  });

  it('uses the baked root for Python paths when packaged', () => {
    const asarDir = '/Applications/Notes Graph.app/Contents/Resources/app.asar/src';
    const paths = resolveAppPaths({
      isPackaged: true,
      baseDir: asarDir,
      metadata: { notesGraphRepoRoot: '/checkout' },
    });
    expect(paths.repoRoot).toBe('/checkout');
    expect(paths.venvPython).toBe('/checkout/.venv/bin/python3');
    expect(paths.script('extract_notes.py')).toBe('/checkout/scripts/extract_notes.py');
  });

  it('keeps bundled files inside the asar when packaged', () => {
    const asarDir = '/Applications/Notes Graph.app/Contents/Resources/app.asar/src';
    const paths = resolveAppPaths({
      isPackaged: true,
      baseDir: asarDir,
      metadata: { notesGraphRepoRoot: '/checkout' },
    });
    expect(paths.distUiIndex).toBe(path.join(asarDir, '..', 'dist-ui', 'index.html'));
    expect(paths.preload).toBe(path.join(asarDir, 'preload.js'));
    expect(paths.dockIcon).toBe(path.join(asarDir, '..', 'assets', 'icon.png'));
  });

  it('reports no Python paths when packaged with no baked root', () => {
    const paths = resolveAppPaths({ isPackaged: true, baseDir, metadata: {} });
    expect(paths.repoRoot).toBeNull();
    expect(paths.venvPython).toBeNull();
    expect(paths.scriptsDir).toBeNull();
    expect(paths.script('read_note.py')).toBeNull();
  });

  it('keeps spaces in a baked root intact', () => {
    const root = '/Users/x/My Repos/notes graph';
    const paths = resolveAppPaths({
      isPackaged: true,
      baseDir,
      metadata: { notesGraphRepoRoot: root },
    });
    expect(paths.venvPython).toBe(`${root}/.venv/bin/python3`);
    expect(paths.script('read_note.py')).toBe(`${root}/scripts/read_note.py`);
  });
});
