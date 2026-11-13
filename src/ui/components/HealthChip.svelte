<script>
  import { healthLens, healthOverlay, toggleHealthLens, visibleGraph } from '../stores.js';
  import { visibleHealthCounts } from '../graph/health.js';
  import { extendTestHook } from '../testhook.js';
  import CircleDot from '@lucide/svelte/icons/circle-dot';
  import Clock from '@lucide/svelte/icons/clock';
  import Unlink from '@lucide/svelte/icons/unlink';
  import Icon from './Icon.svelte';

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  let summary = $derived(`${plural(counts.orphans, 'orphan')}, ${plural(counts.broken, 'broken link')}, ${plural(counts.stale, 'stale note')}`);

  let counts = $derived(visibleHealthCounts(
    $visibleGraph.notes.filter((n) => !n.ghost),
    $healthOverlay
  ));

  extendTestHook({ healthChip: () => counts });
</script>

<button
  class="ng-pill chip"
  class:active={$healthLens}
  data-health-chip
  aria-pressed={$healthLens}
  aria-label="Health lens: {summary}"
  title="{summary}. Click to {$healthLens ? 'hide' : 'show'} them on the graph."
  onclick={toggleHealthLens}
>
  <span class="stat orphan"><Icon icon={CircleDot} size="sm" /><span class="n">{counts.orphans}</span></span>
  <span class="stat broken"><Icon icon={Unlink} size="sm" /><span class="n">{counts.broken}</span></span>
  <span class="stat stale"><Icon icon={Clock} size="sm" /><span class="n">{counts.stale}</span></span>
</button>

<style>
  .chip.active { border-color: rgba(251, 191, 36, 0.55); color: var(--text-body); }
  .stat { display: inline-flex; align-items: center; gap: var(--space-1); }
  .n { color: var(--text-body); font-variant-numeric: tabular-nums; }
  .chip.active .orphan { color: var(--warn); }
  .chip.active .broken { color: var(--danger); }
</style>
