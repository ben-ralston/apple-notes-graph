<script>
  // Renders parsed note notation with Svelte elements only: every piece of note
  // text is a text node, so it can't become markup. Web links are styled text, not
  // anchors, because an anchor could navigate the Electron window.
  import SquareCheck from '@lucide/svelte/icons/square-check';
  import Square from '@lucide/svelte/icons/square';
  import Icon from './Icon.svelte';
  import { parseNote, resolvePreviewLink } from '../markdown/noteMarkdown.js';

  let { content, title, outgoing = [], visibleIds, onWalk, blockCount = $bindable(0) } = $props();

  // A parser bug must not blank the preview: null falls back to the plain text.
  let blocks = $derived.by(() => {
    try {
      return parseNote(content, { title });
    } catch {
      return null;
    }
  });

  $effect(() => {
    blockCount = blocks?.length ?? 0;
  });
</script>

{#snippet inlines(list)}{#each list as n}{#if n.type === 'text'}{n.text}{:else if n.type === 'bold'}<strong>{@render inlines(n.children)}</strong>{:else if n.type === 'italic'}<em>{@render inlines(n.children)}</em>{:else if n.type === 'strike'}<s>{@render inlines(n.children)}</s>{:else if n.type === 'underline'}<u>{@render inlines(n.children)}</u>{:else if n.type === 'code'}<code>{n.text}</code>{:else if n.type === 'color'}<span class="c-{n.name}">{@render inlines(n.children)}</span>{:else if n.type === 'tag'}<span class="tag">#{n.name}</span>{:else if n.type === 'attachment'}<span class="attachment">{n.label}</span>{:else if n.type === 'webLink'}<span class="web" title={n.url}>{n.text}</span>{:else if n.type === 'brokenLink'}<span class="broken" title="This note no longer exists">{n.name}</span>{:else if n.type === 'unknownLink'}<span class="unresolved">unknown link</span>{:else if n.type === 'link'}{@const id = resolvePreviewLink(n.name, outgoing, visibleIds)}{#if id !== null}<button class="note-link" onclick={() => onWalk(id)}>{n.name}</button>{:else}<span class="unresolved" title="Hidden by a filter, or more than one note has this title">{n.name}</span>{/if}{/if}{/each}{/snippet}

{#if blocks === null}
  <pre class="fallback">{content}</pre>
{:else if blocks.length === 0}
  <p class="empty">This note is empty.</p>
{:else}
  <div class="body" data-note-body>
    {#each blocks as b}
      {#if b.type === 'heading'}
        <div class="heading h{b.level}" class:gap={b.gapBefore} role="heading" aria-level={b.level + 2}>{@render inlines(b.inlines)}</div>
      {:else if b.type === 'paragraph'}
        <p class:gap={b.gapBefore}>{@render inlines(b.inlines)}</p>
      {:else if b.type === 'list'}
        <ul class:gap={b.gapBefore}>
          {#each b.items as item}
            <li style:padding-left="{item.depth * 16}px">
              {#if item.checked !== undefined}
                <span class="marker check" class:done={item.checked}><Icon icon={item.checked ? SquareCheck : Square} size="sm" /></span>
              {:else if b.ordered}
                <span class="marker number">{item.number}.</span>
              {:else}
                <span class="marker dot"></span>
              {/if}
              <span class="item-text" class:done={item.checked}>{@render inlines(item.inlines)}</span>
            </li>
          {/each}
        </ul>
      {:else if b.type === 'quote'}
        <blockquote class:gap={b.gapBefore}>{@render inlines(b.inlines)}</blockquote>
      {:else if b.type === 'code'}
        <pre class="code" class:gap={b.gapBefore}>{b.text}</pre>
      {:else if b.type === 'rule'}
        <hr />
      {:else if b.type === 'table'}
        <div class="table-wrap" class:gap={b.gapBefore}>
          <table>
            <thead><tr>{#each b.header as cell}<th>{@render inlines(cell)}</th>{/each}</tr></thead>
            <tbody>
              {#each b.rows as row}
                <tr>{#each row as cell}<td>{@render inlines(cell)}</td>{/each}</tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {/each}
  </div>
{/if}

<style>
  .body { color: var(--text-body); font-size: var(--text-md); line-height: 1.5; overflow-wrap: anywhere; }
  p { margin: 0; min-height: 1em; white-space: pre-wrap; }
  .gap { margin-top: var(--space-3); }
  .empty { margin: 0; color: var(--text-muted); font-size: var(--text-md); }
  .fallback {
    margin: 0; white-space: pre-wrap; overflow-wrap: anywhere;
    color: var(--text-body); font: var(--text-sm) / 1.5 -apple-system, sans-serif;
  }

  .heading { color: var(--text-strong); font-weight: 600; line-height: 1.3; margin-top: var(--space-2); }
  .heading.gap { margin-top: var(--space-4); }
  .heading:first-child { margin-top: 0; }
  .h1 { font-size: var(--text-xl); }
  .h2 { font-size: var(--text-lg); }
  .h3 { font-size: var(--text-md); }

  ul { list-style: none; margin: 0; padding: 0; }
  li { display: flex; gap: var(--space-2); padding-block: 1px; }
  .marker { flex-shrink: 0; color: var(--text-muted); }
  .dot { width: 14px; display: flex; justify-content: center; }
  .dot::before { content: ''; width: 4px; height: 4px; margin-top: 8px; border-radius: 50%; background: var(--text-muted); }
  .number { min-width: 14px; text-align: right; font-variant-numeric: tabular-nums; }
  .check { display: inline-flex; margin-top: 3px; }
  .check.done { color: var(--accent); }
  .item-text { min-width: 0; white-space: pre-wrap; }
  .item-text.done { color: var(--text-muted); }

  blockquote {
    margin: 0; padding-left: var(--space-3);
    border-left: 2px solid var(--border-strong); color: var(--text-muted);
    white-space: pre-wrap;
  }
  .code, code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: var(--surface-sunken); border-radius: var(--radius-sm);
  }
  code { padding: 1px var(--space-1); font-size: 0.92em; }
  .code {
    margin-block: 0; padding: var(--space-2) var(--space-3);
    font-size: var(--text-sm); overflow-x: auto; white-space: pre-wrap;
  }
  hr { border: none; border-top: 1px solid var(--border); margin: var(--space-3) 0; }

  .table-wrap { overflow-x: auto; margin-block: var(--space-1); }
  table { border-collapse: collapse; font-size: var(--text-sm); }
  th, td {
    padding: var(--space-1) var(--space-2); border: 1px solid var(--border);
    text-align: left; vertical-align: top; overflow-wrap: normal; min-width: 48px;
  }
  th { color: var(--text-strong); background: var(--surface-sunken); font-weight: 600; }

  .note-link {
    display: inline; padding: 0; background: none; border: none; cursor: pointer;
    color: var(--link); font: inherit; text-align: left;
  }
  .note-link:hover { color: var(--link-hover); text-decoration: underline; }
  .unresolved { color: var(--text-muted); }
  .broken { color: var(--danger); text-decoration: line-through; }
  .web { color: var(--text-body); text-decoration: underline dotted var(--text-muted); text-underline-offset: 2px; }
  .tag, .attachment {
    display: inline-block; padding: 0 6px; border-radius: var(--radius-pill);
    font-size: var(--text-xs); line-height: 16px; vertical-align: 1px;
  }
  .tag { color: var(--accent); background: var(--accent-soft); }
  .attachment { color: var(--text-muted); border: 1px solid var(--border-control); }

  .c-red { color: #ff8a80; }
  .c-orange { color: #fdba74; }
  .c-yellow { color: #fde68a; }
  .c-green { color: #86efac; }
  .c-teal { color: #5eead4; }
  .c-blue { color: #93c5fd; }
  .c-indigo { color: #a5b4fc; }
  .c-purple { color: #c4b5fd; }
  .c-pink { color: #f9a8d4; }
</style>
