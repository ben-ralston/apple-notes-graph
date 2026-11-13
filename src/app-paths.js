'use strict';
// Single source of truth for where the app finds things (issue #238).
// Bundled files (renderer, preload, icon) sit next to this module, inside the
// asar when packaged. Python lives in the repo checkout: the parent directory
// in dev, the root baked in by `./run ui-app-install` when packaged.

const path = require('path');

function resolveAppPaths({ isPackaged, baseDir, metadata }) {
  const repoRoot = isPackaged
    ? (metadata?.notesGraphRepoRoot ?? null)
    : path.join(baseDir, '..');
  const scriptsDir = repoRoot && path.join(repoRoot, 'scripts');

  return {
    repoRoot,
    scriptsDir,
    venvPython: repoRoot && path.join(repoRoot, '.venv', 'bin', 'python3'),
    script: (name) => scriptsDir && path.join(scriptsDir, name),
    distUiIndex: path.join(baseDir, '..', 'dist-ui', 'index.html'),
    preload: path.join(baseDir, 'preload.js'),
    dockIcon: path.join(baseDir, '..', 'assets', 'icon.png'),
  };
}

module.exports = { resolveAppPaths };
