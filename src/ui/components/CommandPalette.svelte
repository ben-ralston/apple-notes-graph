<script>
  import { graph, refreshGraph, selectNode, centerOnNode, clearSelection, paletteOpen, toggleHealthLens } from '../stores.js';
  import { fuzzySearch } from '../search/fuzzy.js';
  import { mergeSemantic } from '../search/semantic.js';
  import { refreshClusterNames } from '../clusterNaming.js';
  import { extendTestHook } from '../testhook.js';

  let query = $state('');
  let cursor = $state(0);
  let input = $state(null);

  const COMMANDS = [
    { name: '> Refresh graph', run: async () => { await refreshGraph(); refreshClusterNames(); } },
    { name: '> Clear selection', run: clearSelection },
    { name: '> Toggle health lens', run: toggleHealthLens },
  ];

  function computeResults(q) {
    return q.startsWith('>')
      ? COMMANDS.filter((c) => c.name.toLowerCase().includes(q.slice(1).trim().toLowerCase()))
          .map((c) => ({ item: c, positions: [] }))
      : fuzzySearch(q, $graph.notes, (n) => n.name);
  }

  let results = $derived(computeResults(query));

  // Debounced async semantic lookup; last-wins; silent on failure (palette
  // stays fuzzy-only per the degradation rule).
  let semanticHits = $state([]);
  let semanticPending = $state(false);
  let semanticSeq = 0;
  let debounceTimer = null;

  $effect(() => {
    const q = query;
    const open = $paletteOpen;
    clearTimeout(debounceTimer);
    if (!open || q.startsWith('>') || q.trim().length < 3) {
      semanticSeq++; // invalidate any in-flight lookup for the abandoned query
      semanticHits = [];
      semanticPending = false;
      return;
    }
    const seq = ++semanticSeq;
    semanticPending = true;
    debounceTimer = setTimeout(async () => {
      let res = null;
      try {
        res = await window.notesAPI.semanticSearch(q);
      } catch { /* degradation rule: stays fuzzy-only */ }
      if (seq !== semanticSeq) return;
      semanticHits = res?.results ?? [];
      semanticPending = false;
    }, 250);
  });

  let combined = $derived([...results, ...mergeSemantic(results, semanticHits, $graph.notes)]);

  function onKeydown(e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      const next = !$paletteOpen;
      paletteOpen.set(next);
      query = ''; cursor = 0;
      if (next) queueMicrotask(() => input?.focus());
      return;
    }
    if (!$paletteOpen) return;
    if (e.key === 'Escape') { paletteOpen.set(false); }
    if (e.key === 'ArrowDown') { e.preventDefault(); cursor = Math.min(cursor + 1, combined.length - 1); }
    if (e.key === 'ArrowUp') { e.preventDefault(); cursor = Math.max(cursor - 1, 0); }
    if (e.key === 'Enter' && combined[cursor]) pick(combined[cursor].item);
  }

  function pick(item) {
    paletteOpen.set(false);
    if (item.run) item.run();
    else {
      // Jump: select AND fly the viewport there — the note may be off-screen.
      selectNode(item.id);
      centerOnNode(item.id);
    }
  }

  export function show() {
    paletteOpen.set(true); query = ''; cursor = 0;
    queueMicrotask(() => input?.focus());
  }

  extendTestHook({
    palette: (q) => computeResults(q).map((r) => r.item.name),
    openPalette: () => show(),
    semanticResults: () => semanticHits.map((s) => s.name),
  });
</script>

<svelte:window onkeydown={onKeydown} />

{#if $paletteOpen}
  <div class="backdrop" role="presentation"
       onclick={(e) => { if (e.target === e.currentTarget) paletteOpen.set(false); }}>
    <div class="ng-panel palette" role="dialog" aria-label="Command palette" tabindex="-1">
      <input bind:this={input} bind:value={query} placeholder="Jump to note, or > for commands…"
             oninput={() => (cursor = 0)} data-palette-input
             role="combobox" aria-expanded="true" aria-controls="palette-listbox"
             aria-activedescendant={combined[cursor] ? `palette-option-${cursor}` : undefined} />
      <ul id="palette-listbox" role="listbox">
        {#each combined as r, i}
          {#if r.semantic && (i === 0 || !combined[i - 1].semantic)}
            <li class="group-header" aria-hidden="true">Semantic</li>
          {/if}
          <li id={`palette-option-${i}`} role="option" aria-selected={i === cursor} class:active={i === cursor}
              onmousedown={() => pick(r.item)}>
            <span class="name">{r.item.name}</span>
            {#if r.item.folder}<span class="folder">{r.item.folder}</span>{/if}
          </li>
        {/each}
        {#if combined.length === 0 && !semanticPending}<li class="empty">No matches</li>{/if}
        {#if semanticPending}<li class="searching" aria-hidden="true"><span class="ng-spinner"></span>Searching by meaning…</li>{/if}
      </ul>
    </div>
  </div>
{/if}

<style>
  .backdrop { position: fixed; inset: 0; background: rgba(4, 6, 14, 0.55); z-index: 20; }
  .palette {
    width: min(480px, calc(100vw - 48px)); margin: 12vh auto 0;
    overflow: hidden; animation: pop 180ms ease;
  }
  @keyframes pop { from { transform: scale(0.97) translateY(-6px); opacity: 0; } }
  input {
    width: 100%; box-sizing: border-box; background: none; border: none; outline: none;
    color: var(--text-strong); font-size: var(--text-lg); padding: var(--space-4);
    border-bottom: 1px solid var(--border); font-family: inherit;
  }
  input::placeholder { color: var(--text-muted); }
  ul { list-style: none; margin: 0; padding: 6px; max-height: 320px; overflow-y: auto; }
  li {
    padding: var(--space-2) var(--space-3); border-radius: var(--radius-md);
    color: var(--text-body); font-size: var(--text-md); cursor: pointer;
    display: flex; justify-content: space-between; align-items: baseline; gap: var(--space-3);
  }
  li:hover { background: var(--surface-raised); }
  li.active { background: var(--accent-soft); box-shadow: inset 2px 0 0 var(--accent); color: var(--text-strong); }
  .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  li.empty, li.searching { color: var(--text-muted); cursor: default; }
  li.empty:hover, li.searching:hover { background: none; }
  li.searching { justify-content: flex-start; align-items: center; gap: var(--space-2); font-size: var(--text-sm); }
  .group-header {
    color: var(--text-muted); font-size: var(--text-xs); font-weight: 600; letter-spacing: 0.6px;
    text-transform: uppercase; padding: var(--space-2) var(--space-3) 2px; cursor: default;
  }
  .group-header:hover { background: none; }
  .folder { color: var(--text-muted); font-size: var(--text-xs); flex-shrink: 0; }
</style>
