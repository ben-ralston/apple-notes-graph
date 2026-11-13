'use strict';
// Demo mode (NOTES_GRAPH_DEMO=1, set by ./run demo-ui): the app is showing a
// synthetic library, so nothing may drive the real Notes app. Every IPC
// handler that would (open, add link, delete) asks here first. The Python
// scripts refuse on their own as well (apple_notes/demo_mode.py).

const DEMO_ENV = 'NOTES_GRAPH_DEMO';

function isDemoMode(env = process.env) {
  return ['1', 'true', 'yes'].includes(String(env[DEMO_ENV] ?? '').trim().toLowerCase());
}

// Returns {error} when the action must be refused, or null to carry on.
function demoRefusal(action, env = process.env) {
  if (!isDemoMode(env)) return null;
  return {
    error: `${action} is not available in demo mode: the demo library is read-only ` +
      'and is never connected to the Notes app.',
    demo: true,
  };
}

module.exports = { DEMO_ENV, isDemoMode, demoRefusal };
