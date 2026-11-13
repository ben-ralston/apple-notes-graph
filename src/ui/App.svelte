<script>
  import { onMount } from 'svelte';
  import { extendTestHook } from './testhook.js';
  import GraphCanvas from './graph/GraphCanvas.svelte';
  import Inspector from './components/Inspector.svelte';
  import CommandPalette from './components/CommandPalette.svelte';
  import FilterPopover from './components/FilterPopover.svelte';
  import Toast from './components/Toast.svelte';
  import StatsChip from './components/StatsChip.svelte';
  import HealthChip from './components/HealthChip.svelte';
  import UpdateChip from './components/UpdateChip.svelte';
  import CircleAlert from '@lucide/svelte/icons/circle-alert';
  import RotateCw from '@lucide/svelte/icons/rotate-cw';
  import Icon from './components/Icon.svelte';
  import { get } from 'svelte/store';
  import { graph, visibleGraph, selection, focus, paletteOpen, filterPopoverOpen, loadError, healthLens, clearSelection, enterFocus, exitFocus, refreshGraph } from './stores.js';
  import { refreshClusterNames } from './clusterNaming.js';
  import { isEditableTarget } from './keyboard.js';

  let status = $state('Fetching notes…');
  let loading = $state(true);
  let retrying = $state(false);
  let paletteRef;
  let filterRef;

  let focusedNote = $derived($graph.notes.find((n) => n.id === $focus.rootId) ?? null);

  function onKeydown(e) {
    if (e.key === 'f' || e.key === 'F') {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if ($paletteOpen || isEditableTarget(e.target)) return;
      if ($selection.id !== null) enterFocus($selection.id);
      return;
    }
    if (e.key === 'Escape') {
      if ($paletteOpen) return;
      if ($filterPopoverOpen) { filterPopoverOpen.set(false); return; }
      if ($focus.rootId !== null) exitFocus();
      else clearSelection();
    }
  }

  $effect(() => {
    if ($paletteOpen) filterPopoverOpen.set(false);
  });

  async function retry() {
    retrying = true;
    try {
      if (await refreshGraph()) refreshClusterNames();
    } catch (error) {
      loadError.set(`Error: ${error?.message ?? String(error)}`);
    }
    retrying = false;
  }

  onMount(async () => {
    // Filters first: the first layout and launch fit must see the filtered graph,
    // not every note. A bad config falls back to no filters.
    try {
      await filterRef.init();
    } catch (error) {
      console.warn('Could not load filters:', error);
    }
    try {
      const data = await window.notesAPI.fetchNotes();
      if (data.error || !data.stats) {
        loadError.set(`Error: ${data.error ?? 'no data returned'}`);
        status = $loadError;
      } else {
        graph.set({ notes: data.notes ?? [], links: data.links ?? [], health: data.health, stats: data.stats });
        loadError.set(null);
        status = `Loaded ${data.stats.total_notes} notes, ${data.stats.total_links} links`;
        refreshClusterNames();
      }
    } catch (error) {
      loadError.set(`Error: ${error?.message ?? String(error)}`);
      status = $loadError;
    }
    loading = false;
    extendTestHook({
      ready: true,
      status: () => document.querySelector('[data-status]')?.textContent ?? status,
      focus: () => ($focus.rootId !== null ? { rootId: $focus.rootId } : null),
      lens: () => (get(healthLens) ? 'health' : null),
      setLens: (on) => healthLens.set(Boolean(on)),
    });
  });
</script>

<svelte:window onkeydown={onKeydown} />

<main>
  <GraphCanvas />
  <Inspector />
  <CommandPalette bind:this={paletteRef} />
  <FilterPopover bind:this={filterRef} />
  <Toast />
  <button class="ng-pill palette-hint" onclick={() => paletteRef.show()}><kbd>⌘K</kbd>Jump or command…</button>
  {#if focusedNote}
    <div class="ng-pill focus-chip" data-focus-chip>
      <span class="focus-name">Focus: {focusedNote.name}</span>
      <button class="focus-exit" onclick={exitFocus}>Esc to exit</button>
    </div>
  {/if}
  {#if !loading && !$loadError && $visibleGraph.notes.length === 0}
    <div class="empty-state" data-empty-state>
      {#if $graph.notes.length === 0}
        <p class="empty-title">No notes found</p>
        <p>Notes appear here once Apple Notes has some to read.</p>
      {:else}
        <p class="empty-title">No notes to show</p>
        <p>Your filters hide all {$graph.notes.length} notes.</p>
        <button class="ng-mini" onclick={() => filterPopoverOpen.set(true)}>Change filters</button>
      {/if}
    </div>
  {/if}
  <div class="chips">
    {#if loading}
      <div class="ng-pill" data-loading><span class="ng-spinner"></span>Reading your notes…</div>
    {:else if $loadError}
      <div class="ng-pill error" data-status><Icon icon={CircleAlert} size="sm" />{$loadError}</div>
      <button class="ng-pill retry" disabled={retrying} onclick={retry}><Icon icon={RotateCw} size="sm" />{retrying ? 'Retrying…' : 'Retry'}</button>
    {:else}
      <StatsChip />
      <HealthChip />
      <UpdateChip />
    {/if}
  </div>
</main>

<style>
  :global(body) {
    margin: 0;
    background: var(--bg);
    color: var(--text-strong);
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    overflow: hidden;
    -webkit-font-smoothing: antialiased;
  }
  :global(::-webkit-scrollbar) { width: 8px; height: 8px; }
  :global(::-webkit-scrollbar-thumb) { background: var(--border); border-radius: 4px; }
  :global(::-webkit-scrollbar-track) { background: transparent; }
  main { height: 100vh; }
  .palette-hint {
    position: fixed; top: var(--space-4); left: 50%; transform: translateX(-50%);
    font-size: var(--text-sm); padding-left: var(--space-2);
  }
  .palette-hint kbd {
    font-family: inherit; font-size: var(--text-xs); color: var(--text-muted);
    background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-control);
    border-radius: var(--radius-pill); padding: 1px var(--space-2);
  }
  .focus-chip {
    position: fixed; top: 54px; left: 50%; transform: translateX(-50%);
    max-width: 50vw; border-color: rgba(167, 139, 250, 0.45);
    color: var(--text-strong); font-size: var(--text-sm);
    animation: chip-in 200ms ease;
  }
  @keyframes chip-in { from { transform: translate(-50%, -6px); opacity: 0; } }
  .focus-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .focus-exit {
    flex-shrink: 0; background: none; border: none; padding: 0; cursor: pointer;
    color: var(--text-muted); font-family: inherit; font-size: var(--text-sm);
  }
  .focus-exit:hover { color: var(--text-strong); }
  .chips {
    position: fixed; bottom: var(--space-4); left: var(--space-4);
    display: flex; gap: var(--space-2); align-items: center;
  }
  .error { color: var(--danger); border-color: var(--danger-border); }
  .retry { color: var(--text-strong); }
  .retry:disabled { opacity: 0.55; cursor: default; }
  .empty-state {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
    display: flex; flex-direction: column; align-items: center; gap: var(--space-2);
    color: var(--text-muted); font-size: var(--text-md); text-align: center;
  }
  .empty-state p { margin: 0; }
  .empty-title { color: var(--text-strong); font-size: var(--text-lg); font-weight: 600; }
</style>
