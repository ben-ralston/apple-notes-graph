# Notes Graph - Project Context

## Project Overview

This project contains three related components:
1. **Notes Graph UI** - An Electron app that visualizes Apple Notes as an interactive force-directed graph
2. **Apple Notes MCP Server** - A Model Context Protocol server that exposes Apple Notes data to AI assistants
3. **UI Helper Daemon** - A LaunchAgent daemon that runs in the GUI session, enabling remote (SSH) write operations to Notes via Unix socket IPC

## Quick Facts

- **Repository**: ben-ralston/apple-notes-graph
- **Package Manager**: npm (Electron app), uv (Python MCP server)
- **Python Version**: >=3.11

## Tech Stack

### Electron App (Notes Graph UI)
- Electron 42 (desktop app framework)
- Vite 8.x + Svelte 5 (renderer, `src/ui/`) — built to `dist-ui/` via `npm run build:ui`;
  `npm run dev` runs Vite's dev server + Electron together for HMR
- Canvas 2D graph rendering — `d3-force` simulation, `d3-quadtree` for hit-testing,
  `d3-zoom` / `d3-drag` for pan/zoom/drag (no SVG, no DOM per-node)
- Vitest for JS unit tests (`npm test`)

### MCP Server (apple-notes-mcp)
- Python 3.11+
- FastMCP (MCP server framework)
- bbpb (Apple Notes protobuf parsing)
- SQLite (reads Apple Notes database directly)

## Architecture

### Directory Structure
```
src/
├── apple_notes/              # Core Apple Notes data access library
│   ├── database.py           # SQLite queries against Notes database
│   ├── note_content.py       # Note content parsing and link resolution
│   ├── protobuf_parser.py    # Apple Notes protobuf format parsing
│   ├── models.py             # Data models (Note, NoteContent, etc.)
│   ├── exceptions.py         # Custom exceptions
│   ├── librarian.py          # Graph primitives (orphans, clusters, connection counts, ...)
│   ├── ui_payload.py         # get_ui_graph_data — assembles the UI graph payload (pk-keyed
│   │                          # nodes/links + cluster ids + health block)
│   ├── ui_operations.py      # Shared UI scripting orchestration (clipboard + executor)
│   ├── write_service.py      # Shared write chains: append (backup → resolve → convert →
│   │                          # local/daemon route; MCP append + add_link.py), delete
│   │                          # (guards → backup → route → sync; MCP delete_note +
│   │                          # delete_note.py) and move (guards → backup → by-id move)
│   ├── note_target.py        # delete/move guards (#229/#230): exact-one-note, not in
│   │                          # Recently Deleted, not locked; folder resolution; ids
│   ├── embeddings.py         # fastembed semantic-search cache (build + cosine search)
│   ├── link_tokens.py        # the one parser for a note-link token, both stored forms (#247)
│   ├── write_verify.py       # pure post-write content checks (append/replace/find_replace,
│   │                          # checklist states; issues #200/#196)
│   ├── ui_helper_daemon.py   # Asyncio Unix socket daemon (LaunchAgent)
│   └── ui_helper_client.py   # Synchronous Unix socket client
├── apple_notes_mcp/          # MCP server
│   ├── server.py             # FastMCP server (read, write, and librarian tools)
│   ├── remote/               # remote mode (#167): OAuth, allowlist, HTTP app, CLI
│   └── reminders/            # Apple Reminders for remote mode (#171): EventKit store,
│                              # read tools, wrapper grant check
├── ui/                       # Electron renderer (Vite + Svelte 5, canvas graph)
│   ├── App.svelte            # Top-level layout, data fetch, loading/empty/error states, keyboard shortcuts (F, Esc)
│   ├── testhook.js           # extendTestHook — assembles window.__test for the run-ui driver
│   ├── stores.js             # Svelte stores: graph, selection, focus, filters
│   ├── index.html            # Vite entry HTML (CSP: script-src 'self')
│   ├── main.js                # Vite/Svelte mount entry
│   ├── tokens.css             # Design tokens (spacing, radii, type, colour, motion); tokens.test.js checks contrast
│   ├── chrome.css             # Shared ng-* classes for pills, panels, buttons, headings
│   ├── layout.js              # Inspector width shared with the canvas
│   ├── graph/                # GraphCanvas.svelte (canvas render loop, sim, zoom/drag/hover),
│   │                          # simulation.js (layout + unlinked ring), labels.js (placement),
│   │                          # view.js (fit/centre), tween.js (position continuity),
│   │                          # colors.js, focus.js, testGraph.js (synthetic test graph),
│   │                          # clusterNames.js (member-set hash keys + centroids)
│   ├── markdown/              # noteMarkdown.js: parser for read_note_content's notation
│   ├── components/            # Inspector, NoteBody (rendered note), Icon (Lucide, one size scale),
│   │                          # CommandPalette, FilterPopover, StatsChip, HealthChip,
│   │                          # LinkSuggestions (recall→rank cards + add-link), Toast
│   ├── clusterNaming.js       # auto cluster-naming flow (localStorage cache → name-clusters)
│   ├── folderFilter.js        # excluded-folders rule + dropdown options (#146; config format unchanged)
│   ├── toast.js               # transient progress pill store
│   ├── suggest/               # suggest.js — reason chips + rank/recall merge
│   └── search/                 # fuzzy.js + semantic.js (⌘K matchers)
├── app-paths.js              # resolveAppPaths: the one place that knows where Python and bundled files are
├── app-lifecycle.js          # navigation guard, single instance, focus-or-create, quit on failed start, dev profile
├── startup-checks.js         # packaged-only checks + their dialogs: checkout present, Full Disk Access
├── window-state.js           # packaged-only window memory: parse, on-screen check, debounced tracking
├── ui-fingerprint.js         # content hash of packaged paths; drives the update chip; also a CLI for install
├── main.js                   # Electron main process (IPC: fetch-notes, read-note, open-note,
│                              # load-config, save-config, suggest-links, rank-suggestions,
│                              # add-link, delete-note, semantic-search, name-clusters)
├── claude-runner.js          # headless `claude -p` spawn (CLAUDE_MODEL constant = the
│                              # Sonnet↔Haiku switch; context-free: --setting-sources "" +
│                              # own --system-prompt, neutral cwd)
├── claude-llm.js             # pure prompt builders + hallucination-dropping parsers
├── preload.js                # Electron preload script (contextBridge → window.notesAPI)
scripts/
├── extract_notes.py          # IPC-invoked: calls get_ui_graph_data(), prints JSON payload v2
├── read_note.py              # IPC-invoked: --name or --pk, JSON note content for the inspector
├── suggest_links.py          # IPC-invoked: --pk, link-suggestion recall (candidates+reasons)
├── add_link.py               # IPC-invoked: --pk --target, appends [[Target]] via write_service
├── delete_note.py            # IPC-invoked: --pk, moves note to Recently Deleted via write_service
├── build_embeddings.py       # background: incremental fastembed cache build
└── semantic_search.py        # IPC-invoked: --query, cosine top-k over the embedding cache
dist-ui/                      # npx vite build output (gitignored; loaded by main.js in prod)
launchd/
└── com.notes-graph.ui-helper.plist.template  # LaunchAgent plist template
tests/
├── test_mcp_server.py        # MCP server tests
└── test_ui_helper.py         # UI helper daemon + client tests
```

### Notes Graph UI renderer notes

- **`window.__test` hook** (`src/ui/testhook.js` + `extendTestHook` calls throughout
  `src/ui/`): the canvas graph has no DOM per-node elements, so agent/e2e driving goes
  through this hook (`ready`, `nodes()`, `select(id)`, `selection()`, `focus()`,
  `inspector()`, `palette(query)`, `openPalette()`, `setFilters()`, `folderFilter()`, `stats()`,
  `lens()`, `setLens(on)`, `healthChip()`, `suggestLinks()`, `suggestions()`,
  `addSuggestedLink(name)`, `semanticResults()`, `clusterLabels()`, `labels()`, `toasts()`, `uiUpdate()`). See
  `.claude/skills/run-ui/SKILL.md` for the full API table and the driver that uses it.
- Node identity is the Apple Notes **pk** (integer) everywhere in the payload and UI —
  never the note name (duplicate titles exist in real data).
- **Layout (#239)**: `createSimulation` pre-ticks linked notes only, with weak links between
  clusters and a per-cluster pull, shaped to the window. Notes with no *visible* links (not
  `cluster === -1`, so filtered views behave) go on a deterministic rounded-rectangle ring and
  stay out of repulsion, or every drag would push the ring outward. A graph that's at least 90%
  placed only settles (low alpha), so refreshes, filters, lens toggles and focus don't rearrange
  the map; `refreshGraph` seeds new note objects with old positions by pk for the same reason.
- **Labels (#239)**: `placeLabels` is greedy in a fixed priority order (no flicker) with a
  left-side fallback. Labels never overlap each other or leave the viewport (which excludes the
  open Inspector), and only hubs (8+ connections) may cross other nodes. Pinned labels (hover,
  selection) always appear in full.
- **Note preview (#239)**: `noteMarkdown.js` parses to plain data and `NoteBody.svelte` renders
  text nodes only: no `{@html}`, and no `<a href>` because an anchor could navigate the Electron
  window (`icons.test.js` enforces both). Inline parsing must stay linear on hostile text, so
  closer lookups are cached and names are length-capped; timing tests pin this. A `[[link]]` is
  clickable only when exactly one outgoing note has that title. Add new note notation there, and
  check it against real notes.
- `npm start` (`build:ui` + `electron .`) is the production path; `npm run dev`
  (`concurrently` running `vite` + `electron` with `VITE_DEV_SERVER_URL` set) is the HMR
  dev path — both load through the same `main.js`/CSP.
- **Intelligence layer (#117) is additive, never load-bearing**: LLM/embedding failure
  degrades to recall-order suggestions / fuzzy-only ⌘K / unnamed clusters; IPC errors
  travel as `{error}` data, never thrown. Headless `claude -p` calls MUST stay
  context-free (see `src/claude-runner.js` — inheriting user CLAUDE.md/hooks makes the
  model refuse the prompt and hook children can wedge the pipe). Renderer-side: always
  `$state.snapshot()` any `$state`-held object before passing it to `window.notesAPI.*`
  (Svelte 5 proxies fail Electron's structured clone). Embeddings cache lives at
  `~/Library/Application Support/notes-graph/embeddings.json` (incremental, built in the
  background after fetch-notes); cluster-name cache in renderer localStorage keyed by
  member-set hash. Tests that load the real fastembed model carry the `embedding_model`
  marker (excluded in CI with `macos_gui`).

### Installed app (#238)
`./run ui-app-install` builds `/Applications/Notes Graph.app`. The asar holds `package.json`, `src/*.js`, `dist-ui` and the icon; Python runs from the primary worktree, baked in as `notesGraphRepoRoot` in the packaged `package.json` (alongside `notesGraphUiFingerprint`).
- **After merging changes to `src/*.js`, `src/ui/**`, `package.json`, `package-lock.json`, `vite.config.js` or `assets/**`, the post-merge step includes `./run ui-app-install`** (quit the app first). Python-only changes apply on `git pull`.
- `app.isPackaged` gates the startup dialogs, single-instance lock, window memory and update chip. Dev uses a separate `Notes Graph Dev` userData so it can run beside the installed app.
- The window blocks navigation away from the UI and all new windows: the preload exposes `notesAPI` to whatever page it shows, and the app holds Full Disk Access. Open external links with `shell.openExternal`, never in-page.
- All script spawns go through `runScript` / `scriptOptions` in `main.js`. Never rebuild paths from `__dirname`: in the packaged app that's inside the asar.
- **Verify packaged behaviour with `open -a`, never by launching the binary from a terminal.** A terminal launch makes the terminal the app macOS checks, so its Full Disk Access hides a missing grant. Attach with `open -a … --args --remote-debugging-port=<port>` and playwright-core `connectOverCDP`; `window.__test` exists in the packaged build.
- The app holds Full Disk Access and Accessibility but has no hardened runtime or fuses, by decision: same-user code is trusted, and hardening can't protect scripts in a user-writable checkout. See the README's Security note.

### MCP Server Tools
The MCP server exposes these tools:
- `list_notes(folder?)` - List all notes, optionally filtered by folder
- `read_note_content(name)` - Read a note's full content with resolved `[[links]]`
- **Duplicate titles (#83)**: every tool that addresses an existing note takes its title or its id (the uuid; `note_target.is_note_id`). Reads (`read_note_content`, `get_backlinks`, `export_note`, `suggest_links`) go through `note_target.resolve_read_note`: an id reads that note; a shared title reads a live note before one in Recently Deleted, then the newest, and returns the rest as `other_matches` (id, title, folder, modified; case-insensitive, folder scope honoured via `NotesDatabase.is_visible`). Writes open notes by title, so an id becomes its title (`title_for_ref`) and the title must be unique: `require_unique_title` for local update_note_content / toggle_checklist_item (the others had guards), and the remote queue maps an id to its title at accept. Only live notes count for update_note_content / toggle_checklist_item (local and the remote queue's `LIVE_TITLE_TOOLS`): one live note beside copies in Recently Deleted stays writable, because `ui_scripting._note_spec` then addresses the live note as `note id "x-coredata://…"` (`note_target.live_note_id_beside_trashed_twins`), since by-title AppleScript can bind the trashed copy; the other write tools still count Recently Deleted. A `[[link]]` to a title several live notes share is refused naming each match (`write_service.SharedTitle`). Tool tests that mock below the tool use the `stub_note_resolution` fixture (`tests/conftest.py`)
- `search_notes(query, search_content?)` - Search notes by title or content
- `get_note_graph()` - Get all notes and link relationships for graph visualization
- `find_orphans()` - List notes with no incoming or outgoing note links
- `find_broken_links()` - List note links whose target note no longer exists
- `note_health_check(stale_threshold_days?, top_n?)` - Graph health report: orphans, broken links, stale notes, top-connected, clusters, duplicate titles
- `suggest_links(note_name, max_candidates?)` - Note content + link candidates with transparent recall `reasons`; the client does the semantic ranking (no server-side scoring)
- `update_note_content(name, body, operation)` - Update a note's content. `operation` is required: `append`, `replace`, or `find_replace`. `find_replace` is scoped to the note **body** — the first line (the Apple Notes title) is excluded from both the exactly-once guard and the richtext replacement (issue #120)
- `replace_note_link(name, old_target, new_target, replace_all?)` - Re-point existing note links (issue #183). Targets are a title or uuid (`old_target` matches only the note's own links; a link to a deleted note matches by uuid only). It refuses on 0 matches, and on more than 1 unless `replace_all`. Mechanism: copy the full richtext, rewrite the link attachment entries' field `"7"` (alt) and the uuid inside `"34"` (token JSON), then paste. Notes rebuilds links from those fields, with **no Cmd+K keystrokes**. It verifies from NoteStore via `NotesDatabase.get_note_link_targets`, never body HTML, which renders links as `<br>`. Shared guards live in `apple_notes/relink.py`, used by both stdio and `remote/write_apply.py`
- `edit_note_section(name, operation, heading | section_text, body, old_text, new_text)` - Edit one section (issues #184, #240); `operation` is `replace`, `append`, `find_replace` or `delete`. A section is whole paragraphs; a heading's section runs to the next same-or-higher heading and excludes the heading line; `heading=` is compared with the rendered line minus its `#` prefix, so a formatted heading keeps its markdown (`## **Plan**` → `heading="**Plan**"`); `section_text` uses `read_note_content` notation. Mechanism (`apple_notes/section_edit.py`, `sections.py`, design `docs/design/240-section-edit-model.md`): copy the whole note, splice the section in Python (`richtext.slice_richtext` / `replace_range`, the `build_*` builders unchanged), paste the whole note back. Keyboard navigation was removed: Option+Up/Down stop at a `\r` soft break, which misplaced edits (#240). Only allowlisted keys (Cmd+A/C/V/Z, Left), with a frontmost check and a note-identity check before every key; the selection is collapsed after each copy. Lost-update checks before the paste: the editor must match NoteStore (text, paragraph styles, formatting, link targets, up to 6s), a second copy must match the first once parsed (raw bytes differ between idle copies), and the paste is built from that later copy, so a table cell edit the comparison can't see is carried, not reverted. Refusals a retry can clear are `NoteBusyError`, which the remote queue retries once. The after-paste DB diff is **content level** (`sections.content_changes`): text, kind, indent, checked, formatting, link targets (both token forms, `apple_notes/link_tokens.py`), table shape + sorted cells, media size. It ignores what every paste rewrites: run field 13, 12, 2.1, 2.3, 2.5, 2.9, and run splits or merges (#237). So `verified_outside_unchanged` means content, not bytes, and a table's row order isn't proven (#235). One extra empty final paragraph is tolerated on any edit unless the note already ended blank (a paste ending in a list adds it). On failure it sends one Cmd+Z, compares the whole note at content level, then reports loudly (`rolled_back` vs damaged + backup path); the log's `outside_fields` / `undo_diff_fields` carry index + field names only (#224). Refused above 25 MB of media (`refuse_heavy_media`, `ZFILESIZE` of the note's live block attachments) because every paste re-copies it, and when a copy exceeds 100 MB (scans aren't in that figure); each edit also leaves one stale row per link until Notes purges them. v1 limits: no new `{#tags}` or tables, table paragraphs can't be matched by text, a section that is blank right under the title is refused. Over SSH the daemon call has a 300s timeout with a distinct "may still complete" message
- `delete_note(name)` - Delete a note (moves to Recently Deleted, never permanent). Guards (`apple_notes/note_target.py`, #229, shared by stdio, the Electron delete script and the remote queue): the title must match exactly one note case-insensitively, Recently Deleted included (`NotesDatabase.find_title_matches`, scope-exempt like `count_notes_titled`), and equal it exactly; zero/several matches are refused with each match's folder + modified date; trashed and locked (`ZISPASSWORDPROTECTED`) notes are refused. Returns title, folder, modified and a ~200-char body preview
- `move_note(name, folder)` - Move a note into an existing folder (#230). Same note guards; the folder (`get_move_folders`: ZFOLDERTYPE 0 only, never the trash or a smart folder, never created) matches by full path, then name, then name without emoji prefix, refusing >1 in the deciding tier. Pure AppleScript by id: `x-coredata://<Z_METADATA.Z_UUID>/ICNote|ICFolder/p<pk>` (`note_target.coredata_id`, validated before interpolation); daemon command `move_note`
- `export_note(name, output_dir, format?)` - Export a single note as a Markdown file
- `export_folder(folder, output_dir, format?)` - Export all notes in a folder as Markdown files
- `export_graph(output_dir, format?)` - Export the graph as `json`, `dot`, or `csv`

Write/delete tools return `sync_nudged: true/false` indicating whether iCloud sync was nudged (controlled by `NOTES_GRAPH_SYNC_NUDGE` env var).

### Remote mode (issue #167)
`apple-notes-mcp-remote` (`src/apple_notes_mcp/remote/`) serves allowlisted COPIES of the
stdio tools over Streamable HTTP at `/mcp` on 127.0.0.1:9893 behind an OAuth proxy to GitHub
(one allowed login + REQUIRED numeric id, after our own CSRF-guarded consent page). `server.mcp` and `server.main()` are only read, never
changed - stdio stays identical (annotations are applied to the remote copies only).
- `remote/tools.py`: every stdio tool must be in exactly one of `READ_TOOLS` / `WRITE_TOOLS`
  (opt-in via `write_tools`; delete_note + move_note since #229/#230) / `NEVER_REMOTE`
  (export_*). **A new tool must be
  categorized there** or the remote server refuses to start (`test_remote_tools.py` fails).
- `remote/auth.py`: SDK `OAuthAuthorizationServerProvider` + `TokenStore` (hashed tokens,
  chmod-600 JSON re-read per lookup and written under an `flock` shared with the `revoke-all`
  CLI, so revocation hits a running server and can't be undone by a concurrent save; upstream GitHub URLs
  are config fields so tests use a fake IdP (`httpx.MockTransport`, `tests/test_remote_oauth.py`).
- Remote tool copies run in a worker thread (`tools.threaded`): stdio tools are sync and
  would otherwise block the event loop for every other request.
- Remote **write tools are queued** (#169): `remote/write_tools.py` copies each stdio write
  tool (same schema) with a function that calls `write_apply.WriteApplier.accept` and
  returns `{"status": "queued", "write_id"}` at once. `write_queue.py` is the
  sqlite store (caps, atomic `claim`, `drain.lock` flock, expire/purge).
  `write_worker.py` (a daemon thread started only by `cli._serve`) applies FIFO when the
  session is unlocked: immediately if accepted during the current unlocked stretch, else
  `apply_delay_after_unlock_seconds` (60) after unlock. Apply calls the **stdio tool
  function itself**, so every guard (backup, edit_lock, overlay, frontmost, verify)
  stays identical. Errors are classified by exception type along the chain, never by
  ToolError text. A `PartialWriteError` (outermost verification error in the chain),
  or a focus loss after the note already changed, finishes `applied_unverified` (#200):
  final, never retried, post fingerprint captured so `undo` works; the worker counts it
  as applied. `get_write_status` is the one `REMOTE_ONLY_TOOLS` entry. CLI:
  `./run remote-writes`. delete_note / move_note (`TARGET_TOOLS`) take their own
  accept/apply path (`_resolve_target` / `_apply_target`): the note_target guards at both
  ends, the accepted response carries a `note` summary, the post-state poll checks the note
  reached Recently Deleted / the folder, and undo is refused. The applier must never take `edit_lock` (flock is not
  reentrant). The per-write `BackupManager` copy uses the SQLite online backup API, so
  it includes the WAL.
- Remote section edits (#184): `edit_note_section` is queued as operation `edit_note_section`, with `section_delete` for deletes; both are on by default in `allowed_operations`. `write_apply.py` refuses replace, find_replace and delete on sections over `MAX_REMOTE_SECTION_DELETE` (50) paragraphs (append is exempt) and any section edit on a note over the 25 MB media cap, at accept and at apply; new tags/tables are refused at accept too (#264). A conflict is raised only when the section itself changed since queueing. Undo covers text edits only (replace, append, find_replace), never delete or sections with tables or images, and is journaled only when the post-write span is proven (else "undo by hand"). The purge clears section text after 7 days.
- Remote mode sets `folder_scope.set_remote_mode(True)`: NotesDatabase then never serves
  Recently Deleted (by name/pk/uuid too); config exclusions stay a caller-overridable
  convenience. Trust-on-first-use client pin + `allow-client`; token-family reuse detection;
  `revoke-all` bumps an epoch that cancels in-flight sign-ins.
- Folder allowlist = `apple_notes/folder_scope.py`, enforced inside `NotesDatabase` (no-op
  when unset); links to live notes in hidden folders are dropped (not reported broken). Any NEW NotesDatabase read method must respect it. Deliberate exceptions: `count_notes_titled` and `note_fingerprint` (#169 write-queue safety checks, return only a count / hash; writes can't run with an allowlist), and `get_note_link_targets` (#183, the source note's scope is honored but it returns target uuids already in that note, hidden targets included, so a relink guard sees every link it would rewrite), and `find_title_matches` (#229/#230 delete/move guard: every same-titled note incl. Recently Deleted, with folder + modified date, so a refusal can name each match; writes can't run with an allowlist, and it reveals no more than `count_notes_titled`'s refusal already does), and `hidden_note_uuids` (#247, the uuids of live out-of-scope notes, so a read or a broken-link report can withhold them; never show its result).
- Request log (`~/Library/Logs/apple-notes-mcp/requests.log`) carries metadata only - never
  tool arguments, contents, query strings or tokens.
- Unified logger (#170): `cli._serve` calls `unified_log.configure(component="remote",
  redact=True)` - every event (profiler-hook tool failures included) carries the action
  and exception TYPE only, never the exception text or the `name="<title>"` detail.
  The one addition is a reason code (#255): `unified_log._REASON_CODES` maps a known
  message (exception type + message prefix, e.g. `OperationalError` "database is
  locked", `ClipboardError` "Pasteboard read timed out") to a constant such as
  `db_locked`; any other sqlite3 error falls back to its `sqlite_errorname`
  (`SQLITE_NOTADB` → `sqlite_notadb`, validated). Matching starts at the root error and
  follows explicit `__cause__` only, never the implicit context of an earlier handled
  error. `report_failure` logs it as `json_extra.reason` and in the message
  (`<action> failed: <Type> (<code>)`). Both modes use that message, since the collector
  fingerprints it; local mode moves the text to `json_extra.error_text` (+ `cause_text`
  when a cause set the code). The unhandled-exception event carries it in `json_extra`
  only (its message stays constant) and throttles per reason. Only constants are ever
  logged; add a cause by adding a row there, never by logging text.
  `remote/logger_events.py` adds config/startup failure (sync send, throttled across
  launchd restarts by a stamp file), an `UnhandledExceptionHandler` that must stay
  FIRST on the root logger (the redacting handler strips `exc_info`), and a
  `TokenExchangeMonitor` (a streak of 3 failed GitHub exchanges, at most once an hour,
  `bad_verification_code` ignored). `remote-writes` (`cli._writes`) configures the same
  redacted mode.
- Setup/runbook: `docs/remote-connector.md`; LaunchAgent template (not installed):
  `launchd/com.notes-graph.mcp-remote.plist.template` (runs via `AppleNotesMCPRunner
  --bridge-stdio` with `NOTES_GRAPH_PYTHON_ENTRY` pointing at the remote entry, for FDA).
- Reminders (issue #171): `apple_notes_mcp/reminders/` = PyObjC EventKit store + its own
  FastMCP source (reads `reminders_lists`, `reminders_list`; guarded writes
  `reminders_complete`, `reminders_create`, `reminders_update` (#232: edit title, due, notes,
  list, priority; compare-and-swap on `expected_due`/`expected_title`; refuses due edits on
  recurring reminders, which would re-anchor the whole series)), merged only when `remote.json` has
  `"reminders": {"enabled": true}`; categorized in `REMINDERS_READ_TOOLS` /
  `REMINDERS_WRITE_TOOLS` (`READ_TOOLS` mirrors stdio exactly). Writes are exposed only via
  `reminders.write_tools`; the guards are pure functions in `reminders/guards.py`. The
  completion guards and create's duplicate check run in the store under the same lock as the
  save; create's list checks (`create_lists`, ambiguous names) run in the tool just before. Refusals name their `remote.json` key and never carry
  reminder titles or notes. Under launchd, EventKit is TCC-checked against
  AppleNotesMCP.app, so the wrapper carries Reminders usage strings + the hardened-runtime
  `personal-information.calendars` entitlement (`AppleNotesMCP.entitlements`, passed by
  `build.sh` AND the `mcp-app-install` re-sign) and `--reminders-status` / `--probe-reminders`.
  The server never requests access.

### Timing/Profiling (issue #98)
Every tool is timed via a context-scoped profiler (`src/apple_notes/profiling.py`). Each action writes a human-readable `START` and `END` line to `~/Library/Application Support/notes-graph/timing.log` (same per-user dir as the socket + edit lock); the END line carries `total=<ms>`, a per-phase span breakdown, and an outcome (`ok` / `error:<Type>` / `timeout`). Write tools additionally return `elapsed_ms` + a `timing` dict (phases: `backup`, `convert`, `execute`, `sync`, `lock_wait` recorded by `edit_lock`, `indicator` — time spent spawning/confirming the write-guard overlay; `lead` — the deliberate lead-window wait before keystrokes, issue #108; and, since issue #141's keystroke batching, `open`, `focus`, `copy`, `paste` (ui_scripting) and `verify` (`ui_operations._verify_content`, issues #200/#196: append / replace / find_replace copy the note back out of the editor — Cmd+A, Cmd+C, Escape, inside the overlay and the clipboard save/restore — and re-copy for up to `VERIFY_POLL_TIMEOUT` = 2s until `write_verify`'s per-operation content check passes and the note changed (copy-back text/checked states, else body HTML, which sees formatting-only edits but not checked state); a write that truly didn't land shows ~2s here and raises `WriteVerificationError`, one that changed the note but isn't fully confirmed raises `PartialWriteError`; `toggle_checklist_item` copies the note back and checks the item's checked state, issue #177) — `copy` comes from `select_all_and_copy`, while `paste` also accumulates from the single-key `paste_from_clipboard` helper used on the append path and for pending-action/table pastes. Every pasteboard read records a span from inside `ClipboardManager`'s choke point — `clip_read_richtext`, `clip_read_rtf`, `clip_read_text`, and `clip_read_items` for the whole-clipboard snapshot (#125) — so all read sites are attributed (find_replace's post-copy reads, replace/checklist template reads, and `save_state`'s reads before every write); the find_replace richtext path additionally records `fr_compute` (pure-Python protobuf find/replace), `clip_write` (pasteboard write before the paste), and `pending` (`apply_pending_keyboard_actions` — the Cmd+F flow for tags/tables), all nested inside `execute` alongside the spans above); a write's link check (issue #212) is part of its `verify` span (add_note's own link copy-back records `verify` too); reads are logged only. Note: `lock_wait`, `indicator`, `lead`, `open`, `focus`, `copy`, `paste`, and `verify` are all nested sub-intervals of `execute`, not additive. `indicator` and `lead` now run just before the `open` span (the overlay is shown before Notes is activated, issue #169 follow-up), so they sit beside `open` inside `execute`; between issue #141 and that change they nested inside the first keystroke span (usually `focus`), so `focus` spans from that window include ~1.5s of lead. The UI helper daemon profiles its own command handler, so remote-path writes append to the same log. Historical note: END lines from before 2026-08-13 can show ~60s hiding in `execute` on successful writes — that was a macOS pasteboard promise lost-wakeup race in the clipboard read-back (`ClipboardManager._read_via_fresh_thread` documents it); all pasteboard reads now run on a fresh thread, which dodges it.

### Write-Guard Indicator (issue #108)
UI-scripting writes display a floating non-activating write-in-progress overlay ("Apple Notes MCP is creating/editing …") (`src/apple_notes/write_indicator.py` — context manager + AppKit helper subprocess with SHOWN handshake, stdin-EOF and 120s self-exit). Since issue #141, the overlay is registered at `write_indicator(...)` entry but only spawned by `show_pending()`, which `open_note` / `_activate_notes` call **before** bringing Notes to the front (and `_guarded_system_events` calls again, idempotently, for any keystroke path that skips them) — so the warning appears while the user's own app is still frontmost, not after the switch. After the overlay confirms visible, a lead window (default 1500ms, a depleting progress bar) holds before Notes is activated so the user can finish typing in their own app rather than into Notes; tune with `NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS` (`0` = immediate, no bar). Fail-open: indicator/lead failure never blocks a write, and the lead is skipped if the overlay never confirms. `delete_note` and read tools never show it. Disable the whole overlay with `NOTES_GRAPH_WRITE_INDICATOR=0`.

Frontmost readiness itself is guarded by a 5s wall-clock deadline (`_ensure_notes_frontmost`, `FRONTMOST_POLL_TIMEOUT`, issue #141) — always at least one probe, elapsed seconds reported in failure messages, and worst case ~15s (deadline + one in-flight probe budgeted up to `SYSTEM_EVENTS_TIMEOUT`) before giving up. A hung System Events call is separately capped and raises a distinct `NotesNotRespondingError` ("Apple Notes didn't respond in Ns…") rather than the old, misleading not-frontmost error. `SYSTEM_EVENTS_TIMEOUT = 10.0`s is the budget for a *single* event — one frontmost probe or one keystroke script; batched scripts pass `_guarded_system_events(..., timeout=…)` scaled to their statement count (focus = 3×, select-all+copy/paste = 2×, and the text-length-scaled `_type_text`/`_paste_text` = `TEXT_ENTRY_TIMEOUT = 30.0`s), so a slow-but-answering System Events can't kill a script between a select-all and its paired key while the per-script wait still stays bounded at ≤30s. The error message names the budget the failing script actually ran under.

An open sheet or dialog in Notes (issue #199) passes the frontmost-process check but would take the keystrokes, so `_frontmost_recheck()` also checks Notes' front window (`window 1`: a sheet on it, or an `AXDialog`/`AXSystemDialog` subrole) before every key and raises `NotesModalOpenError` (a `NotesNotFrontmostError` subclass: "a dialog is open in Notes (…); close it and retry"). It never dismisses the dialog. Keys a write sends into a dialog it opened itself run inside `_expecting_dialog()`, which drops only the dialog scan (the frontmost check stays): the Space + Backspace after a typed `#tag` (Notes' tag-suggestion popup is a separate `AXSystemDialog` window). A new keyboard flow that opens a Notes popup needs the same scope, or its next key is refused. The remote queue waits it out: the drain worker runs the read-only `ui_scripting.notes_modal_title()` probe before each apply and holds the queue (`MODAL_WAIT_REASON`, no attempt counted), and a write the dialog interrupts mid-flight is requeued the same way unless the note changed (then it finishes `applied_unverified`, #200).

### Apple Notes Database Access
The server reads directly from Apple Notes' SQLite database at:
`~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`

**Requires Full Disk Access** permission for the terminal/app running the server.

## Available Commands

```bash
./run --help                   # List all commands

# Notes Graph UI (Electron)
./run ui-start                 # Start the Electron app in dev mode
./run ui-build                 # Build the UI bundle (dist-ui/)
./run ui-test                  # Run Electron tests
./run ui-app-install           # Build Notes Graph.app and install it to /Applications

# MCP Server (Python)
./run mcp-test                 # Run Python tests

# UI Helper Daemon
./run ui-helper-start          # Run daemon in foreground (dev/debug)
./run ui-helper-install        # Install as LaunchAgent
./run ui-helper-uninstall      # Unload and remove LaunchAgent
./run ui-helper-status         # Check if daemon is running

# Worktree Setup
./run initialize               # Initialize a new worktree after creation
```

Worktree lifecycle (create/switch/remove) belongs to worktrunk (`wt`), wired up in
`.config/wt.toml`: `pre-start` runs `./run initialize`, `pre-merge` runs `./run test`,
`pre-remove` kills the tmux session.

### UI Helper Daemon Architecture
Write operations (create, append, replace) require Notes.app to be frontmost in the GUI session. When the MCP server runs remotely (SSH), it routes write operations through the UI helper daemon via Unix socket IPC:

```
[Remote MCP Server] → UIHelperClient → Unix Socket → UIHelperDaemon → ui_operations → Notes.app
[Local MCP Server]  → ui_operations → Notes.app (direct, no daemon needed)
```

- Detection: `_is_gui_session()` checks `SSH_CLIENT`/`SSH_CONNECTION` env vars
- Socket: `~/Library/Application Support/notes-graph/ui-helper.sock`
- Identity: the LaunchAgent runs through `AppleNotesMCP.app` (`--bridge-stdio`, `NOTES_GRAPH_PYTHON_ENTRY`), so the daemon uses that bundle's Full Disk Access and Accessibility grants. `./run ui-helper-install` needs `./run mcp-app-install` first
- Protocol: newline-delimited JSON, richtext as base64
- Timeouts (#143, #125): the client waits `ui_helper_client.WRITE_TIMEOUT` (300s) for every write, kept above one write's realistic worst case (one stalled 90s pasteboard read; `tests/test_ui_helper_timeouts.py` adds up the real constants, so raise it when a budget grows). It is not a hard bound: several stalled reads can outrun it. A client timeout (`UIHelperTimeoutError`) means the write may still complete, and every write tool says "re-read the note before retrying", never "install the daemon". A command waits at most `QUEUE_WAIT_TIMEOUT` (= `EDIT_LOCK_TIMEOUT`, 30s) behind the one the daemon is running, then is refused unstarted with `EditLockTimeoutError`. Ping uses `PING_TIMEOUT` (5s)

## CI

`.github/workflows/ci.yml` runs on PRs targeting `main`: a single `test` job on
`macos-26` (billed 10x — keep it lean) running `npm test`, then `uv run pytest -m "not macos_gui"`.
The JS tests run without the Electron binary (`ELECTRON_SKIP_BINARY_DOWNLOAD`), so a test that needs
Electron itself would fail there; pass Electron objects in instead. Both the runner
and the interpreter (`.python-version`, read by uv/setup-uv) are pinned: `macos-latest` rolled to
an image where uv picked CPython 3.15 and onnxruntime has no wheel (#163);
`tests/test_python_pin.py` guards both.
Tests that need a live GUI session + Apple Notes/TCC carry a module-level
`pytestmark = pytest.mark.macos_gui` and are excluded there; `tests/test_ci_markers.py`
enforces the marking. Fully mocked modules stay unmarked so CI sees them; `test_ui_scripting.py`
was marked by mistake until #143, and `test_ci_markers.CI_VISIBLE_MOCKED_MODULES` keeps it unmarked.
The full suite still runs locally via `./run mcp-test`.

## Project-Specific Notes

- **Unified logger (#147)**: system-fault errors (parse failures, the UIScriptingError family, unexpected exceptions) and daemon lifecycle events report best-effort to an optional self-hosted log collector via `src/apple_notes/unified_log.py` (the only importer of the vendored client in `src/apple_notes/_vendor/logger.py`). Reporting is off unless `LOGGER_URL` names a collector endpoint. User-input errors (note not found, encrypted, validation) are never logged. Long-lived processes deliver on a fire-and-forget thread; IPC scripts append to a local queue file (`LOGGER_QUEUE_FILE`) that any later `log()` call drains. Kill switch: `NOTES_GRAPH_UNIFIED_LOG=0`. Profiler hook in `profiling.profile()` is the central wire point; the daemon attaches real exceptions via `profiling.attach_error`.
- **Table reads walk the object graph (#235)**: a table's ZMERGEABLEDATA1 is a MergeableData object graph; `table_parser._grid_from_object_graph` places each cell from the `ICTable` root's `crRows` / `crColumns` (ordered uuid sets) and `cellColumns` (column uuid -> row uuid -> cell). Object order is storage order only and a paste reshuffles it, so never place cells by op order (the old reader did, and fresh tables came back reversed). Object order is only a fallback for a blob with no `ICTable` root; a root it can't walk raises `TableShapeError` (logged, read as `[table]`), and a grid over `MAX_TABLE_CELLS` (100,000) is `TableTooLarge`, refused without a fault report, since a shared note's table is someone else's bytes. `_TABLE_PIN` pins cell text (#240: an empty cell first in op order read every cell as empty), the key/type/uuid lists and the ordered sets' uuids as bytes.
- **Bullets are paragraph style_type 100, not field 2.3 (#235)**: Notes sets field 3 = 1 on most paragraphs (dashed, numbered, checklist items, a table's line, text after a table); reading it as a bullet added stray `* `. `extract_paragraph_styles` reports a stored 100 as `STYLE_BULLET_LIST` (104), matching the clipboard parser's `PARAGRAPH_TYPE_MAP`.
- **Protobuf decoders (#184)**: the text and paragraph/checklist uuid fields are pinned in the decoders, because short texts were mis-decoded.
- **Locked notes (#126)**: a password-protected note's `ZDATA` is a `bplist00` blob, not gzip — `protobuf_parser.parse()` raises `EncryptedNoteError` on that signature (the MCP tools' "encrypted" branches depend on it). On the AppleScript side Notes returns an *empty* body for a locked note with no error, so `ui_scripting._note_body_html` checks `password protected` itself and raises `EncryptedNoteError`; only Notes' own `Can’t get note …` output (see `applescript.is_note_not_found_output`) maps to `NoteNotFoundError` — never wrap a script in a blanket `on error error "Note not found"`.
- **Richtext decode pins field types (#198)**: `richtext/parser.py` decodes the clipboard protobuf with `PINNED_TYPEDEF`, not bare type guessing. In a repeated field, blackboxprotobuf lets the first entry fix the type, and ~1.4% of random 16-byte paragraph UUIDs parse as a message, which made those notes unparseable. When code starts reading another length-delimited field, add it to `PINNED_TYPEDEF` with the type it guesses to on normal data. Pinned entries show up in the returned typedef even when the note lacks that field, so `_ensure_complete_typedef` must merge subfields into an existing entry, not skip it. If the bytes don't fit the pins (e.g. non-UTF-8 in a string field), decoding retries with string pins relaxed to bytes, then plain guessing, and reports the degradation once via `unified_log.log_parse_error`.
- **The typed tag/table flow checks its Find (#122)**: `_find_and_select_text` copies the selection back (clipboard cleared first, read via the fresh-thread reader) and requires it to equal the placeholder exactly before the Delete that starts `_replace_with_hashtag` / `_replace_with_table`; on a miss Notes keeps the previous selection. A miss collapses the leftover selection (Left) and raises `PlaceholderNotFoundError` (a `PartialWriteError`: the paste landed, the literal placeholder stays); the check is the `find_check` span. Not Accessibility: `AXFocusedUIElement` on Notes took ~30s per query under load. Table rows are padded to `col_count`; tag spans never merge in `_consolidate_spans`. Refusals that say "Nothing was written" run before any paste on every path: any table not exactly 2 columns wide (`refuse_untypeable_actions`; one column #122, 3+ columns #260: Notes inserts a 2x2 and has no add-column shortcut on this macOS, so Option+Cmd+Right never took and the cells landed in the body beside an empty table; rows are added by Tab and work; replace and find_replace pre-check their markdown with `ui_operations._typed_actions`; the remote queue runs the same check at accept, `WriteApplier._refuse_untypeable`, #264), and a typed write into a note that already holds a literal placeholder (`_refuse_stale_placeholder`, from the `before` body HTML; placeholders inside find_replace's own `old_text` are exempt, counted not stripped, so the placeholder can be replaced).
- The Electron app and MCP server share the `apple_notes` library for data access
- Note links use `[[Note Name]]` wiki-style syntax
- **A note link has two stored forms (#247)**: `applenotes:note/<uuid>?ownerIdentifier=…` (ours, older links) and `applenotes://showNote?identifier=<uuid>` (made by hand). Parse them only through `link_tokens.note_link_uuid`; a reader with its own pattern hides links (`test_link_tokens.py` guards this). A read of a link to a live note the folder scope hides shows `[[unknown link]]`, never its title.
- **`restore_state` always writes plain text explicitly (#248)**: with RTF alone, a later plain-text read is answered on demand by the restoring process and hangs for 90s if that process isn't serving the pasteboard, and `save_state` reads plain text before every write. An RTF saved with no text gets the text it holds.
- **`save_state` snapshots the whole clipboard (#125, NTG-020)**: every type of every pasteboard item (an image, Finder files), read on one fresh thread under `SNAPSHOT_TIMEOUT` (5s, span `clip_read_items`) and `SNAPSHOT_MAX_BYTES` (256 MB, checked on the length before the copy). File-promise types are never read or restored. It is best effort: a snapshot that times out, is too large, is still blocked from an earlier timeout, or saw the clipboard change mid-save logs a warning (sizes only) and the write goes on with the RTF + text restore, so it never fails a write; any other error also goes to the unified logger. `restore_state` skips the restore when `changeCount` is unchanged (the write never touched the clipboard), otherwise writes the items back with `writeObjects_`; the first item always gets an explicit string when it holds text or a type AppKit converts to text (RTF, HTML, RTFD, webarchive; the #248 rule), never for an image alone.
- **Every full-note paste re-creates every link in the note**: Notes mints new attachment rows and keeps the old ones with `ZMARKEDFORDELETION = 1` until purge. So link queries must skip soft-deleted rows (`get_raw_note_links` does, #183), and per-note link reads should pair the ZDATA attachment runs to rows by identifier (`get_note_link_targets`)
- Graph relationships are based on internal note links, not folder hierarchy
- **`[[links]]` are synthesized, never typed (#212)**: the Cmd+K picker dropped links, picked fuzzy matches and left its sheet open (#185), so it is gone. Every write path (`add_note`, append, replace, find_replace, `edit_note_section`) pastes each link as U+FFFC plus a field-6 link attachment built by `richtext/section_fragment.py` (`markdown_to_richtext_linked`, `synthesize_pending_links`) from a `LinkTarget` (uuid, live title, ownerIdentifier) that `write_service.link_targets` resolves; a match with no owner id is refused before anything is written. The daemon wire carries `links` (a list of `{uuid, title, owner}`), plus the legacy `name_to_uuid` so a daemon still on older code keeps resolving links; a new daemon refuses an old client's payload (`name_to_uuid` with no `links`) instead of writing its links as text. `refuse_untypeable_actions` (hashtags and tables only) runs before create/append paste and inside `apply_pending_keyboard_actions`, which refocuses the note body once after any table: the table flow's closing Escape leaves focus in the table, so the verify copy-back's Cmd+A copied one cell and reported a landed table write as partial (#222). A placeholder may span runs (a plain find_replace replacement inherits the replaced text's runs). Links are confirmed by target uuid in the editor copy-back (`write_verify.unconfirmed_links`, positional via `richtext.operations.link_targets_from`, so an append only counts links in its appended lines; `add_note` runs its own copy-back only when the payload has links): a miss is partial (`PartialWriteError` / `LinksNotConfirmedError`). Editor, not NoteStore: NoteStore saves lazily.
