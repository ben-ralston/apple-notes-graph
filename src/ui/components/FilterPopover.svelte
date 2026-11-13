<script>
  import { filters, filterPopoverOpen, graph } from '../stores.js';
  import { extendTestHook } from '../testhook.js';
  import {
    addExcludedFolder, addExcludedFolders, cleanExcludedFolders, folderChoices, removeExcludedFolder,
  } from '../folderFilter.js';
  import Settings from '@lucide/svelte/icons/settings';
  import X from '@lucide/svelte/icons/x';
  import Icon from './Icon.svelte';

  // Persisted exactly as before: config.json's `exclude_folders` name list (#146).
  let excludeFolders = $state([]);
  let patternText = $state('');
  let otherText = $state('');
  let popoverEl = $state(null);
  let toggleEl = $state(null);

  // Folders from the graph already loaded (no extra fetch), minus those already hidden.
  const choices = $derived(folderChoices($graph.notes, excludeFolders));

  export async function init() {
    const config = await window.notesAPI.loadConfig();
    excludeFolders = cleanExcludedFolders(config.exclude_folders);
    patternText = localStorage.getItem('notesGraph_excludePattern') ?? '';
    apply(false);
  }

  // Saves run one at a time, in order: each is a read-modify-write of the
  // config.json the MCP server shares, so quick chip clicks must not interleave.
  let saving = Promise.resolve();

  function apply(persist = true) {
    const folders = [...excludeFolders];
    const pattern = patternText;
    filters.set({ excludeFolders: folders, excludePattern: pattern });
    if (!persist) return saving;
    saving = saving
      .then(() => save(folders, pattern))
      .catch((error) => console.error('Failed to save filters:', error));
    return saving;
  }

  async function save(folders, pattern) {
    const config = await window.notesAPI.loadConfig();
    config.exclude_folders = folders;
    await window.notesAPI.saveConfig(config);
    localStorage.setItem('notesGraph_excludePattern', pattern);
  }

  function setFolders(next) {
    if (next === excludeFolders) return;
    excludeFolders = next;
    apply();
  }

  // A dropdown pick is one real folder name, commas and all.
  function pickFolder(e) {
    const name = e.currentTarget.value;
    e.currentTarget.value = '';
    setFolders(addExcludedFolder(excludeFolders, name));
  }

  // Free text commits on Return only, so a half-typed name never saves on blur.
  function otherKeydown(e) {
    if (e.key !== 'Enter' || e.isComposing) return;
    const text = otherText;
    otherText = '';
    setFolders(addExcludedFolders(excludeFolders, text));
  }

  function removeFolder(name) {
    setFolders(removeExcludedFolder(excludeFolders, name));
  }

  extendTestHook({
    setFilters: ({ excludeFolders: folders = [], excludePattern = '' }) => {
      excludeFolders = cleanExcludedFolders(folders);
      patternText = excludePattern;
      apply(false);
    },
    // {excluded, choices}: the chip list and the dropdown's options.
    folderFilter: () => ({ excluded: [...excludeFolders], choices: [...choices] }),
  });

  function handleOutsideClick(e) {
    if (!$filterPopoverOpen) return;
    const target = e.target;
    if (popoverEl?.contains(target) || toggleEl?.contains(target)) return;
    filterPopoverOpen.set(false);
  }
</script>

<svelte:window onmousedown={handleOutsideClick} />

<button class="ng-pill chip" bind:this={toggleEl} aria-expanded={$filterPopoverOpen} aria-controls="filter-popover"
        onclick={() => filterPopoverOpen.set(!$filterPopoverOpen)}>
  <Icon icon={Settings} size="sm" />
  Filter
</button>
{#if $filterPopoverOpen}
  <div class="ng-panel popover" id="filter-popover" role="dialog" aria-label="Graph filters" tabindex="-1" bind:this={popoverEl}>
    <div class="group" role="group" aria-labelledby="exclude-folders-label">
      <span class="label" id="exclude-folders-label">Exclude folders</span>
      {#if excludeFolders.length}
        <ul class="excluded">
          {#each excludeFolders as folder (folder)}
            <li>
              <button class="ng-mini" aria-label="Stop excluding {folder}" title="Stop excluding"
                      onclick={() => removeFolder(folder)}>
                <span class="name">{folder}</span><Icon icon={X} size="sm" />
              </button>
            </li>
          {/each}
        </ul>
      {/if}
      <select class="field" aria-label="Add a folder to exclude" onchange={pickFolder}
              disabled={!choices.length}>
        <option value="">{choices.length ? 'Add a folder…' : 'No other folders'}</option>
        {#each choices as folder (folder)}
          <option value={folder}>{folder}</option>
        {/each}
      </select>
      <input class="field other" aria-label="Exclude a folder by name" bind:value={otherText} onkeydown={otherKeydown}
             placeholder="Or type a name, press Return" />
    </div>
    <label class="label">Exclude notes (pattern)
      <input class="field" bind:value={patternText} onchange={() => apply()} placeholder="^Draft" />
    </label>
  </div>
{/if}

<style>
  .chip { position: fixed; top: var(--space-4); right: var(--space-4); }
  .chip[aria-expanded="true"] { color: var(--text-strong); border-color: var(--border-strong); }
  .popover {
    position: fixed; top: 54px; right: var(--space-4); width: 280px; box-sizing: border-box;
    padding: var(--space-3); z-index: 10;
    max-height: calc(100vh - 54px - var(--space-4)); overflow-y: auto; overscroll-behavior: contain;
    animation: popover-in 180ms ease;
  }
  @keyframes popover-in { from { transform: translateY(-4px); opacity: 0; } }
  .label { display: block; color: var(--text-muted); font-size: var(--text-xs); }
  .group + .label { margin-top: var(--space-3); }
  .excluded {
    display: flex; flex-wrap: wrap; gap: var(--space-1);
    list-style: none; margin: var(--space-1) 0 0; padding: 0;
  }
  .excluded .ng-mini { max-width: 100%; }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .field {
    display: block; width: 100%; box-sizing: border-box; margin-top: var(--space-1);
    background: var(--surface-sunken); border: 1px solid var(--border-control); border-radius: var(--radius-sm);
    color: var(--text-strong); font-family: inherit; font-size: var(--text-sm); padding: 7px 9px;
  }
  .field:focus { outline: none; border-color: var(--accent); }
  .field:disabled { color: var(--text-muted); }
  .other { font-size: var(--text-xs); padding: 5px 9px; }
</style>
