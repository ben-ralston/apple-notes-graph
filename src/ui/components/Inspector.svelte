<script>
  import { graph, visibleGraph, selection, selectNode, centerOnNode, clearSelection, refreshGraph, healthLens, healthOverlay } from '../stores.js';
  import { extendTestHook } from '../testhook.js';
  import { formatModified } from '../time.js';
  import { showToast, updateToast } from '../toast.js';
  import { openInNotes } from '../openNote.js';
  import ArrowLeft from '@lucide/svelte/icons/arrow-left';
  import ArrowRight from '@lucide/svelte/icons/arrow-right';
  import ChevronRight from '@lucide/svelte/icons/chevron-right';
  import CircleAlert from '@lucide/svelte/icons/circle-alert';
  import ExternalLink from '@lucide/svelte/icons/external-link';
  import Trash from '@lucide/svelte/icons/trash-2';
  import X from '@lucide/svelte/icons/x';
  import Icon from './Icon.svelte';
  import LinkSuggestions from './LinkSuggestions.svelte';
  import NoteBody from './NoteBody.svelte';

  // Walking a link/backlink selects a note that may be off-screen — fly the
  // viewport there too, same as a ⌘K jump.
  function walk(id) {
    selectNode(id);
    centerOnNode(id);
  }

  // Lists longer than this start collapsed so the preview keeps the panel's height.
  const OPEN_LIST_MAX = 5;

  let preview = $state(null);   // {content} | {error} | null while loading
  let previewBlocks = $state(0);
  let note = $derived(
    $graph.notes.find((n) => n.id === $selection.id)
      ?? ($healthLens ? $healthOverlay.ghostNodes.find((g) => g.id === $selection.id) : null)
      ?? null
  );

  // Ghost = broken-link target: the notes whose links point at it.
  let ghostSources = $derived(!note?.ghost ? [] : note.sources
    .map((pk) => $graph.notes.find((n) => n.id === pk))
    .filter(Boolean));

  let modifiedLabel = $derived(note && !note.ghost ? formatModified(note.modified) : null);

  let outgoing = $derived(!note ? [] : $graph.links
    .filter((l) => (l.source.id ?? l.source) === note.id)
    .map((l) => $graph.notes.find((n) => n.id === (l.target.id ?? l.target)))
    .filter(Boolean));
  let incoming = $derived(!note ? [] : $graph.links
    .filter((l) => (l.target.id ?? l.target) === note.id)
    .map((l) => $graph.notes.find((n) => n.id === (l.source.id ?? l.source)))
    .filter(Boolean));

  // A link to a note hidden by filters or focus can't be walked to on the canvas.
  let visibleIds = $derived(new Set($visibleGraph.notes.map((n) => n.id)));

  let previewRequestId = 0;

  $effect(() => {
    preview = null;
    if (note && !note.ghost) {
      const requestId = ++previewRequestId;
      window.notesAPI.readNote(note.id).then((r) => {
        if (requestId === previewRequestId) preview = r;
      });
    }
  });

  let deleteState = $state('idle');  // idle | confirming | deleting
  let deleteError = $state(null);

  // Switching notes abandons any confirm/error state.
  $effect(() => {
    note?.id;
    deleteState = 'idle';
    deleteError = null;
  });

  async function doDelete() {
    const pk = note.id;
    const name = note.name;
    deleteState = 'deleting';
    deleteError = null;
    const toastId = showToast(`Deleting “${name}”…`, { spinner: true });
    try {
      const res = await window.notesAPI.deleteNote(pk);
      if (note?.id !== pk) return; // user moved on mid-delete
      if (res?.error) {
        deleteState = 'idle';
        updateToast(toastId, 'Delete failed', { ttlMs: 3500 });
        deleteError = res.error;
        return;
      }
      updateToast(toastId, `Deleted “${name}” → Recently Deleted`, { ttlMs: 3500 });
      clearSelection();
      await refreshGraph();
    } catch (e) {
      updateToast(toastId, 'Delete failed', { ttlMs: 3500 });
      if (note?.id === pk) {
        deleteState = 'idle';
        deleteError = String(e);
      }
    }
  }

  extendTestHook({
    inspector: () => note ? {
      name: note.name,
      ghost: note.ghost ?? false,
      links: outgoing.map((n) => n.name),
      backlinks: incoming.map((n) => n.name),
      sources: ghostSources.map((n) => n.name),
      deleteState,
      deleteError,
      modifiedLabel,
      previewBlocks,
    } : null,
    deleteNote: () => {
      if (!note || note.ghost) return false;
      deleteState = 'confirming';
      return doDelete();
    },
  });
</script>

{#snippet noteList(label, notes, glyph)}
  {#if notes.length > 0}
    <details class="group" open={notes.length <= OPEN_LIST_MAX}>
      <summary class="ng-heading"><span class="chevron"><Icon icon={ChevronRight} size="sm" /></span>{label} ({notes.length})</summary>
      {#each notes as n}
        <button class="ng-walk" onclick={() => walk(n.id)}><Icon icon={glyph} size="sm" /><span>{n.name}</span></button>
      {/each}
    </details>
  {/if}
{/snippet}

{#if note}
  <aside data-inspector class="ng-panel">
    <header>
      <div class="title-row">
        <h2>{note.name}</h2>
        <button class="close" aria-label="Close" onclick={clearSelection}><Icon icon={X} /></button>
      </div>
      {#if note.ghost}
        <p class="meta ghost-flag"><Icon icon={CircleAlert} size="sm" />Broken link: this note doesn't exist</p>
      {:else}
        <p class="meta">{note.folder} · {note.connections} connection{note.connections === 1 ? '' : 's'}{modifiedLabel ? ` · Modified ${modifiedLabel}` : ''}</p>
      {/if}
    </header>

    {#if note.ghost}
      <div class="relations full">
        <section class="group">
          <h3 class="ng-heading">Linked from ({ghostSources.length})</h3>
          {#each ghostSources as n}<button class="ng-walk" onclick={() => walk(n.id)}><Icon icon={ArrowLeft} size="sm" /><span>{n.name}</span></button>{/each}
          <p class="hint">Fix the link inside the source note, or create the missing note.</p>
        </section>
      </div>

      <footer>
        <button class="ng-button" disabled title="Coming soon: will create the note via the MCP write path">
          Create “{note.name}” in Notes
        </button>
      </footer>
    {:else}
      {#key note.id}
        <section class="preview" data-preview>
          {#if preview === null}<p class="muted">Loading preview…</p>
          {:else if preview.error}<p class="muted">Preview unavailable: {preview.error}</p>
          {:else}<NoteBody content={preview.content} title={note.name} {outgoing} {visibleIds} onWalk={walk} bind:blockCount={previewBlocks} />{/if}
        </section>

        <div class="relations">
          {@render noteList('Links', outgoing, ArrowRight)}
          {@render noteList('Backlinks', incoming, ArrowLeft)}
          <LinkSuggestions {note} />
        </div>
      {/key}

      <footer>
        {#if deleteError}
          <p class="delete-error">Failed: {deleteError}</p>
        {/if}
        {#if deleteState === 'confirming'}
          <div class="confirm">
            <span>Move to Recently Deleted?</span>
            <button class="ng-mini danger" onclick={doDelete}>Yes</button>
            <button class="ng-mini" onclick={() => (deleteState = 'idle')}>Cancel</button>
          </div>
        {:else}
          <div class="actions">
            <button class="ng-button" onclick={() => openInNotes(note.name)}>Open in Notes<Icon icon={ExternalLink} size="sm" /></button>
            <button class="delete" disabled={deleteState === 'deleting'}
                    onclick={() => { deleteError = null; deleteState = 'confirming'; }}>
              <Icon icon={Trash} size="sm" />{deleteState === 'deleting' ? 'Deleting…' : 'Delete note…'}
            </button>
          </div>
        {/if}
      </footer>
    {/if}
  </aside>
{/if}

<style>
  aside {
    position: fixed; top: 64px; right: var(--space-4); bottom: 56px;
    /* Keep in sync with inspectorWidth() in layout.js. */
    width: min(420px, calc(100vw - 96px)); box-sizing: border-box;
    display: flex; flex-direction: column; overflow: hidden;
    color: var(--text-strong); font-size: var(--text-md);
    animation: slide-in var(--motion-base) ease;
  }
  @keyframes slide-in { from { transform: translateX(24px); opacity: 0; } }

  header { flex: none; padding: var(--space-4) var(--space-4) var(--space-3); border-bottom: 1px solid var(--border); }
  .title-row { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-2); }
  h2 { font-size: var(--text-lg); font-weight: 600; letter-spacing: -0.1px; margin: 0; line-height: 1.3; overflow-wrap: anywhere; }
  .close {
    flex: none; display: inline-flex; padding: 2px; margin: -1px -2px 0 0;
    background: none; border: none; border-radius: var(--radius-sm); color: var(--text-muted); cursor: pointer;
  }
  .close:hover { color: var(--text-strong); }
  .meta { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-1) 0 0; }
  .ghost-flag { display: flex; align-items: center; gap: 6px; color: var(--danger); }
  .muted { color: var(--text-muted); margin: 0; }
  .hint { color: var(--text-muted); font-size: var(--text-xs); margin: var(--space-3) 0 0; }

  /* The note gets the height; lists and suggestions scroll in what's left. */
  .preview { flex: 1 1 0; min-height: 45%; overflow-y: auto; padding: var(--space-3) var(--space-4); }
  .relations {
    flex: 0 1 auto; max-height: 40%; overflow-y: auto;
    padding: var(--space-1) var(--space-4) var(--space-2);
    border-top: 1px solid var(--border);
  }
  .relations.full { flex: 1 1 0; max-height: none; border-top: none; padding-top: var(--space-3); }

  .group { padding-block: var(--space-1); }
  summary {
    display: flex; align-items: center; gap: var(--space-1); padding-block: var(--space-1);
    cursor: pointer; list-style: none; border-radius: var(--radius-sm);
  }
  summary::-webkit-details-marker { display: none; }
  summary:hover { color: var(--text-strong); }
  .chevron { display: inline-flex; transition: transform var(--motion-fast) ease; }
  details[open] > summary .chevron { transform: rotate(90deg); }

  footer {
    flex: none; display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    background: var(--surface-solid); border-top: 1px solid var(--border);
  }
  .actions { display: flex; align-items: center; gap: var(--space-3); }
  .delete {
    flex: none; display: inline-flex; align-items: center; gap: var(--space-1); padding: 6px var(--space-1);
    background: none; border: none; border-radius: var(--radius-sm); cursor: pointer;
    color: var(--text-muted); font-family: inherit; font-size: var(--text-sm);
    transition: color var(--motion-fast) ease;
  }
  .delete:hover:not(:disabled) { color: var(--danger); }
  .delete:disabled { opacity: 0.55; cursor: default; }
  .confirm { display: flex; align-items: center; gap: var(--space-2); color: var(--warn); font-size: var(--text-sm); min-height: 32px; }
  .confirm span { flex: 1; }
  .ng-mini.danger { border-color: var(--danger-border); color: var(--danger); }
  .ng-mini.danger:hover { color: var(--danger); background: var(--danger-soft); border-color: var(--danger); }
  .delete-error { color: var(--danger); font-size: var(--text-xs); margin: 0; }
</style>
