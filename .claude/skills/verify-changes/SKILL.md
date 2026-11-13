---
name: verify-changes
description: >
  Verification toolkit for the Apple Notes MCP server (via apple-notes-dev). A decision matrix
  maps the shape of the diff to the relevant phases — pick the minimum set that exercises the
  change's real risk surface. Not a fixed sequence.
  USE WHEN "verify-changes", "verify changes", "test mcp", "verify mcp", "test apple notes", "mcp smoke test"
user_invokable: true
---

# Verify Changes — Apple Notes MCP Server

> **Scope.** This is the maintainer's procedure for their own machine. Phases B to D run
> against a real Apple Notes library through the `apple-notes-dev` server in `.mcp.json`, and
> assume scratch notes the maintainer keeps for testing (a formatting-test note per feature,
> named `Test note format <Feature>`). It is not reproducible on a fresh clone. On a fresh
> clone, Phase A plus the read checks in Phase B against the demo library (`./run demo-mcp`,
> which is read-only) are what can be run; substitute demo note names such as
> `Weather station`.

This is a **toolkit**, not a script. PRs vary — a folder-filtering tweak doesn't need a write
round-trip; a richtext-conversion change doesn't need pagination smoke. Pick the phases that
match what changed; over-running everything every time is slow and dilutes signal.

The two parts that ARE fixed: always run the cheap floor first (Phase A), and always clean up +
report at the end. Everything between is a menu.

All behavioral phases drive the `apple-notes-dev` MCP server against the real Apple Notes
database — that's the point: the pytest floor is mostly mocked and never opens the live DB or
drives UI scripting.

## What changed → recommended phases

| Diff shape | Run |
| --- | --- |
| Read-path (`database.py` queries, list/search/graph/read, folder filtering) | A + B |
| Tool signature / params / validation (`server.py` tool defs) | A + B (smoke the changed tool) + C's validation checks |
| Write-path (add/update/delete, `find_replace`, `ui_operations`, `clipboard`) | A + C (+ D if formatting can be affected) |
| Richtext / formatting (markdown↔richtext, colors, tables, links, hashtags, `protobuf_parser`) | A + D (round-trip) + manual Notes visual check |
| Config / folder filtering (`exclude_folders`, `include_folders`, `include_all`) | A + B with `include_all` on/off |
| Daemon / remote write path (`ui_helper_daemon`, `ui_helper_client`) | A (its pytest suite) + C through the daemon if reachable |
| Keystroke guards (`_frontmost_recheck`, `_guarded_system_events`, dialog / frontmost checks) | A + C1/C2 + C4 (a `[[link]]` + `{#tag}` write) + C6 |
| Electron main process / packaging (`src/main.js`, `src/preload.js`, `package.json` `build`, `./run ui-app-install`) | A + the run-ui skill for dev, plus the installed app launched with `open -a … --args --remote-debugging-port=<port>` and driven over CDP. **Never launch the packaged binary from a terminal**: the terminal's Full Disk Access hides a missing grant. Write checks on scratch notes only |
| Docs / comments / skill files / pure rename | A only (or none) |

Pick the **minimum** set. If you find yourself running C + D for a read-only filtering change,
you've gone wrong.

## Mode argument

- (default) → use the matrix above to pick phases for the diff
- `quick` → A + B only (read-only, ~30s)
- `full` → A + B + C + D (write ops + format round-trip; mutates real Notes, needs cleanup)
- Explicit list like `B D` → run only those phases

Track which notes you create so you can clean them up.

---

## Phase A — Floor (always)

Cheap and mostly mocked. If it fails, stop and report — don't proceed to the behavioral phases.

```bash
./run mcp-test    # pytest — catches regressions in parsing / markdown↔richtext / query logic
```

The pytest suite does NOT open the live Notes database or exercise UI scripting, so it can't
catch errors that only surface against real notes or the write path. That's what Phases B–D are
for.

## Phase B — Read-only MCP smoke (apple-notes-dev)

Read-only, safe, ~30s. Run each check, verify assertions, track pass/fail. Use the
`apple-notes-dev` MCP server.

**B1 — list_notes (basic).** `list_notes()` → list (not string), length > 0; first item has
`name` + `folder`, NOT `uuid`.

**B2 — list_notes (fields).** `list_notes(fields=["name", "folder", "uuid"])` → first item has
`uuid`.

**B3 — list_notes (pagination).** `list_notes(limit=2)` → dict with `notes`, `total`, `limit`,
`offset`; `limit == 2`, `offset == 0`; `notes` is a list of length ≤ 2.

**B4 — search_notes.** `search_notes(query="<a word in one of your titles>")` → list, length > 0; first item has `name`,
`folder`, `match_type`.

**B5 — get_note_graph.** `get_note_graph(format="links_only")` → dict with `links` + `stats`.
Always use `format="links_only"` — the default returns every note (huge token cost).

**B6 — read_note_content.** Using the `name` from B1, `read_note_content(name=...)` → dict with
`name`, `folder`, `content`.

**B7 — find format test note.** `search_notes(query="Test note format")` → list, length > 0; at
least one result name starts with "Test note format". Each worktree/feature should have a
companion note named `Test note format <Feature>`. Save the first match for B8.

**B8 — read + assert formatting.** `read_note_content(name=<B7>)` → dict with non-empty
`content` (> 10 chars). Then feature-specific assertions by note name:
- name contains **"Color"** → ≥1 `{color:` tag + ≥1 `{/color}`; colors are valid Apple Notes
  highlights (blue, green, orange, pink, purple, or hex like `#FF3B30`).
- name contains **"Table"** → ≥1 markdown table row (`| ... |`) + a separator row (`| --- |`).

**Config variant** — when folder filtering changed: re-run B1/B4 with `include_all=true` and
confirm notes from excluded folders appear only when `include_all` is set.

## Phase C — Write-op exercise (mutates real Notes)

Creates/modifies real Apple Notes → **clean up afterward**, and some assertions need a manual
look in Apple Notes.app. The validation checks (C3) are non-mutating — they error before
touching anything, so they're safe to run on their own.

**C1 — add_note.** `add_note(name="MCP Test Note", body="Test content from smoke test.")` →
dict with `success`, `note_name`, `folder`, `backup_path`.

**C2 — update_note_content (find_replace).**
```
update_note_content(name="MCP Test Note", old_text="Test content",
                    body="Edited content", operation="find_replace")
```
→ `success: True`, `operation: "find_replace"`. Re-read the note to confirm "Edited content"
applied.

**C3 — validation errors (non-mutating).**
- `update_note_content(name="MCP Test Note", body="x", operation="find_replace")` (no
  `old_text`) → error contains **"old_text is required"**.
- `update_note_content(name="MCP Test Note", body="x", operation="append", old_text="foo")` →
  error contains **"only valid for find_replace"**.
- `update_note_content(name="MCP Test Note", old_text="NONEXISTENT_XYZ", body="x",
  operation="find_replace")` → error contains **"not found"**.

**C4 — links & tags.**
```
add_note(name="MCP Link Test",
         body="Visit [Google](https://www.google.com)\n\nSee [[MCP Test Note]]\n\nTagged #MCPTest")
```
→ success. Verify in Apple Notes: hyperlink clickable, wiki link resolves, hashtag renders as a
tag.

**C5 — color write** (valid highlights only: blue, green, orange, pink, purple).
```
add_note(name="MCP Color Test",
         body="This has {color:pink}pink text{/color} and {color:blue}blue text{/color} and {color:green}green text{/color}.")
```
→ `success: True`. Open "MCP Color Test" in Apple Notes and confirm the three colors render.

**C6 — open-dialog guard (#199).** Run from a fresh `uv run python` process calling the stdio
tool functions (`apple_notes_mcp.server.*`) — the apple-notes-dev MCP runs session-start code.
Open a harmless sheet on a scratch note (`click menu item "Print…" of menu "File" of menu bar 1
of process "Notes"`), assert `ui_scripting.notes_modal_title()` is truthy and a `find_replace`
raises "a dialog is open in Notes" at the first step; close it with Escape **only while Notes is
frontmost** (its buttons are out-of-process, not AX-visible); assert the probe is None and the
write lands. For an alert window: `tell application "Notes" to display dialog "x" giving up after
15` in a background subprocess. Pick `old_text` that is in the DB already: NoteStore saves
lazily, so text from a write seconds earlier is not yet visible to the DB-based guard.

## Phase D — Format round-trip (formatting survives a write)

Verify feature formatting survives the `find_replace` binary round-trip. This is the class of
check that catches a write path silently dropping colors / tables / links.

1. `read_note_content(name="Test note format <Feature>")` (your colour-test scratch note) — save content, count
   `{color:` tags (`before`).
2. find/replace a **plain-text** word (one NOT inside a `{color:}` tag):
   ```
   update_note_content(name="Test note format <Feature>",
                       old_text="<word>", body="<word> [verified]", operation="find_replace")
   ```
   → `success: True`.
3. Re-read; count `{color:` tags (`after`). Assert `after == before` (colors survived) and
   "[verified]" is present.

For table notes, run the same round-trip and confirm the `| --- |` separator + cells survive.
`find_replace` deliberately rejects `old_text` that lives inside a table cell (see
`server.py:789-798`), so pick a word outside any table.

## Phase E — Audit-exposed checks (deep audit #109)

Run when the diff touches `markdown_converter.py`, `protobuf_parser.py`, or the write path.
These are the concrete regressions the #109 audit found; add the matching check when you touch
that surface.

**E1 — read-path bold+italic (NTG-010).** Font style field 5 is a *bitmask* (1=bold, 2=italic,
3=both), not an enum. Non-behavioral live check (no GUI needed): scan real notes for a run with
field `"5" == 3` and assert `extract_text_formatting` reports `is_bold and is_italic`. Pre-fix it
reported neither, silently dropping the formatting on every read→edit→write. Covered by
`tests/test_protobuf_parser.py::TestExtractTextFormattingFontStyleBitmask`.

**E2 — inline-code round-trip (NTG-008).** Write a note whose body line is
`` `first` and `second` `` (two separate inline-code spans on one line). Read it back and assert
both words survive as text — the line must NOT collapse into one monospace paragraph with literal
interior backticks. This is the `add_note` behavioral form of the fixed greedy `^`(.+)`$` regex.

**E3 — adjacent duplicate attachments (NTG-017, KNOWN LIMITATION — do not "fix" naively).**
`[[Note]][[Note]]` or `{#tag}{#tag}` back-to-back: the converter merges the duplicate spans and
silently drops one. Un-merging them in `_consolidate_spans` alone is WRONG — it produces
back-to-back keyboard-action placeholders (`{__NG_LINK_1__}`) that the Cmd+F pending-action flow
can't resolve, leaving literal placeholder text in the note (verified worse than the drop, #109).
Any real fix needs adjacency-safe pending actions. Assert only that a write leaves **no**
`NG_LINK`/`NG_TAG` residue in the read-back content.
**Update (#122, 2026-11-09):** links are synthesized since #212 (no Find), and tag spans no
longer merge: `{#a}{#a}` was written live as two hashtag attachments with no residue. So for a
tag change, write `{#x}{#x}` on a scratch note and assert two
`com.apple.notes.inlinetextattachment.hashtag` attachments.

**E4 — Find-miss guard + table shape (#122).** Run from a fresh `uv run python` process (the
dev MCP runs session-start code). (a) Find miss: open a scratch note, select real text with
Cmd+F (e.g. "alpha"), then `apply_pending_keyboard_actions([<tag action with a placeholder not in
the note>])` → `PlaceholderNotFoundError`, and the note reads back unchanged. (b) Ragged table:
append `| a | b |\n|---|---|\n| 1 |\n| x | y |` → read back `| 1 |  |` / `| x | y |` (main
shifted it to `| 1 | x |` / `| y |  |`). (c) One-column and 3+ column tables are refused with "Nothing was
written" and the note unchanged (#260: only 2-column tables can be typed; rows any number).

**Duplicate-title / Recently-Deleted gotcha (NTG-005).** When cleaning up test notes, name-only
deletion can bind a Recently-Deleted or duplicate-titled copy and report "not found" for the live
one (hit live during #109 verification). Clean up by iterating `every note whose name = X` and
skipping notes whose container is "Recently Deleted", not `first note whose name`.

## Always — cleanup + report

**Cleanup.** Delete the notes you created (or tell the user to): "MCP Test Note",
"MCP Link Test", "MCP Color Test". Undo the find/replace in "Test note format <Feature>" (Cmd+Z in Apple Notes) to restore it.

**Report.** Adapt the shape to which phases ran — don't report on phases you skipped (it's
misleading). Example for an A + B + C(validation) run:

```
Verify Changes: PASSED
  Phase A — floor:       ./run mcp-test pass
  Phase B — read smoke:  8/8 (list/fields/pagination, search, graph, read, format note)
  Phase C — validation:  3/3 (old_text required / only-valid-for-find_replace / not-found)
  Skipped: C write ops, D round-trip (no write-path change in this diff)
```

If any check fails: report which + the failing assertion and the actual value received, but
**don't block the save**.

---

## Bugs this skill was shaped by

Specific historical bugs each phase exists to catch. New findings belong here so the skill
evolves with the codebase.

- **Find miss erased note text (#122, 2026-11-09)**: a missed Cmd+F keeps the previous
  selection, and the tag/table flow's first key is Delete. Now checked by copying the
  selection back. The Accessibility route (`AXFocusedUIElement`) worked once, then took ~30s per
  query under load, which only a live run showed. **E4** exists for this. The same run found that
  3+ column tables were broken on main; since #260 they are refused before the paste.

- **open-dialog guard vs the write's own popups (#199, 2026-10-31)** — the new per-key dialog
  check refused the keys `_replace_with_wiki_link` sends into Notes' own Cmd+K Add Link sheet
  (caught by Codex review, confirmed live) AND the Space after a typed `#tag`, which goes to
  Notes' tag-suggestion popup, a separate `AXSystemDialog` window (caught only by a live C4-style
  write; timing-dependent, one of two runs passed). The mocked suite was blind to both. Why the
  matrix row for keystroke guards requires a live `[[link]]` + `{#tag}` write, and why C6 exists.
- **edit_lines removal left the recipe stale (#81, 2026-03-30)** — `update_note_content` dropped
  the `edit_lines` operation (and `start_line`/`end_line`) in favor of `find_replace` +
  `old_text`, but this skill's write checks still called `edit_lines`. They would have errored
  on every run. **Caught while adopting the verify-changes convention (#92)** and rewritten into
  Phase C's find_replace checks. Lesson: when an MCP operation's signature changes, update the
  write-op phase in the same PR.
- **edit_lines broke external links in untouched sections (#56, 2026-03-01)** — an edit to one
  section corrupted external links elsewhere in the note. This is the formatting-survival class
  **Phase D's round-trip guards** — a write must leave untouched formatting/links intact.
- **color highlighting (#52 / #63, 2026-03-06)** — why **C5 (color write) + Phase D
  (round-trip)** exist: `{color:}` tags must both write correctly and survive a later
  find_replace.
- **table support + format-safe find_replace + CRDT fix (#72, 2026-03-26)** — native table
  writing plus a find_replace that preserves table attachments. **B8's table assertions + Phase
  D** guard rendering; the find_replace table-cell guard (`server.py:789-798`) is why D says to
  pick a word outside any table.
- **deep audit read/convert regressions (#109, 2026-07-12)** — seeded **Phase E**. A whole-repo
  audit (86 confirmed findings) fixed three small ones that this recipe missed: greedy whole-line
  monospace regex swallowing separate inline-code spans (**E2**), the read-path font-style enum-vs-
  bitmask bug dropping bold+italic (**E1**), and an AppleScript-injection escaping gap in the
  Electron open-note handler (unit-tested, `src/applescript-escape.test.js`, vitest). The NTG-017 duplicate-attachment merge was
  **reverted** after Phase E caught the placeholder-residue regression (**E3**). Lesson: the pytest
  floor is mostly mocked — read-path parser and markdown-converter bugs only surface when you feed
  the converter/parser adversarial input (spaced asterisks, twin links, interior backticks).
