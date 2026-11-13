'use strict';
// Checks the packaged app runs before opening a window (issue #238). Electron and
// the filesystem are passed in so they can be tested; main.js supplies them.

const { constants } = require('fs');

const FULL_DISK_ACCESS_SETTINGS_URL =
  'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles';

// Returns a sentence describing what's missing, or null if the checkout is usable.
function checkoutProblem({ repoRoot, scriptsDir, venvPython }, { existsSync, accessSync }) {
  if (!repoRoot) return 'This build has no checkout location baked in.';
  if (!existsSync(repoRoot)) return `The checkout at ${repoRoot} doesn't exist.`;
  if (!existsSync(scriptsDir)) return `The scripts folder at ${scriptsDir} doesn't exist.`;
  try {
    accessSync(venvPython, constants.X_OK);
  } catch {
    return `The Python at ${venvPython} can't be run.`;
  }
  return null;
}

// Reading NoteStore.sqlite without Full Disk Access fails with one of these.
// A missing database (ENOENT) is a different problem with its own error path.
function isFullDiskAccessDenied(error) {
  return error?.code === 'EPERM' || error?.code === 'EACCES';
}

// Returns whether the app can carry on. Without its checkout or Full Disk Access
// nothing works, so say what to do instead of showing an empty graph.
async function runStartupChecks({ paths, noteStorePath, fsApi, showDialog, openSettings }) {
  const problem = checkoutProblem(paths, fsApi);
  if (problem) {
    await showDialog({
      type: 'error',
      message: "Notes Graph can't find its checkout",
      detail: `${problem}\n\nRun ./run ui-app-install from your notes-graph repo, then open the app again.`,
      buttons: ['Quit'],
    });
    return false;
  }

  try {
    fsApi.closeSync(fsApi.openSync(noteStorePath, 'r'));
  } catch (error) {
    if (isFullDiskAccessDenied(error)) {
      const { response } = await showDialog({
        type: 'warning',
        message: 'Notes Graph needs Full Disk Access',
        detail:
          "It reads your notes directly from Apple Notes' database. Turn on Notes Graph in " +
          "System Settings (add it with the + button if it isn't listed), then open the app again.",
        buttons: ['Open Privacy Settings', 'Quit'],
        defaultId: 0,
        cancelId: 1,
      });
      if (response === 0) await openSettings(FULL_DISK_ACCESS_SETTINGS_URL);
      return false;
    }
    // Anything else (e.g. a missing database) has its own error path after load.
  }
  return true;
}

module.exports = { checkoutProblem, isFullDiskAccessDenied, runStartupChecks };
