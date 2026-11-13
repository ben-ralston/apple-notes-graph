<script>
  import { graph, refreshGraph, selectNode, centerOnNode } from '../stores.js';
  import ArrowRight from '@lucide/svelte/icons/arrow-right';
  import Plus from '@lucide/svelte/icons/plus';
  import Sparkles from '@lucide/svelte/icons/sparkles';
  import Icon from './Icon.svelte';
  import { extendTestHook } from '../testhook.js';
  import { mergeRanking, reasonChips } from '../suggest/suggest.js';
  import { showToast, updateToast } from '../toast.js';

  let { note } = $props();

  const MAX_SHOWN = 8;

  let state = $state('idle');       // idle | recalling | ready | error
  let rankState = $state('idle');   // idle | ranking | ranked | failed
  let recall = $state(null);        // suggest_links.py payload
  let cards = $state([]);
  let errorMsg = $state('');
  let requestId = 0;

  // Note switch invalidates any in-flight request and resets the panel.
  $effect(() => {
    note?.id;
    requestId++;
    state = 'idle'; rankState = 'idle'; recall = null; cards = []; errorMsg = '';
    confirming = null; adding = null; addError = null;
  });

  async function suggest() {
    const rid = ++requestId;
    state = 'recalling';
    try {
      const res = await window.notesAPI.suggestLinks(note.id);
      if (rid !== requestId) return;
      if (res?.error) { state = 'error'; errorMsg = res.error; return; }
      recall = res;
      cards = mergeRanking(res.candidates ?? [], undefined).slice(0, MAX_SHOWN);
      state = 'ready';
      rank(rid); // fire-and-forget: recall cards are already on screen
    } catch (e) {
      if (rid === requestId) { state = 'error'; errorMsg = String(e); }
    }
  }

  async function rank(rid) {
    rankState = 'ranking';
    try {
      // $state.snapshot: recall is a $state proxy, and proxied objects fail
      // Electron's structured clone across the contextBridge (the invoke
      // rejects and the panel would hang in "ranking" — found live, #117).
      const plain = $state.snapshot(recall);
      const res = await window.notesAPI.rankSuggestions({
        noteName: plain.note.name,
        noteFolder: plain.note.folder,
        noteContent: plain.note.content,
        candidates: plain.candidates ?? [],
      });
      if (rid !== requestId) return;
      if (res?.error || !res?.picks) { rankState = 'failed'; return; }
      cards = mergeRanking(plain.candidates ?? [], res.picks).slice(0, MAX_SHOWN);
      rankState = 'ranked';
    } catch {
      if (rid === requestId) rankState = 'failed';
    }
  }

  function jump(name) {
    const target = $graph.notes.find((n) => n.name === name);
    if (target) { selectNode(target.id); centerOnNode(target.id); }
  }

  // Results arrive below the lists, so bring them into view when the state changes.
  let sectionEl = $state(null);
  $effect(() => {
    if (state !== 'idle') sectionEl?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });

  let confirming = $state(null);  // candidate name awaiting confirm
  let adding = $state(null);      // candidate name being written
  let addError = $state(null);    // {name, message}

  async function addLink(name) {
    confirming = null;
    adding = name;
    addError = null;
    const toastId = showToast(`Adding link → ${name}…`, { spinner: true });
    try {
      const res = await window.notesAPI.addLink({ sourcePk: note.id, targetName: name });
      adding = null;
      if (res?.error) {
        updateToast(toastId, 'Add link failed', { ttlMs: 3500 });
        addError = { name, message: res.error };
        return;
      }
      updateToast(toastId, `Link added → ${name}`, { ttlMs: 3500 });
      // The link now exists — drop the card so it can't be added twice.
      cards = cards.filter((c) => c.name !== name);
      await refreshGraph();
    } catch (e) {
      adding = null;
      updateToast(toastId, 'Add link failed', { ttlMs: 3500 });
      addError = { name, message: String(e) };
    }
  }

  extendTestHook({
    suggestLinks: () => suggest(),
    addSuggestedLink: (name) => { confirming = name; return addLink(name); },
    suggestions: () => ({
      state,
      rankState,
      cards: cards.map((c) => ({
        name: c.name, ranked: c.ranked ?? false, reason: c.reason ?? null,
        chips: reasonChips(c.reasons),
      })),
    }),
  });
</script>

<section class="suggestions" bind:this={sectionEl}>
  {#if state === 'idle'}
    <button class="ng-button" onclick={suggest}><Icon icon={Sparkles} size="sm" />Suggest links</button>
  {:else if state === 'recalling'}
    <p class="muted">Scanning notes for candidates… (can take ~30s)</p>
  {:else if state === 'error'}
    <p class="muted">Suggestions unavailable: {errorMsg}</p>
  {:else}
    <h3 class="ng-heading">Suggested links ({cards.length})</h3>
    {#if rankState === 'ranking'}<p class="muted rank-note">Asking Claude to rank…</p>
    {:else if rankState === 'failed'}<p class="muted rank-note">Showing recall order (ranking unavailable)</p>{/if}
    <!-- unkeyed: duplicate note titles exist in real data, so c.name is not a safe key -->
    {#each cards as c}
      <div class="card" data-suggestion>
        <button class="ng-walk" onclick={() => jump(c.name)}><Icon icon={ArrowRight} size="sm" /><span>{c.name}</span></button>
        <div class="chips">
          {#each reasonChips(c.reasons) as chip}<span class="chip">{chip}</span>{/each}
        </div>
        {#if c.ranked}<p class="why">{c.reason}</p>{/if}
        {#if addError?.name === c.name}
          <p class="add-error">Failed: {addError.message}</p>
        {/if}
        {#if confirming === c.name}
          <div class="confirm">
            <span>Notes.app will briefly take focus — don't type.</span>
            <button class="ng-mini primary" onclick={() => addLink(c.name)}>Add</button>
            <button class="ng-mini" onclick={() => (confirming = null)}>Cancel</button>
          </div>
        {:else}
          <button class="ng-mini add" disabled={adding !== null}
                  onclick={() => (confirming = c.name)}>
            {#if adding === c.name}Adding…{:else}<Icon icon={Plus} size="sm" />Add link{/if}
          </button>
        {/if}
      </div>
    {/each}
    {#if cards.length === 0}<p class="muted">No candidates found.</p>{/if}
  {/if}
</section>

<style>
  .suggestions { padding-block: var(--space-1); }
  h3 { padding-block: var(--space-1); }
  .muted { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-1) 0; }
  .rank-note { font-size: var(--text-xs); margin: 2px 0 6px; }
  .why { color: var(--text-body); font-size: var(--text-xs); margin: var(--space-1) 0 0; font-style: italic; }
  .card { padding: 6px 0; border-bottom: 1px solid var(--border); }
  .card:last-of-type { border-bottom: none; }
  .chips { display: flex; flex-wrap: wrap; gap: var(--space-1); margin-top: 3px; }
  .chip {
    font-size: var(--text-xs); color: var(--text-muted); background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border-control); border-radius: var(--radius-pill); padding: 1px 7px;
  }
  .add { margin-top: var(--space-1); }
  .ng-mini.primary { border-color: rgba(167, 139, 250, 0.6); color: #c4b5fd; }
  .confirm {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    font-size: var(--text-xs); color: var(--warn); margin-top: var(--space-1);
  }
  .add-error { color: var(--danger); font-size: var(--text-xs); margin: var(--space-1) 0 0; }
</style>
