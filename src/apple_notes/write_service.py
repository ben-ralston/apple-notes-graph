"""Shared write chains: append (issue #117), delete (#229) and move (#230).

One chain for every caller that appends markdown to a note - the MCP server's
update_note_content(append) and the Electron add-link script both route here,
so backup, link resolution, local/daemon routing, and the UI-scripting safety
stack (edit_lock, write indicator, verify-after-write - inside
execute_append_to_note) can never fork.
"""

import os
import re
import unicodedata
from collections.abc import Mapping
from typing import Callable, ContextManager, Optional

from .backup import BackupManager
from .database import NotesDatabase
from .exceptions import AppleScriptError
from .icloud_sync import nudge_icloud_sync
from .models import TitleMatch
from .note_content import NoteContentReader
from .note_target import coredata_id, read_preview, resolve_folder, resolve_unique_note
from .profiling import span
from .richtext.section_fragment import (
    FragmentError,
    LinkTarget,
    markdown_to_richtext_new_linked,
)
from .ui_helper_client import UIHelperClient
from .ui_operations import (
    execute_append_to_note,
    execute_delete_note,
    execute_move_note,
)


def is_gui_session() -> bool:
    """True when running in a macOS GUI session (not SSH/remote).

    Set NOTES_GRAPH_USE_DAEMON=1 to force daemon routing from any context
    (e.g., cron, launchd, Automator).
    """
    if os.environ.get("NOTES_GRAPH_USE_DAEMON"):
        return False
    if os.environ.get("SSH_CLIENT") or os.environ.get("SSH_CONNECTION"):
        return False
    return True


def _link_key(name: str) -> str:
    """How [[link]] titles are compared: case-insensitively, with composed and
    decomposed accents equal (as find_title_matches compares them, #83)."""
    return unicodedata.normalize("NFC", name).casefold()


class SharedTitle(tuple):
    """A ``name_to_uuid`` value for a title several live notes carry (#83):
    the notes themselves, so a refused [[link]] can name each one."""


def build_name_to_uuid(db_path: Optional[str] = None) -> dict:
    """Case-insensitive note name -> UUID map (for [[link]] resolution).

    A title more than one live note carries (case-insensitively) maps to a
    SharedTitle instead of a uuid, so link_targets refuses it rather than
    silently linking whichever note came last (#83).
    """
    with NotesDatabase(db_path) as db:
        notes = db.get_all_notes()
    by_name: dict[str, list] = {}
    for n in notes:
        if n.uuid and n.name:
            by_name.setdefault(_link_key(n.name), []).append(n)
    return {
        key: group[0].uuid if len(group) == 1 else SharedTitle(group)
        for key, group in by_name.items()
    }


def _shared_title_error(link_name: str, notes: SharedTitle) -> FragmentError:
    listing = "\n".join(
        f"- '{n.name}' in folder '{n.folder}', id {n.uuid}, "
        f"modified {n.modified or 'unknown'}"
        for n in sorted(notes, key=lambda n: n.pk)
    )
    return FragmentError(
        f"Can't link [[{link_name}]]: {len(notes)} notes are titled that "
        "(titles match case-insensitively), so the link target is ambiguous. "
        "Rename one first, or leave the link out. Matches:\n" + listing
    )


_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def link_targets(
    markdown: Optional[str],
    *,
    db_path: Optional[str] = None,
    name_to_uuid: Optional[Callable[[], dict]] = None,
    open_db: Optional[Callable[[], ContextManager]] = None,
) -> dict[str, LinkTarget]:
    """Live-note targets for every [[Name]] in `markdown`, keyed by lower-case title.

    What a synthesized link needs (issue #212): the uuid, the live title (the
    link's alt text) and the ownerIdentifier. A name that matches no live note
    stays plain text, as the converter treats unresolved links; a match with
    no owner id can't be linked and raises FragmentError. The database is
    opened only when a name matches. `name_to_uuid` / `open_db` are injection
    points for the MCP server (its own map, an already-open database).
    """
    names = sorted({m.group(1) for m in _WIKI_LINK_RE.finditer(markdown or "")})
    if not names:
        return {}
    mapping = name_to_uuid() if name_to_uuid else build_name_to_uuid(db_path)
    wanted: dict[str, tuple[str, str]] = {}
    for link_name in names:
        uuid = mapping.get(_link_key(link_name))
        if isinstance(uuid, SharedTitle):
            raise _shared_title_error(link_name, uuid)
        if uuid is not None:
            wanted.setdefault(link_name.lower(), (link_name, uuid))
    if not wanted:
        return {}
    links: dict[str, LinkTarget] = {}
    with open_db() if open_db else NotesDatabase(db_path) as db:
        for key, (link_name, uuid) in wanted.items():
            owner = db.link_owner_identifier(uuid)
            if not owner:
                raise FragmentError(
                    f"Can't create a link to '{link_name}': its account owner id "
                    "wasn't found."
                )
            live = db.get_note_by_uuid(uuid)
            links[key] = LinkTarget(uuid, live.name if live else link_name, owner)
    return links


def append_markdown_to_note(
    name: str,
    body: str,
    *,
    db_path: Optional[str] = None,
    links: Optional[Mapping[str, LinkTarget]] = None,
    skip_backup: bool = False,
) -> dict:
    """Append markdown to a note through the full write-safety chain.

    [[links]] are pasted as synthesized link attachments (issue #212), never
    typed through the Cmd+K picker. skip_backup/links are injection points for
    the MCP server, which backs up and resolves link targets once for all
    three update operations.
    """
    backup_path: Optional[str] = None
    if not skip_backup:
        with span("backup"):
            manager = BackupManager()
            backup_path = str(manager.create_backup())
            manager.cleanup_old_backups()

    if links is None:
        links = link_targets(body, db_path=db_path)

    with span("convert"):
        result = markdown_to_richtext_new_linked(body, links)

    if is_gui_session():
        with span("execute"):
            execute_append_to_note(name, result.data, result.pending_actions)
    else:
        with span("execute"):
            UIHelperClient().append_to_note(name, result.data, result.pending_actions)

    return {"backup_path": backup_path}


def note_preview(match: TitleMatch, *, db_path: Optional[str] = None) -> Optional[str]:
    """First ~200 characters of the note's body, read by pk; None if the
    note can't be parsed (a preview is a courtesy, never a reason to fail)."""
    return read_preview(NoteContentReader(db_path), match)


def _backup(skip_backup: bool) -> Optional[str]:
    if skip_backup:
        return None
    with span("backup"):
        manager = BackupManager()
        backup_path = str(manager.create_backup())
        manager.cleanup_old_backups()
    return backup_path


def delete_note_by_name(
    name: str, *, skip_backup: bool = False, db_path: Optional[str] = None
) -> dict:
    """Delete a note (move to Recently Deleted) through the shared chain.

    Guards first (note_target.resolve_unique_note, issue #229): the title must
    match exactly one note, case-insensitively and Recently Deleted included,
    and that note must be neither in Recently Deleted (a second delete would be
    permanent) nor locked. A refusal touches nothing, not even a backup.

    Deletion itself is pure AppleScript - no clipboard, keystrokes, or focus
    requirements - so the rest of the chain is backup -> local/daemon route ->
    iCloud sync nudge. Used by the MCP delete_note tool (stdio and the remote
    queue) and the Electron delete-note script. Returns the deleted note's
    title, folder, modified date and a body preview so the caller can confirm
    it hit the right note.
    """
    with NotesDatabase(db_path) as db:
        match = resolve_unique_note(db, name)
    preview = note_preview(match, db_path=db_path)

    backup_path = _backup(skip_backup)

    if is_gui_session():
        with span("execute"):
            execute_delete_note(name)
    else:
        with span("execute"):
            UIHelperClient().delete_note(name)

    with span("sync"):
        sync_nudged = nudge_icloud_sync()

    return {
        "backup_path": backup_path,
        "sync_nudged": sync_nudged,
        "title": match.title,
        "folder": match.folder,
        "modified": match.modified,
        "preview": preview,
    }


def move_note_by_name(
    name: str,
    folder: str,
    *,
    skip_backup: bool = False,
    db_path: Optional[str] = None,
) -> dict:
    """Move a note into an existing folder (issue #230).

    Same note guards as delete (exactly one match, not in Recently Deleted,
    not locked); the folder must already exist (note_target.resolve_folder,
    never created). The move is pure AppleScript addressed by id, so it can't
    hit a same-titled note. A note already in that folder is a no-op.
    """
    with NotesDatabase(db_path) as db:
        match = resolve_unique_note(db, name)
        target = resolve_folder(db, folder)
        store = db.store_uuid()
    result = {
        "title": match.title,
        "old_folder": match.folder,
        "new_folder": target.path,
        "moved": False,
        "backup_path": None,
        "sync_nudged": False,
    }
    if match.folder_pk == target.pk:
        return result
    if store is None:
        raise AppleScriptError(
            "Couldn't read the Notes store id; the note was not moved"
        )
    note_id = coredata_id(store, "ICNote", match.pk)
    folder_id = coredata_id(store, "ICFolder", target.pk)

    result["backup_path"] = _backup(skip_backup)

    if is_gui_session():
        with span("execute"):
            execute_move_note(note_id, folder_id)
    else:
        with span("execute"):
            UIHelperClient().move_note(note_id, folder_id)

    with span("sync"):
        result["sync_nudged"] = nudge_icloud_sync()
    result["moved"] = True
    return result
