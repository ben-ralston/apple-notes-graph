'use strict';
// App lifecycle rules for the installed app (issue #238). Electron objects are
// passed in so these can be tested without it; main.js connects them.

const path = require('path');

// The preload gives whatever page a window shows access to notesAPI, and the
// installed app holds Full Disk Access, so a dropped URL or file must not replace
// the UI. Nothing in the UI navigates; same-URL reloads stay allowed for dev HMR.
function blockNavigation(webContents) {
  webContents.on('will-navigate', (event, url) => {
    if (url !== webContents.getURL()) event.preventDefault();
  });
  webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
}

// Only the installed app is single-instance: dev runs beside it and other worktrees.
function claimSingleInstance({ isPackaged, requestLock }) {
  return !isPackaged || requestLock();
}

// `ready` stays false while a startup dialog is open, so a Dock click or second
// launch can't open a window behind it.
function focusOrCreateWindow({ ready, windows, createWindow }) {
  if (!ready) return;
  const [win] = windows;
  if (!win) {
    createWindow();
    return;
  }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

// A failed start must quit: a windowless process would hold the single-instance
// lock, so relaunching would do nothing and the install command would refuse to run.
function startOrQuit({ whenReady, start, quit, logError }) {
  return whenReady().then(start).catch((error) => {
    logError('Failed to start:', error);
    quit();
  });
}

// Dev gets its own profile: sharing one with the installed app would have the
// two Chromium instances fighting over the same storage.
function devUserDataPath(appDataDir) {
  return path.join(appDataDir, 'Notes Graph Dev');
}

module.exports = {
  blockNavigation,
  claimSingleInstance,
  focusOrCreateWindow,
  startOrQuit,
  devUserDataPath,
};
