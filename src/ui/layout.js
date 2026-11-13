// Shared layout constants for chrome that the canvas has to work around.
export const INSPECTOR_WIDTH = 420;

const MIN_GRAPH_VISIBLE = 96;

// Keep in sync with the width rule in Inspector.svelte.
export function inspectorWidth(viewportWidth) {
  return Math.max(0, Math.min(INSPECTOR_WIDTH, viewportWidth - MIN_GRAPH_VISIBLE));
}
