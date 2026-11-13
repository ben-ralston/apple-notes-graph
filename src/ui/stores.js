import { derived, get, writable } from 'svelte/store';
import { neighborhood } from './graph/focus.js';
import { deriveGhosts, healthSets } from './graph/health.js';
import { seedPositions } from './graph/tween.js';
import { folderExcluder } from './folderFilter.js';

export const graph = writable({ notes: [], links: [], health: null, stats: null });
export const loadError = writable(null);

// Shared refresh: ⌘K "Refresh graph" command and post-add-link reload both use it.
// Sequenced so a slow stale extraction can never overwrite a newer result.
let refreshSeq = 0;
export async function refreshGraph() {
  const seq = ++refreshSeq;
  const data = await window.notesAPI.fetchNotes();
  if (seq !== refreshSeq) return false;
  if (data.error || !data.stats) {
    loadError.set(`Error: ${data.error ?? 'no data returned'}`);
    return false;
  }
  const notes = data.notes ?? [];
  seedPositions(notes, get(graph).notes);
  graph.set({ notes, links: data.links ?? [], health: data.health, stats: data.stats });
  loadError.set(null);
  return true;
}
export const selection = writable({ id: null, neighborIds: new Set() });
export const filters = writable({ excludeFolders: [], excludePattern: '' });
export const focus = writable({ rootId: null, hops: 2 });
export const paletteOpen = writable(false);
export const filterPopoverOpen = writable(false);
export const healthLens = writable(false);
// LLM-assigned cluster labels: {clusterId: name} (see clusterNaming.js).
export const clusterNames = writable({});

export function toggleHealthLens() {
  healthLens.update((on) => !on);
}

// Ghost nodes only exist under the lens — a selection pointing at one must
// not outlive the lens being switched off.
healthLens.subscribe((on) => {
  if (!on && typeof get(selection).id === 'string') clearSelection();
});

// Health lookups + ghost graph, derived from the payload independent of the
// lens toggle so ghost node objects (and their sim positions) stay stable
// across lens on/off flips.
export const healthOverlay = derived(graph, ($graph) => ({
  ...healthSets($graph.health),
  ...deriveGhosts($graph.notes, $graph.health?.broken_links),
}));

export const visibleGraph = derived(
  [graph, filters, focus, healthLens, healthOverlay],
  ([$graph, $filters, $focus, $healthLens, $overlay]) => {
  const folderHidden = folderExcluder($filters.excludeFolders);
  let regex = null;
  try {
    regex = $filters.excludePattern ? new RegExp($filters.excludePattern, 'i') : null;
  } catch { /* invalid pattern = no pattern filter */ }

  let notes = $graph.notes.filter((n) => {
    if (folderHidden(n.folder)) return false;
    if (regex && regex.test(n.name)) return false;
    return true;
  });
  const ids = new Set(notes.map((n) => n.id));
  let links = $graph.links.filter(
    (l) => ids.has(l.source.id ?? l.source) && ids.has(l.target.id ?? l.target)
  );

  if ($focus.rootId !== null) {
    const { noteIds } = neighborhood(links, $focus.rootId, $focus.hops);
    notes = notes.filter((n) => noteIds.has(n.id));
    links = links.filter(
      (l) => noteIds.has(l.source.id ?? l.source) && noteIds.has(l.target.id ?? l.target)
    );
  }

  if ($healthLens) {
    // Ghosts ride along with their (visible) source notes.
    const visibleIds = new Set(notes.map((n) => n.id));
    const ghostLinks = $overlay.ghostLinks.filter((l) => visibleIds.has(l.source.id ?? l.source));
    const linkedGhostIds = new Set(ghostLinks.map((l) => l.target.id ?? l.target));
    notes = [...notes, ...$overlay.ghostNodes.filter((g) => linkedGhostIds.has(g.id))];
    links = [...links, ...ghostLinks];
  }

  return { notes, links };
});

// One-shot view command consumed by GraphCanvas (seq retriggers repeat jumps
// to the same note). Currently only 'center' — the ⌘K jump fly-to.
export const viewCommand = writable(null);
let viewSeq = 0;
export function centerOnNode(id) {
  viewCommand.set({ type: 'center', id, seq: ++viewSeq });
}

export function selectNode(id) {
  const { links } = get(graph);
  const ghostLinks = get(healthLens) ? get(healthOverlay).ghostLinks : [];
  const neighborIds = new Set();
  for (const l of [...links, ...ghostLinks]) {
    const s = l.source.id ?? l.source;
    const t = l.target.id ?? l.target;
    if (s === id) neighborIds.add(t);
    if (t === id) neighborIds.add(s);
  }
  selection.set({ id, neighborIds });
}

export function clearSelection() {
  selection.set({ id: null, neighborIds: new Set() });
}

export function enterFocus(rootId) {
  // Ghosts have no real edges — focusing one would BFS to an empty graph.
  if (typeof rootId === 'string') return;
  focus.set({ rootId, hops: 2 });
}
export function exitFocus() { focus.set({ rootId: null, hops: 2 }); }
