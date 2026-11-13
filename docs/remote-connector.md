# Remote connector (claude.ai custom connector)

Remote mode serves the
Apple Notes MCP tools over **Streamable HTTP at `/mcp`**, protected by **OAuth that only
your GitHub account can pass**, so cloud Claude sessions (claude.ai, Claude Duet) can
use them through a public tunnel.

It is a separate entry point, `apple-notes-mcp-remote`. The stdio server
(`apple-notes-mcp`, used by local clients such as Claude Code and Claude Desktop) is
unchanged.

```
claude.ai (Anthropic cloud)
   │ HTTPS
   ▼
tunnel: Cloudflare named tunnel  or  Tailscale Funnel      ← the only public part
   │ http://127.0.0.1:9893
   ▼
launchd → AppleNotesMCP.app (AppleNotesMCPRunner, holds FDA) → apple-notes-mcp-remote
   ├─ /mcp                      MCP, bearer token required (401 + WWW-Authenticate otherwise)
   ├─ /.well-known/oauth-*      protected-resource + authorization-server metadata
   ├─ /register /authorize /token /revoke   OAuth (DCR, PKCE S256, rotating refresh tokens)
   ├─ /oauth/consent            our consent page (names the client; CSRF + SameSite cookie)
   └─ /oauth/github/callback    GitHub sign-in lands here; only ONE identity gets a code
```

## What is exposed

| Category | Tools | Remote |
|---|---|---|
| Read | `list_notes`, `list_folders`, `read_note_content`, `get_backlinks`, `search_notes`, `get_note_graph`, `find_orphans`, `find_broken_links`, `note_health_check`, `suggest_links` | **on** (annotated `readOnlyHint: true`) |
| Write | `add_note`, `update_note_content`, `toggle_checklist_item`, `replace_note_link`, `edit_note_section`, `delete_note`, `move_note` | off; opt-in via `write_tools` (annotated `destructiveHint: true`, except `move_note`: `destructiveHint: false`). Remotely they are **queued** (see below) |
| Remote-only | `get_write_status` | present only when a write tool is enabled (read-only) |
| Never | `export_note`, `export_folder`, `export_graph` | **cannot be enabled** |
| Reminders (read) | `reminders_lists`, `reminders_list` | off; on with `"reminders": {"enabled": true}` (annotated `readOnlyHint: true`; see [Reminders](#reminders-issue-171)) |
| Reminders (write) | `reminders_complete`, `reminders_create`, `reminders_update` | off; opt-in via `reminders.write_tools` (`reminders_complete` and `reminders_update` annotated `destructiveHint: true`, `reminders_create` `destructiveHint: false`). Applied **directly** through EventKit, not queued |

`export_*` write files to a caller-chosen path, so a prompt-injected agent could plant a
`CLAUDE.md` or slash command in a repo or `~/.claude`. The allowlist
lives in `src/apple_notes_mcp/remote/tools.py`; a new tool that is not categorized there
stops the remote server from starting.

**Recently Deleted is never served remotely**, whatever the arguments
(`include_all`, a note name, a pk or uuid, links, the graph). Your config-file
exclusions (`exclude_folders`) apply by default and a caller may still override them
with `include_all` / `include_folders`: they're a convenience filter, not a security
boundary. If you want a hard boundary, use `folder_allowlist`.

**Folder allowlist.** `folder_allowlist` confines every read to the named folders and their
subfolders (exact folder path, case-insensitive: `"Daily"` allows `Daily` and `Daily/Old`,
not `Daily Archive`). It is enforced inside `NotesDatabase`, so no tool argument
(`include_all`, a note name, a pk) gets around it. Reading is the main leak risk if Duet
is prompt-injected through an email, so set this.

**Result size.** Claude rejects results over ~150k characters. A larger result is refused
with an error that tells Claude to narrow the request (folder filter, query, or
`get_note_graph` with `start_note` + `depth`) rather than being sent truncated.

### Write tools (queued, issue #169)

Every write tool drives Notes.app by **UI scripting**: it brings Notes to the front,
types keystrokes, uses the clipboard (saved and restored) and needs an unlocked screen.
So remotely, a write call doesn't type anything. It is **accepted into a write queue and
returns at once**:

```json
{"status": "queued", "write_id": "w_3f9c…", "queued_ahead": 0,
 "applies": "now (Mac unlocked)" | "1 min after the Mac is next unlocked"}
```

The remote server's drain thread then applies it through **the same pipeline as a local
write**: backup, markdown conversion, the write-guard overlay with its lead countdown,
the frontmost guards, verify-after-write.

- **Accepted while the Mac is unlocked** → applied right away, like a local Claude Code
  write.
- **Accepted while it is locked** (for example, from a scheduled run) → applied 1 minute after you
  next unlock.
- No idle gate: the overlay + countdown is the warning when you're at the Mac.
- Strictly first in, first out. Afterwards the app you were in is brought back to the
  front.

**What the queue checks.**
- At accept, the stdio tool's own validation runs, plus the caps (below). The note
  title must match exactly **one** note. The match is case-insensitive and includes
  Recently Deleted, because the UI path opens notes by title.
- At apply, it re-checks, and a mismatch becomes `conflict`, never a blind write:

  | Operation | Rule at apply |
  |---|---|
  | `add_note` | the title must still be free |
  | `append` | applies even if the note changed meanwhile |
  | `find_replace` | `old_text` must still match exactly once |
  | `replace` | only if the note is unchanged since it was queued |
  | `toggle_checklist_item` | stored as the desired checked state, so a queued toggle is idempotent: if you ticked it by hand, applying is a no-op |
  | `edit_note_section` | conflicts only if the section itself changed since it was queued (edits elsewhere in the note are fine); replace, find_replace and delete are refused for sections over 50 paragraphs, append is exempt |
  | `replace_note_link` | targets are resolved to uuids when queued; the link must still match (same count unless `replace_all`) |

- A write interrupted by a focus change is retried once, and only if the note is
  provably untouched. A write that may have half-landed is marked `failed` for you to
  check.

**Statuses** (`get_write_status`, `./run remote-writes list`): `queued`, `applying`,
`applied`, `conflict`, `failed`, `expired` (not applied within 48 h), `rejected` (you
cleared it), `undone`.

**Caps** (remote.json `write_queue`, defaults):

| Cap | Default |
|---|---|
| `max_body_chars` | 20,000 |
| `max_old_text_chars` | 2,000 |
| `max_per_day` | 100 |
| `max_pending` | 50 |
| `max_pending_per_note` | 10 |
| `pending_ttl_seconds` | 48 h |

`allowed_operations` (default: all of `add_note`, `append`, `find_replace`, `replace`,
`toggle_checklist_item`, `replace_note_link`, `edit_note_section`, `section_delete`) narrows what remote
callers may do.

**Bulk relinks** (`replace_note_link`, #183): one call re-points the links in **one** note,
and each queued write takes about 15 s to apply while the Mac is unlocked.

- There is no hourly cap (removed: bulk edits kept hitting it); `max_per_day` and
  `max_pending` bound volume. At the defaults (100/day, 50 pending) a 55-note migration
  needs `max_pending` raised, or two passes; each write still takes ~15 s unlocked.
- Undo (`./run remote-writes undo <id>`) is a reverse relink. It is refused when the
  note already linked the new target before the write, since a reverse relink would
  flip those links too. It is also refused when the old target note no longer exists.

**Delete and move** (`delete_note` #229, `move_note` #230): opt in with `"delete_note"` /
`"move_note"` in `write_tools` (operation names are the same, both on by default in
`allowed_operations`). Both are pure AppleScript, queued like the other writes, and guarded
by `apple_notes/note_target.py`, the same guards the stdio tools run:

- The title must match exactly one note (case-insensitive, Recently Deleted included) and
  equal it exactly. Zero or several matches are refused with the count and each match's
  folder and modified date. Notes in Recently Deleted and locked notes are refused.
- `delete_note` only ever moves the note to Recently Deleted (recoverable for 30 days),
  never a permanent delete. The queued response carries a `note` block (title, folder,
  modified, first ~200 characters) so the caller can confirm it hit the right note.
- `move_note` needs an existing folder (never created; not Recently Deleted or a smart
  folder), matched case-insensitively by full path, then folder name, then the name
  without its emoji prefix (`"Archive"` finds `🗄️ Archive`); more than one match in the
  deciding tier is refused. The folder is queued as its resolved full path.
- At apply time the guards run again: a note that went to Recently Deleted, gained a
  same-titled twin, got locked or was replaced by another note of that title is a
  `conflict`. Edits to the note's text in between don't block it.
- No undo: `./run remote-writes undo` refuses both (recover from Recently Deleted, or move
  the note back).
- Leave the claude.ai permission for both at **Needs approval** (Ask).

**Section edits** (`edit_note_section`, #184): opt in with `"edit_note_section"` in `write_tools`.
The two operation names are `edit_note_section` (replace, append, find_replace) and
`section_delete`; both are on by default in `allowed_operations`, so remove one to disallow it.

- Replace, find_replace and delete are refused remotely for sections over 50 paragraphs
  (append is exempt). A bigger edit has to run locally.
- Notes holding more than 25 MB of images and files are refused, at accept and at apply:
  every edit re-copies them. `update_note_content` append still works there.
- Undo (`./run remote-writes undo <id>`) covers text edits only (replace, append,
  find_replace). It never covers delete or sections containing tables or images, and a
  write is journaled for undo only when the post-write span is proven; otherwise the
  status says to undo by hand.
- The purge clears the stored section text after 7 days.

**Turning writes on (the owner):**

1. One-time grants at the console. `AppleNotesMCP.app` (the process the LaunchAgent runs)
   needs **Device Control and Data Access** (Accessibility on older macOS) and **Automation → Notes / System Events**. The prompts only
   appear on first use and can't be answered from a locked Mac. Trigger them once
   while you're at it, e.g. `./run remote-writes apply <id>` on a test write.
2. Add the tools to `remote.json`, optionally with a `write_queue` block:

   ```json
   "write_tools": ["add_note", "update_note_content", "toggle_checklist_item", "replace_note_link", "edit_note_section"],
   "write_queue": {"apply_delay_after_unlock_seconds": 60, "max_per_day": 100}
   ```

   Other keys: `state_dir` (default `~/Library/Application Support/notes-graph/write-queue/`,
   dir 700 / file 600: it holds write arguments), `auto_apply` (default true; false means
   only `./run remote-writes apply` applies), `allowed_operations`, and the caps above.
3. `./run remote-check`, then restart the server yourself
   (`launchctl kickstart -k gui/$(id -u)/com.notes-graph.mcp-remote`).
4. In claude.ai choose how the write tools are approved. **Needs approval** is the safer
   setting. An assistant that runs unattended has nobody to approve its writes, so it needs
   **Always allow**; the caps, conflict checks and undo are then the only guard. Set
   `get_write_status` to Always allow either way.
5. Keep the write-guard overlay on for the server: don't set
   `NOTES_GRAPH_WRITE_INDICATOR=0` or a `0` lead.

**Undo** (local only, never a remote tool): `./run remote-writes undo <write_id>`.
- It refuses if the note changed after the write.
- Otherwise it puts the journaled prior content back:
  - a `replace`, `append` or `find_replace` comes back through the markdown round
    trip (what `read_note_content` shows);
  - a toggle is toggled back;
  - an `add_note` is undone by deleting the note (to Recently Deleted).
- The journal is purged after 7 days. The per-write database backup is now a full SQLite
  online backup, WAL included (the old file copy could miss recent edits).

**Instructions for the assistant** (paste into its guidance for Notes):

> A Notes write through this connector is queued, not done. Don't resubmit it. Before
> relying on it - or on your next run - call `get_write_status`. If a write shows
> `conflict`, re-read the note and decide again; if `failed`, tell the owner.

`write_tools` still cannot be combined with `folder_allowlist` (the server refuses that
config): writes address a note by title, outside the folder scope.

## Reminders (issue #171)

Two read tools and two guarded write tools, through EventKit in the server process (no UI
scripting, so they work with the screen locked):

- `reminders_lists()`: every list, with `id`, `name` and `source` (account). Two lists can
  share a name.
- `reminders_list(list_name?, include_completed?, due_by?, include_notes?)`: open reminders by
  default, sorted by due date. `list_name` is a name (matches every list with it) or an id;
  `due_by` (YYYY-MM-DD) is inclusive and leaves out undated reminders; notes only on request,
  capped at 2000 characters.

- `reminders_complete(id, expected_due)`: marks one reminder complete and returns
  `{id, list, recurring, completed_due, next_due}`. A recurring reminder keeps its id, stays
  open and moves to its first occurrence after now (a date with no time counts as midnight,
  so an overdue daily one lands on tomorrow).
- `reminders_create(title, list, due?, notes?)`: adds one reminder, with no recurrence, alert
  or location. `list` is a name or, when two lists share it, an id.
- `reminders_update(id, expected_due, expected_title?, title?, due?, clear_due?, notes?,
  clear_notes?, list?, priority?)` (#232): edits one open reminder; only the fields passed
  change. `expected_due` (and optionally `expected_title`) is what the caller last read: a
  mismatch writes nothing. `list` moves it (a name or id, limited by `create_lists`);
  `priority` is `none`, `low`, `medium` or `high`. Returns
  `{id, list, list_id, changed: {field: {before, after}}}`. Due dates of **recurring**
  reminders are refused: EventKit keeps one reminder per series, so a new due date
  re-anchors the whole series (verified: a monthly reminder re-dated from the 5th
  to the 8th next came due on the 8th). Their title, notes, list and priority can change.
  **Alerts move with the due date** (the owner's review): each absolute alert shifts by the due
  date's wall-clock move (by whole days when either side is date-only), so one at the due
  time stays at it and one a day before stays a day before; relative alerts keep their
  offset, location alerts are untouched, and a start date equal to the old due date moves
  too. `clear_due` removes the alert at the due time (absolute at it, or relative offset 0)
  and refuses if another timed alert would be left. `changed.alerts` shows before/after.

Nothing can delete or uncomplete a reminder, change its recurrence or location, set its
alerts (they only move with its due date, as above), or create or change a list.
Every list is readable.

**Guards.** Each refusal writes nothing and names its cause and, where one applies, the
`remote.json` key, so a forgotten restriction is traceable from the error alone:

| Refused when | Controlled by |
|---|---|
| the list is in `never_complete_lists` (case-insensitive) | `reminders.never_complete_lists` |
| the reminder is already completed | (fixed) |
| `expected_due` differs from its current due date (it moved: completed already this pass) | (fixed; re-read it) |
| a recurring reminder already has a completed copy from today | (fixed) |
| `create` into a list outside `create_lists` | `reminders.create_lists` |
| `create` with a name two lists share | (fixed; pass the id) |
| `create` where an open reminder with the same title is already in that list | (fixed) |
| `update` that moves into a list outside `create_lists` | `reminders.create_lists` |
| `update` whose new title or list would duplicate an open reminder there | (fixed) |
| `update` of a completed reminder, with a stale `expected_due` / `expected_title`, or that changes nothing | (fixed; re-read it) |
| `update` of a recurring reminder's due date | (fixed; change it in Reminders) |
| `update` with `clear_due` on a reminder with a timed alert other than the one at its due time | (fixed; change it in Reminders) |
| `update` of the due date when EventKit can't read the reminder's alerts | (fixed; change it in Reminders) |
| `update` that moves a reminder out of a `never_complete_lists` list | `reminders.never_complete_lists` |
| `update` that moves a reminder into another account (its id or subtasks could change) | (fixed; move it in Reminders) |
| `update` that renames or moves a recurring reminder already completed today | (fixed; retry tomorrow) |
| title over 500 or notes over 2000 characters, or a bad `due` | (fixed) |
| the list is read-only in Reminders (a shared list) | Reminders sharing |

**Config.**

```json
"reminders": {
  "enabled": true,
  "never_complete_lists": ["Grocery List", "errands this week!"],
  "create_lists": null,
  "write_tools": ["reminders_complete", "reminders_create"]
}
```

`write_tools` here is separate from the top-level Notes `write_tools`; naming a Reminders
tool there is refused. With `reminders_complete` in it, `never_complete_lists` must be
present (`[]` allows every list). `create_lists: null` (or absent) means every list; `[]` is
refused. `remote-check` prints the write tools and both list settings.

**Access.** Under launchd, EventKit checks the Reminders grant of `AppleNotesMCP.app`, not of
your terminal, and the server never asks for access itself: without the grant, both tools
return an error naming the fix, and the Notes tools keep working.

### Rollout (the owner)

1. `./run mcp-app-install` (adds the Reminders usage strings and the calendars entitlement),
   then `./run mcp-app-spike` to confirm Full Disk Access survived the re-sign.
2. While the Mac is unlocked: `open ~/Applications/AppleNotesMCP.app --args --probe-reminders`
   and allow the prompt that names "Apple Notes MCP". If "Apple Notes MCP" is already listed
   under System Settings → Privacy & Security → Reminders, switching it on does the same.
3. Add `"reminders": {"enabled": true}` to `remote.json`.
4. `./run remote-check`: it should show `reminders: on, Apple Notes MCP.app access: granted`
   and list both tools.
5. `launchctl kickstart -k gui/$(id -u)/com.notes-graph.mcp-remote`.
6. In claude.ai, reconnect the connector so it picks up the new tools, and set both to
   **Always allow**.

`remote-check` asks the installed app for its status by launching it with `open`, so the
answer is the wrapper's grant, not the terminal's.

### Write rollout (the owner)

No wrapper rebuild or new grant: the full-access grant and the calendars entitlement from the
read rollout already cover writes. There is no shadow week.

1. **First**, turn off any other automation that completes Reminders. A path with no
   `expected_due` check can complete the same recurring reminder twice, which skips an
   occurrence. Until then, leave `reminders_complete` out of `write_tools`
   (`reminders_create` alone is safe).
2. Add `never_complete_lists`, `create_lists` and `write_tools` to the `reminders` section.
3. `./run remote-check`: it should list the write tools and both list settings.
4. `launchctl kickstart -k gui/$(id -u)/com.notes-graph.mcp-remote`.
5. In claude.ai, reconnect so the new tools appear, and set both write tools to **Needs
   approval**. Move `reminders_create` and then `reminders_complete` to **Always allow** once
   you trust them.

To add `reminders_update` (#232), append it to `reminders.write_tools`:
`"write_tools": ["reminders_complete", "reminders_create", "reminders_update"]`, then run
steps 3-5 again. It leaves the claude.ai permission at **Needs approval** until you decide.

## Library choice

The server already uses the official `mcp` SDK (`mcp>=1.30`, its built-in FastMCP), which
ships the whole OAuth authorization-server side: `/authorize`, `/token`, `/register`
(Dynamic Client Registration), `/revoke`, both metadata documents, PKCE S256 checking, the
bearer middleware and the RFC 9728 `WWW-Authenticate` header. What it leaves to us is the
provider: where tokens live and who may sign in. That provider is
`src/apple_notes_mcp/remote/auth.py` (~350 lines): it shows a consent page, redirects to
GitHub, exchanges the code, checks `/user` against one login AND numeric id, and issues
opaque, hashed, short-lived tokens.

**Consent page.** Because GitHub re-approves an already-authorized OAuth app silently,
anyone who registers a client and gets you to open their `/authorize` link could otherwise
receive a code (the MCP spec's "confused deputy" case for proxies). So `/authorize` first
shows our own page naming the registered client; Approve is a form POST whose CSRF value
must match a `SameSite=Strict` cookie that page set, and the page can't be framed.
Approving also sets a `SameSite=Lax` flow cookie that the GitHub callback requires, so an
approval made in someone else's browser can't be completed in yours. Only
approve right after clicking Connect in claude.ai yourself.

**Client pinning (trust on first use).** The first connector client that completes
sign-in is pinned. Any other client_id - for example someone who added your URL as a
connector in *their* claude.ai account and sent you the consent link - gets a refusal
page that shows its client_id, and no Approve button. If *you* re-added the connector
(Claude registers a new client each time), allow the id shown on that page with
`./run remote-allow-client <client_id>`, or start over with `./run remote-revoke-tokens`
(which also clears the pin). The consent page itself always shows the client_id.
Once a client is pinned, `/authorize` for any other client goes straight to that refusal
page without parking a sign-in, the pin is taken atomically (of two racing sign-ins exactly
one wins), and the token endpoint and every `/mcp` request also refuse unpinned clients.
Each client can have at most 5 sign-ins in flight.

**Registration and token hardening.** `/register` bodies are capped at 16 KB and
registrations are rate-limited (10 per minute); only the fields the flow uses are stored,
with length caps. At most 50 sign-ins can be in flight. Replaying a used authorization
code or a rotated refresh token revokes every token issued from that sign-in (token-family
reuse detection). `remote-revoke-tokens` also cancels sign-ins already in progress in the
running server, including one that is mid-way through the GitHub round trip.

The standalone `fastmcp` package has a ready GitHub OAuth proxy, but it is a different
server class: adopting it would mean either moving the stdio server onto it (risking
the "stdio stays exactly as today" rule) or running two MCP frameworks side by side, plus
a large new dependency on the read path to your notes. Staying on the SDK keeps one
framework and one small, fully tested provider.

## Setup (the owner)

### 1. GitHub OAuth app

GitHub → Settings → Developer settings → OAuth Apps → New OAuth App:

- Homepage URL: `https://<your public host>`
- Authorization callback URL: `https://<your public host>/oauth/github/callback`

Note the Client ID; generate a client secret. Find your numeric user id:
`gh api user -q .id`.

### 2. Secret (Keychain, never in the repo)

```bash
security add-generic-password -U -s notes-graph-mcp-remote -a github-client-secret \
  -T /usr/bin/security -w        # prompts for the secret; nothing lands in shell history
```

Fallback if the Keychain doesn't work under launchd: `~/Library/Application Support/notes-graph/remote-secrets.json`
containing `{"github_client_secret": "..."}`, `chmod 600` (the server refuses a looser mode).

### 3. Config

`~/Library/Application Support/notes-graph/remote.json`:

```json
{
  "public_url": "https://connector.example.com",
  "allowed_github_login": "octo-owner",
  "allowed_github_id": 12345678,
  "github_client_id": "Ov23li...",
  "folder_allowlist": ["Daily Notes", "★ Maps"],
  "write_tools": []
}
```

- `allowed_github_id` is required (logins can be renamed or re-registered; the id can't).
- `public_url`: scheme + host only (no `/mcp`). The server adds it to the DNS-rebinding
  allowed hosts, so the tunnel's Host header isn't rejected with "Invalid Host".
- Optional: `port` (default 9893), `state_dir` (default `…/notes-graph/remote/`), `log_path`
  (default `~/Library/Logs/apple-notes-mcp/requests.log`). The bind host is fixed at
  `127.0.0.1` and cannot be configured.

Check it: `./run remote-check` (prints the URL, identity, folders and exposed tools; never
the secret).

### 4. Keep the Mac awake

Use **System Settings → Battery/Energy → "Prevent automatic sleeping on power adapter when
the display is off"**. Not `caffeinate -s` inside the LaunchAgent: caffeinate would become
the launched (responsible) process and the Full Disk Access grant on AppleNotesMCP.app
would no longer apply. If the Mac sleeps, the connector just fails until it wakes.

### 5. LaunchAgent (prepared, not installed)

The server runs through the signed `AppleNotesMCP.app` wrapper, which keeps the Full Disk
Access grant: `AppleNotesMCPRunner --bridge-stdio` execs `NOTES_GRAPH_PYTHON_ENTRY`, which
the plist points at `.venv/bin/apple-notes-mcp-remote`.

```bash
./run mcp-app-install          # if the wrapper isn't installed/current (then grant FDA)
./run remote-plist             # renders dist/com.notes-graph.mcp-remote.plist
mkdir -p ~/Library/Logs/apple-notes-mcp
cp dist/com.notes-graph.mcp-remote.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.notes-graph.mcp-remote.plist
curl -s http://127.0.0.1:9893/.well-known/oauth-authorization-server   # should print JSON
```

Confirm FDA holds under launchd: after sign-in, a `list_folders` call from claude.ai must
return folders, not "database not found".

### 6. Tunnel (pick one)

**Cloudflare named tunnel** (needs a domain on Cloudflare)

- `cloudflared tunnel create notes-mcp`, then route a hostname under your domain to
  `http://127.0.0.1:9893`. It must have an IPv4 A record (Claude connectors are IPv4 only;
  a proxied Cloudflare hostname has one).
- Do **not** put Cloudflare Access in front; it blocks Claude.
- WAF: allow only Anthropic's egress `160.79.104.0/21` on `/mcp`, `/token`, `/register`,
  `/revoke` and `/.well-known/*`. Leave `/authorize`, `/oauth/consent` and
  `/oauth/github/callback` out of the IP rule (your browser uses them). Check bot-fight / WAF isn't returning 403/429 to
  Anthropic.
- Run cloudflared as its own LaunchAgent labelled `com.notes-graph.mcp-remote-tunnel` so
  the kill switch can stop it.

**Tailscale Funnel** (no domain needed)

- Needs HTTPS certificates enabled and the `funnel` node attribute in the tailnet policy.
  Funnel only serves ports 443, 8443 or 10000.
- `tailscale funnel --bg --https=443 http://127.0.0.1:9893`; the URL is
  `https://<machine>.<tailnet>.ts.net` - use that as `public_url`.
- Tradeoffs: just as public, the URL shows your machine and tailnet names, and there is no
  WAF, so OAuth is the only gate.

### 7. Public checks (after the tunnel is up)

From outside the LAN (e.g. phone hotspot):

```bash
curl -si https://<host>/mcp -X POST -H 'content-type: application/json' -d '{}' | head -5
#   → 401 with: WWW-Authenticate: Bearer ... resource_metadata="https://<host>/.well-known/oauth-protected-resource/mcp"
curl -s https://<host>/.well-known/oauth-authorization-server
npx @modelcontextprotocol/inspector --cli https://<host>/mcp --transport http --method tools/list
#   → 401 until you sign in; sign in with a second GitHub account and confirm "Access denied"
```

### 8. Add the connector in claude.ai

Customize → Connectors → **Add custom connector**:

- URL: `https://<host>/mcp`
- Authentication: **Sign in now**; OAuth client: **Register automatically** (DCR)
- Sign in with GitHub as `your-github-login`.

Tool permissions: all exposed tools are reads; set them to **Always allow** or **Needs
approval** as you prefer. If you later opt in write tools, set them to **Needs approval**.
`export_*` never appear; if they somehow did, set them to **Blocked**. Keep `delete_note` and
`move_note` at **Needs approval** if you opt them in.

## Operating it

| Task | Command |
|---|---|
| Start in the foreground (dev) | `./run remote-start` |
| Stop everything (tunnel + server) | `./run remote-kill` (turns Funnel off only if it proxies to 127.0.0.1:9893) |
| Revoke every token | `./run remote-revoke-tokens` (takes effect on the running server immediately, clears the client pin and in-flight sign-ins; Claude must sign in again) |
| Allow a re-added connector | `./run remote-allow-client <client_id>` (the id is on the refusal page) |
| Validate config | `./run remote-check` |
| Remote write queue | `./run remote-writes list \| apply <id>\|--all \| undo <id> \| pause \| resume \| clear` |
| Render the LaunchAgent | `./run remote-plist` |

**Rotate the GitHub secret:** generate a new one on the OAuth app, re-run the
`security add-generic-password -U …` command, restart the server
(`launchctl kickstart -k gui/$(id -u)/com.notes-graph.mcp-remote`), then delete the old
secret on GitHub. Our own tokens are random per issue (no signing key), so there is
nothing else to rotate; `remote-revoke-tokens` invalidates all of them.

**Logs** (`~/Library/Logs/apple-notes-mcp/`):

- `requests.log`: one JSON line per event: `http` (method, path without query string,
  status, ms), `tool` (tool name, allow/deny, outcome, result size, GitHub login),
  `consent` / `sign_in` / `token` / `register` / `revoke` (decision + reason). Never arguments, note
  contents, headers, codes or tokens.
- `requests.log` is capped at 5 MB with one rotated copy (`requests.log.1`); logged paths
  are truncated to 200 characters.
- `server.log`: stdout/stderr from launchd, WARNING and above only. Records from the MCP
  SDK, uvicorn and the notes library are redacted to logger + level + exception type, so
  note titles or contents in exception messages never reach it.
- uvicorn runs with `proxy_headers=False`: `X-Forwarded-*` from the tunnel is ignored
  (the issuer and callback URLs come from config).
- Optional log collector (issue #170, `remote/logger_events.py`): when `LOGGER_URL` is set,
  the server reports faults to that endpoint as app `notes-graph`, component `remote`.
  Stable messages, metadata only: `remote config or secret failed to load`
  and `remote server exited with an error` (exception type + phase; at most one per
  hour each across launchd restarts, stamped in
  `~/Library/Application Support/notes-graph/remote-logger-stamps.json`),
  `remote server: unhandled exception` (logger + level + exception type, once per
  10 minutes per pair; user-input errors skipped), `GitHub token exchange failing
  repeatedly` (3 failed sign-ins in a row, at most once an hour; `bad_verification_code`
  never counts: exception type, HTTP status, GitHub's error code such as
  `incorrect_client_credentials`). Tool failures keep the stdio reporting path but carry
  only the tool name and exception type in remote mode, in the server and in
  `./run remote-writes apply|undo`, plus a reason code when the message is a known,
  content-free one (issue #255: `db_unable_to_open`, `db_locked`,
  `pasteboard_read_timeout`, …, chosen from a fixed list in
  `apple_notes/unified_log.py`, or SQLite's own result-code name such as `sqlite_busy`
  for other sqlite3 errors; anything else stays type-only). Never note titles
  or contents, tokens, codes or the client secret. Kill switch:
  `NOTES_GRAPH_UNIFIED_LOG=0` in the LaunchAgent environment. The tunnel is not
  monitored: no traffic looks the same as the owner simply not using the connector.

**State:** `~/Library/Application Support/notes-graph/remote/tokens.json` (dir 700, file
600) holds only SHA-256 hashes of codes and tokens, plus registered client metadata (at
most 25; registration is open, so the oldest clients WITHOUT live tokens are evicted, and
registration is refused if all 25 hold live grants - `revoke-all` resets that). Writes take an `flock`
shared with the `revoke-all` CLI, so a concurrent save can't resurrect revoked tokens.

Token lifetimes: access token 1 hour; refresh token 30 days, rotated on every use (the
old refresh token and its access token die immediately).
