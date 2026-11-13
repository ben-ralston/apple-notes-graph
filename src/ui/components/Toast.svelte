<script>
  import { toasts } from '../toast.js';
  import { extendTestHook } from '../testhook.js';

  extendTestHook({ toasts: () => $toasts.map((t) => t.text) });
</script>

{#if $toasts.length > 0}
  <div class="stack">
    {#each $toasts as t (t.id)}
      <div class="ng-pill toast" data-toast>
        {#if t.spinner}<span class="ng-spinner"></span>{/if}
        {t.text}
      </div>
    {/each}
  </div>
{/if}

<style>
  .stack {
    position: fixed; bottom: var(--space-4); right: var(--space-4); z-index: 30;
    display: flex; flex-direction: column; gap: var(--space-2); align-items: flex-end;
  }
  .toast { color: var(--text-strong); font-size: var(--text-sm); animation: toast-in 200ms ease; }
  @keyframes toast-in { from { transform: translateY(6px); opacity: 0; } }
</style>
