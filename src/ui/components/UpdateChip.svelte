<script>
  import { onMount } from 'svelte';
  import { extendTestHook } from '../testhook.js';
  import { watchUpdateStatus } from '../updateStatus.js';

  // Shown only by the installed app, when the checkout's UI has moved on since install.
  let stale = $state(false);

  onMount(() => watchUpdateStatus({
    api: window.notesAPI,
    target: window,
    onChange: (value) => { stale = value; },
  }));

  extendTestHook({ uiUpdate: () => ({ stale }) });
</script>

{#if stale}
  <div class="ng-pill" data-update-chip>UI update ready: run <code>./run ui-app-install</code></div>
{/if}

<style>
  code { font-family: inherit; color: var(--warn); }
</style>
