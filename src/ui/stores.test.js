import { afterEach, describe, expect, test } from 'vitest';
import { get } from 'svelte/store';
import {
  graph, filters, focus, visibleGraph, enterFocus, exitFocus,
  healthLens, toggleHealthLens, selection, selectNode,
  loadError, refreshGraph,
} from './stores.js';
import { ghostId } from './graph/health.js';

describe('visibleGraph: focus respects active filters', () => {
  test('BFS cannot traverse a link through a filtered-out intermediate note', () => {
    graph.set({
      notes: [
        { id: 1, name: 'Root', folder: 'Keep' },
        { id: 2, name: 'Intermediate', folder: 'Excluded' },
        { id: 3, name: 'TwoHop', folder: 'Keep' },
      ],
      links: [
        { source: 1, target: 2 },
        { source: 2, target: 3 },
      ],
      health: null,
      stats: null,
    });
    filters.set({ excludeFolders: ['Excluded'], excludePattern: '' });
    enterFocus(1);

    const ids = get(visibleGraph).notes.map((n) => n.id);
    expect(ids).toContain(1);
    expect(ids).not.toContain(2); // filtered out directly
    expect(ids).not.toContain(3); // only reachable via the filtered-out intermediate

    exitFocus();
    filters.set({ excludeFolders: [], excludePattern: '' });
  });
});

describe('health lens', () => {
  const HEALTH_GRAPH = {
    notes: [
      { id: 1, name: 'Alpha', folder: 'Keep' },
      { id: 2, name: 'Beta', folder: 'Excluded' },
    ],
    links: [{ source: 1, target: 2 }],
    health: {
      orphans: [],
      stale: [],
      duplicate_titles: [],
      broken_links: [
        { source_name: 'Alpha', intended_target_title: 'Missing', occurrences: 1 },
        { source_name: 'Beta', intended_target_title: 'Gone', occurrences: 1 },
      ],
    },
    stats: null,
  };

  afterEach(() => {
    healthLens.set(false);
    filters.set({ excludeFolders: [], excludePattern: '' });
    graph.set({ notes: [], links: [], health: null, stats: null });
    selection.set({ id: null, neighborIds: new Set() });
  });

  test('lens off: visibleGraph carries no ghosts', () => {
    graph.set(HEALTH_GRAPH);
    const { notes, links } = get(visibleGraph);
    expect(notes.some((n) => n.ghost)).toBe(false);
    expect(links.some((l) => l.ghost)).toBe(false);
  });

  test('lens on: ghost nodes and dashed edges appear', () => {
    graph.set(HEALTH_GRAPH);
    toggleHealthLens();
    const { notes, links } = get(visibleGraph);
    expect(notes.find((n) => n.id === ghostId('Missing'))).toBeTruthy();
    expect(links).toContainEqual(
      expect.objectContaining({ source: 1, target: ghostId('Missing'), ghost: true })
    );
  });

  test('ghosts whose only source is filtered out disappear with it', () => {
    graph.set(HEALTH_GRAPH);
    healthLens.set(true);
    filters.set({ excludeFolders: ['Excluded'], excludePattern: '' });
    const { notes } = get(visibleGraph);
    expect(notes.find((n) => n.id === ghostId('Missing'))).toBeTruthy();
    expect(notes.find((n) => n.id === ghostId('Gone'))).toBeFalsy();
  });

  test('selecting a ghost lights its source notes', () => {
    graph.set(HEALTH_GRAPH);
    healthLens.set(true);
    selectNode(ghostId('Missing'));
    expect(get(selection).neighborIds.has(1)).toBe(true);
  });

  test('a ghost cannot anchor focus mode (F on a ghost is a no-op)', () => {
    graph.set(HEALTH_GRAPH);
    healthLens.set(true);
    enterFocus(ghostId('Missing'));
    expect(get(focus).rootId).toBe(null);
    expect(get(visibleGraph).notes.length).toBeGreaterThan(0);
  });

  test('turning the lens off clears a selected ghost', () => {
    graph.set(HEALTH_GRAPH);
    healthLens.set(true);
    selectNode(ghostId('Missing'));
    healthLens.set(false);
    expect(get(selection).id).toBe(null);
  });

  test('turning the lens off keeps a real-note selection', () => {
    graph.set(HEALTH_GRAPH);
    healthLens.set(true);
    selectNode(1);
    healthLens.set(false);
    expect(get(selection).id).toBe(1);
  });

  test('selecting a note with a broken link lights its ghost while lens is on', () => {
    graph.set(HEALTH_GRAPH);
    healthLens.set(true);
    selectNode(1);
    expect(get(selection).neighborIds.has(ghostId('Missing'))).toBe(true);
    healthLens.set(false);
    selectNode(1);
    expect(get(selection).neighborIds.has(ghostId('Missing'))).toBe(false);
  });
});

describe('refreshGraph', () => {
  afterEach(() => {
    delete globalThis.window;
    loadError.set(null);
    graph.set({ notes: [], links: [], health: null, stats: null });
  });

  test('success populates graph and clears loadError', async () => {
    globalThis.window = {
      notesAPI: {
        fetchNotes: async () => ({
          notes: [{ id: 1, name: 'A' }], links: [], health: null,
          stats: { total_notes: 1, total_links: 0 },
        }),
      },
    };
    loadError.set('old');
    const ok = await refreshGraph();
    expect(ok).toBe(true);
    expect(get(graph).notes).toHaveLength(1);
    expect(get(loadError)).toBe(null);
  });

  test('error payload sets loadError and leaves graph alone', async () => {
    graph.set({ notes: [{ id: 9, name: 'Keep' }], links: [], health: null, stats: { total_notes: 1, total_links: 0 } });
    globalThis.window = { notesAPI: { fetchNotes: async () => ({ error: 'boom' }) } };
    const ok = await refreshGraph();
    expect(ok).toBe(false);
    expect(get(loadError)).toBe('Error: boom');
    expect(get(graph).notes[0].name).toBe('Keep');
  });

  test('a stale slow refresh cannot overwrite a newer one', async () => {
    let resolveOld;
    const oldPayload = new Promise((r) => { resolveOld = r; });
    const newPayload = Promise.resolve({
      notes: [{ id: 2, name: 'New' }], links: [], health: null,
      stats: { total_notes: 1, total_links: 0 },
    });
    let call = 0;
    globalThis.window = {
      notesAPI: { fetchNotes: () => (call++ === 0 ? oldPayload : newPayload) },
    };
    const first = refreshGraph();   // stale: resolves last
    const second = refreshGraph();  // newer: resolves first
    await second;
    resolveOld({
      notes: [{ id: 1, name: 'Old' }], links: [], health: null,
      stats: { total_notes: 1, total_links: 0 },
    });
    const ok = await first;
    expect(ok).toBe(false);
    expect(get(graph).notes[0].name).toBe('New');
  });
});
