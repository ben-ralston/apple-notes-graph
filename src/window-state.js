'use strict';
// Remembered window size and position for the packaged app (issue #238).

// How much of a saved window must be on a screen for it to stay put. The top
// edge must be on that screen too, or the title bar can't be reached to move it.
const MIN_VISIBLE_PX = 100;
const SAVE_DELAY_MS = 500;

function isPositiveNumber(value) {
  return Number.isFinite(value) && value > 0;
}

function parseWindowState(text) {
  let saved;
  try {
    saved = JSON.parse(text);
  } catch {
    return null;
  }
  if (saved === null || typeof saved !== 'object' || Array.isArray(saved)) return null;
  const { x, y, width, height, isMaximized } = saved;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  if (!isPositiveNumber(width) || !isPositiveNumber(height)) return null;
  return { x, y, width, height, isMaximized: isMaximized === true };
}

function overlap(startA, lengthA, startB, lengthB) {
  return Math.min(startA + lengthA, startB + lengthB) - Math.max(startA, startB);
}

function isVisibleOn({ x, y, width, height }, { workArea }) {
  return (
    y >= workArea.y &&
    overlap(x, width, workArea.x, workArea.width) >= MIN_VISIBLE_PX &&
    overlap(y, height, workArea.y, workArea.height) >= MIN_VISIBLE_PX
  );
}

function resolveBounds(saved, displays, defaults) {
  if (!saved || !displays.some((display) => isVisibleOn(saved, display))) {
    return { ...defaults, isMaximized: false };
  }
  const { x, y, width, height, isMaximized } = saved;
  return { x, y, width, height, isMaximized: isMaximized === true };
}

function trackWindowBounds(win, save) {
  let timer;
  const saveNow = () => {
    clearTimeout(timer);
    if (win.isDestroyed()) return;
    save({ ...win.getNormalBounds(), isMaximized: win.isMaximized() });
  };
  const saveSoon = () => {
    clearTimeout(timer);
    timer = setTimeout(saveNow, SAVE_DELAY_MS);
  };
  win.on('move', saveSoon);
  win.on('resize', saveSoon);
  win.on('close', saveNow);
}

module.exports = { parseWindowState, resolveBounds, trackWindowBounds };
