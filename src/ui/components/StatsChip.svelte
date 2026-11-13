<script>
  import { visibleGraph } from '../stores.js';
  import { extendTestHook } from '../testhook.js';

  // Ghost nodes/links (health lens) are render artifacts, not notes.
  let real = $derived({
    notes: $visibleGraph.notes.filter((n) => !n.ghost),
    links: $visibleGraph.links.filter((l) => !l.ghost),
  });
  let stats = $derived({
    notes: real.notes.length,
    links: real.links.length,
    hubs: real.notes.filter((n) => n.connections >= 8).length,
    orphans: real.notes.filter((n) => n.cluster === -1).length,
  });
  extendTestHook({ stats: () => stats });
</script>

<div class="ng-pill chip" data-status>
  <span class="n">{stats.notes}</span> notes · <span class="n">{stats.links}</span> links · <span class="n">{stats.hubs}</span> hubs · <span class="n">{stats.orphans}</span> orphans
</div>

<style>
  .chip { display: block; line-height: 28px; }
  .n { color: var(--text-body); font-variant-numeric: tabular-nums; }
</style>
