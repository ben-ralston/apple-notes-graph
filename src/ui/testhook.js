// Agent/test hook: the run-ui driver and future e2e tests drive the app
// through this object because the canvas graph has no DOM elements.
// Tasks extend it via extendTestHook — never assign window.__test directly.
export function extendTestHook(patch) {
  window.__test = Object.assign(window.__test ?? {}, patch);
}
