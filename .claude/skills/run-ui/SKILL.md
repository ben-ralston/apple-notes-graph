---
name: run-ui
description: >
  Launch, drive, screenshot, and record the Notes Graph Electron app via a Playwright REPL
  driver. The verified path for seeing UI changes actually working — jump/select notes, read
  the stats chip, walk the inspector, capture PNGs, and burst frames into a GIF.
  USE WHEN "run the ui", "start the app", "screenshot the ui", "screen record the ui",
  "test the electron app", "see the graph", or verifying any change under src/ui/,
  src/main.js, or src/preload.js.
user_invokable: true
---

# Run UI — Notes Graph Electron app

Drive the app via the Playwright REPL at `.claude/skills/run-ui/driver.mjs`. This runs on
macOS in the live GUI session — **a real window appears on the user's screen** (1400×900).
No xvfb, no `--no-sandbox`.

The UI (`src/ui/`, Vite + Svelte 5) renders the graph on a `<canvas>` — there are no DOM
node elements to query or click. All interaction goes through the `window.__test` hook
(set up via `extendTestHook` calls in `App.svelte` and the components) — see
"`window.__test` API" below. The driver's `click-node`/`nodes`/`open-node` commands are thin
wrappers over that hook; anything the hook doesn't cover, drive with `eval`.

## Prerequisites

- `npm install` (needs the `playwright-core` devDependency — no browser download required)
- `uv sync` + Full Disk Access, so `scripts/extract_notes.py` can read the live Notes DB.
  Without it `fetch-notes` returns `{error: ...}`, the status chip shows
  `Error: ...`, and the app still reaches `ready: true` (readiness means "app booted", not
  "data loaded") — the driver won't hang, but `launch` won't show real note/link counts.

## Run (agent path)

Build first: `npx vite build` (the packaged app loads `dist-ui/`; a stale build shows stale
UI — the driver doesn't rebuild for you, and a running app must be relaunched to pick up a
fresh build). Readiness keys off `window.__test.ready` (set by `App.svelte` once mounted and
data fetched), not DOM node counts.

For the dev-server (HMR) path instead of the built bundle, set `VITE_DEV_SERVER_URL` before
starting the driver (Electron inherits it): run `npx vite --port 5173` in one terminal, then
`VITE_DEV_SERVER_URL=http://localhost:5173 node .claude/skills/run-ui/driver.mjs` in the tmux
pane below. Useful for verifying the HMR path itself still boots (CSP is `script-src 'self'`;
same-origin `http://localhost:5173` scripts satisfy it).

```bash
tmux new-session -d -s ngui -x 200 -y 50
tmux send-keys -t ngui 'cd <repo-root> && node .claude/skills/run-ui/driver.mjs' Enter
timeout 20 bash -c 'until tmux capture-pane -t ngui -p | grep -q "driver>"; do sleep 0.2; done'
tmux send-keys -t ngui 'launch' Enter
timeout 90 bash -c 'until tmux capture-pane -t ngui -p | grep -qE "launched\.|ERROR"; do sleep 0.5; done'
tmux capture-pane -t ngui -p | tail -3    # expect: launched. <N> notes · <M> links · <H> hubs · <O> orphans
tmux send-keys -t ngui 'ss landing' Enter
```

Launch takes ~15–25s: most of it is `extract_notes.py` reading every note from the Notes database. The driver's
`launch` only returns once `window.__test.ready` is `true` (mounted + initial fetch resolved).

Screenshots land in `/tmp/notes-graph-shots/` (override: `SCREENSHOT_DIR`).
**Always Read the screenshot after taking it** — a blank frame means the app didn't render.

### Commands

| command | what it does |
|---|---|
| `launch` | launch the app, wait until `window.__test.ready` is true |
| `ss [name]` | screenshot → `$SCREENSHOT_DIR/<name>.png` |
| `click-node <name>` | select a note by name via `window.__test.select` (exact match, then substring) |
| `open-node <name>` | resolve the note via `window.__test.nodes()` and call `notesAPI.openNote` — **opens the note in Apple Notes.app** (real side effect) |
| `nodes [filter]` | list `window.__test.nodes()`: name, connection count, cluster id |
| `stats` | print `window.__test.stats()` + `window.__test.status()` |
| `click <css-sel>` | DOM-click a real element — still needed for chrome like the palette-hint button, Filter chip (`[aria-controls=filter-popover]`), inspector's close button (`.close`), focus-chip's exit button |
| `fill <css-sel> <text>` | fill an input and fire `change` — for the `FilterPopover` inputs (`input.other` = the free-text folder fallback, which commits on Return only: `fill input.other <name>` then `press Enter`). The selector is the first word, so it can't contain spaces |
| `frames <name> <count> <ms>` | screenshot burst → `$SCREENSHOT_DIR/<name>/NNN.png` — for recordings |
| `type` / `press` / `wait` / `eval` / `text` | the usual Playwright helpers |
| `meval <expr>` | evaluate in the Electron **main** process (`electron` module in scope) — e.g. `meval electron.BrowserWindow.getAllWindows()[0].webContents.getZoomLevel()`; menu items can be driven via `Menu.getApplicationMenu()` + `item.click(undefined, win, win.webContents)` |
| `quit` | close app, exit REPL |

### `window.__test` API

The canvas has no DOM nodes, so this hook is the primary way to drive and inspect graph
state. Call it through `eval` (e.g. `eval window.__test.stats()` — see the eval gotcha
below for the no-arrow-function rule). Assembled piecemeal by `extendTestHook` calls in
`App.svelte`, `GraphCanvas.svelte`, `Inspector.svelte`, `StatsChip.svelte`,
`FilterPopover.svelte`, `CommandPalette.svelte`, `LinkSuggestions.svelte`, `UpdateChip.svelte`,
and `Toast.svelte`:

| call | returns |
|---|---|
| `ready` | `true` once mounted and the initial fetch has resolved (property, not a function) |
| `status()` | status-chip text, e.g. `"318 notes · 268 links · 12 hubs · 40 orphans"` or an `Error: ...` string |
| `nodes()` | `[{id, name, x, y, connections, cluster, ghost}, …]` for every rendered node (ghosts only appear while the health lens is on) |
| `select(name)` | selects by exact name, then substring fallback; returns `true`/`false` |
| `selection()` | `{id, neighborIds}` — `id` is `null` when nothing is selected |
| `inspector()` | `{name, ghost, links, backlinks, sources, deleteState, deleteError, modifiedLabel, previewBlocks}` for the selected note, or `null` — ghost nodes (health lens) report `ghost: true` plus their `sources`; `deleteState` is `idle\|confirming\|deleting`; `previewBlocks` counts the rendered note's blocks |
| `focus()` | `{rootId}` in focus mode, else `null` |
| `view()` | current canvas transform `{x, y, k}` — ⌘K jumps fly here (node centered, k ≥ 1.5) |
| `openPalette()` | opens the ⌘K palette (equivalent to the button/shortcut) |
| `palette(query)` | fuzzy-search results for `query` as a name array — **headless**: doesn't type into the visible input, so a screenshot after this call won't show the query; use `type <text>` after `openPalette()` to see it rendered |
| `setFilters({excludeFolders, excludePattern})` | sets the in-memory filter state; pass `persist: false`-equivalent by not calling `apply(true)` — it does **not** touch `config.json` or `localStorage` (safe to call repeatedly without backing up config) |
| `folderFilter()` | `{excluded, choices}`: the excluded-folder chips and the folder dropdown's options (graph folders not already hidden). The dropdown is a real `<select aria-label="Add a folder to exclude">`: set `selectedIndex` and dispatch `change` from `eval` to pick one; that persists to `config.json`, so redirect the `load-config` / `save-config` IPC handlers with `meval` first |
| `lens()` | `"health"` while the health lens is on, else `null` |
| `setLens(on)` | switches the health lens programmatically (same as the health chip / `> Toggle health lens` palette command) |
| `healthChip()` | `{orphans, broken, stale}` counts scoped to the visible graph (what the summary chip shows) |
| `suggestLinks()` | triggers the link-suggestion flow for the selected note (same as the "Suggest links" inspector button). Recall parses every note body — poll `suggestions().state` for up to ~60s |
| `suggestions()` | `{state, rankState, cards:[{name, ranked, reason, chips}]}` — `state`: `idle→recalling→ready` (or `error`); `rankState`: `idle→ranking→ranked` (or `failed` = recall-order fallback) |
| `addSuggestedLink(name)` | confirm + write a suggested link into the selected note — **REAL Apple Notes write** (write-guard overlay appears, Notes.app briefly frontmost); resolves after the write + graph refresh |
| `deleteNote()` | confirm + delete the selected note — **REAL Apple Notes delete** (moves to Recently Deleted; pure AppleScript, no focus steal). Returns `false` for ghost/no selection; resolves after delete + selection clear + graph refresh. Only use on throwaway test notes |
| `semanticResults()` | current semantic hit names for the open palette query (empty until the debounced `semantic-search` IPC returns; empty forever if embeddings aren't built — degradation rule) |
| `clusterLabels()` | `{clusterId: label}` LLM cluster names currently drawn as region labels (empty until naming completes; cached in localStorage across launches) |
| `labels()` | ids of the node labels placed in the last frame |
| `toasts()` | current toast pill texts, e.g. `["Naming clusters…"]` — first launch shows the naming toast, cache-hit launches stay silent |
| `uiUpdate()` | `{stale}`: `true` when the installed app's UI differs from the checkout (the "UI update ready" chip). Always `false` in dev |

Node identity is the Apple Notes **pk** (integer) — `select`/`nodes` key off `name` for
convenience, but `selection().id` and `inspector()`/backlink walks are pk-based under the
hood.

### Verification hooks

- `window.__test.status()` (also mirrored at `[data-status]` in the DOM via `StatsChip`):
  `<N> notes · <M> links · <H> hubs · <O> orphans` on real data, or `Error: ...` on a failed fetch.
- `window.__test.stats()` returns `{notes, links, hubs, orphans}` — reflects the current
  filter/focus scope (e.g. entering focus mode shrinks these to the 2-hop neighborhood).
- `window.__test.selection()` / `inspector()` / `focus()` are the ground truth for
  selection state — screenshots confirm the *visual* result, these confirm the *data*.

## Screen recording

Playwright's `recordVideo` is **not supported for Electron** (window never becomes ready,
no webm is written — verified). Instead: capture a `frames` burst during the animation, then
assemble with ffmpeg:

```bash
# in the driver: the simulation freezes when settled and only re-warms on a drag, and launch
# pre-ticks. To record a settle, start a drag in the background (dispatch mousedown on the
# canvas, then mousemove/mouseup on window, from eval) and capture it straight away:
#   frames settle 10 300
ffmpeg -y -framerate 4 -pattern_type glob -i '/tmp/notes-graph-shots/settle/*.png' \
  -vf 'scale=800:-1' /tmp/notes-graph-shots/settle.gif
```

For embedding screenshots/GIFs into a PR body, hand the captured files to the `pr-media`
skill — don't re-capture there.

## Run (human path)

```bash
./run ui-start   # npm start — opens the window; Ctrl-C to quit
```

## Gotchas

- **The graph is a `<canvas>`, not DOM** — there's nothing to `querySelector` for a node;
  everything node-related goes through `window.__test` (see the API table above). Real DOM
  still exists for chrome (palette, filter popover, inspector, buttons) — `click`/`fill`
  work there as normal.
- **`eval` takes a plain expression, not an arrow function** — `eval window.__test.stats()`
  works; `eval () => window.__test.stats()` evaluates to a function reference and
  `JSON.stringify`s to `"undefined"` (the driver's `eval` does
  `page.evaluate(expr)` where `expr` is the raw string you typed — no call happens if it's a
  function literal).
- **`page.waitForFunction(fn, arg, options)`** — options is the THIRD argument; passing
  `{timeout}` second silently uses the 30s default.
- **A stale `dist-ui/` build is invisible until relaunch** — `npx vite build` after a source
  change doesn't affect an already-running app; `quit` and `launch` again (or just `launch`
  fresh) to pick it up.
- **`open-node` touches Apple Notes.app** — it activates Notes and shows the note. Fine
  locally, but it's a real side effect; don't do it in bulk.
- **Extraction failures surface as an Error status, not a hang** — `fetch-notes` errors
  return `{notes: [], links: [], health: null, stats: null, error: <message>}`, and the
  status chip shows `Error: ...` (the app still reaches `ready: true`). If the status chip
  shows an error when you expected real notes, check `.venv` exists and the terminal has
  Full Disk Access; the Python stderr lands on the Electron process's own console, so run
  `npm start` in a terminal to see it directly.
- **Electron steals stdin** — the driver reads `/dev/stdin` via a raw fd; don't "simplify"
  that away.
- **Screenshots go through `webContents.capturePage()`, not `page.screenshot()`** — the app
  defaults to zoomLevel 1 (Cmd+0 returns there), and Playwright's `page.screenshot` crops the
  window under any non-zero zoom. `ss`/`frames` already handle this; don't switch them back.
- **System Events keystrokes/menu clicks are unreliable from a sandboxed agent shell** — they
  exit 0 without delivering. To exercise menu items (zoom, etc.), use `meval` with
  `Menu.getApplicationMenu()` and `item.click(undefined, win, win.webContents)` instead.
