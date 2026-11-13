"""Resolve the note and folder a delete or move addresses (#229, #230).

One policy for every caller - the stdio delete_note / move_note tools, the
Electron delete script (through write_service) and the remote write queue - so
the guards can't fork:

- the title must match exactly one note (case-insensitively, Recently Deleted
  included: the AppleScript delete addresses notes by title), and that note's
  title must equal the requested one exactly;
- a note already in Recently Deleted is refused (deleting it again would be
  permanent; moving it would restore it);
- a locked (password-protected) note is refused;
- a move target must be an existing ordinary folder, never created.
"""

import re
import unicodedata
from typing import Iterable, Optional

from .exceptions import (
    AmbiguousFolderError,
    AmbiguousNoteTitleError,
    EncryptedNoteError,
    FolderNotFoundError,
    NoteInTrashError,
    NoteNotFoundError,
    UIScriptingError,
)
from .models import FolderMatch, TitleMatch
from .title_scope import body_text

PREVIEW_CHARS = 200

_STORE_RE = re.compile(r"^[0-9A-Fa-f-]{36}$")
_ID_KINDS = ("ICNote", "ICFolder")
# Leading emoji / symbols / spaces on a folder name ("🗄️ Archive", "★ Inbox").
_PREFIX_RE = re.compile(r"^[\W_]+")


_NOTE_ID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _describe(m: TitleMatch) -> str:
    return f"- folder '{m.folder}', id {m.uuid}, modified {m.modified or 'unknown'}"


def describe_matches(matches: Iterable[TitleMatch]) -> str:
    return "\n".join(_describe(m) for m in matches)


def is_note_id(ref: str) -> bool:
    """Whether ``ref`` has the shape of a note id (the note's uuid, #83)."""
    return isinstance(ref, str) and bool(_NOTE_ID_RE.match(ref))


def title_for_ref(db, ref: str) -> str:
    """The title a write should address for ``ref``, a title or a note id (#83).

    An id of a note the database can see becomes that note's title; anything
    else (a title, or an id-shaped string naming no note) is returned as is.
    Writes open notes by title, so the caller still has to check the title is
    unique (require_unique_title / resolve_unique_note).
    """
    note = _note_for_id(db, ref)
    return note.name if note is not None else ref


def title_for_write(db, ref: str) -> str:
    """title_for_ref for a write (#83), refusing an id that names a note in
    Recently Deleted while a live note shares its title: the write path would
    address the live note (live_note_id_beside_trashed_twins), not the one the
    id named. Raises NoteInTrashError."""
    note = _note_for_id(db, ref)
    if note is None:
        return ref
    matches = db.find_title_matches(note.name)
    named = next((m for m in matches if m.pk == note.pk), None)
    if named is not None and named.in_trash and any(not m.in_trash for m in matches):
        raise NoteInTrashError(
            f"Note id {ref} is in Recently Deleted, and a live note shares its title "
            f"'{note.name}', so a write would land in the live note instead. Restore "
            "it in Notes first, or address the live note."
        )
    return note.name


def _note_for_id(db, ref: str):
    """The note whose id is ``ref``, or None. A uuid of a row that isn't a
    note (a folder or attachment shares the table, with no note title) is
    None too, never a Note without a name."""
    if not is_note_id(ref):
        return None
    note = db.get_note_by_uuid(ref)
    return note if note is not None and note.name else None


def match_summary(m: TitleMatch) -> dict:
    """One ``other_matches`` entry of a read result (#83)."""
    return {"id": m.uuid, "title": m.title, "folder": m.folder, "modified": m.modified}


def _same_title(a: str, b: str) -> bool:
    return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)


def resolve_read_note(db, ref: str, *, case_insensitive: bool = False):
    """The note a read of ``ref`` (a title or a note id) addresses, plus every
    other visible note sharing its title (#83).

    An id reads exactly that note. A title reads the note carrying exactly
    that title, preferring a live note over a copy in Recently Deleted, then
    the most recently modified; with ``case_insensitive`` (suggest_links'
    documented lookup) a title differing only in case is accepted when no
    exact one exists. The other matches are compared case-insensitively
    (AppleScript and [[links]] match titles that way) and honour the folder
    scope, so a scoped server never lists a hidden note.

    Returns ``(Note, [TitleMatch, ...])``. Raises NoteNotFoundError.
    """
    note = _note_for_id(db, ref)
    title = note.name if note is not None else ref
    matches = [
        m for m in db.find_title_matches(title) if db.is_visible(m.folder_pk, m.folder)
    ]
    if note is None:
        candidates = [m for m in matches if _same_title(m.title, ref)]
        if not candidates and case_insensitive:
            candidates = list(matches)
        if not candidates:
            raise NoteNotFoundError(f"Note '{ref}' not found")
        candidates.sort(key=lambda m: m.pk)
        candidates.sort(key=lambda m: m.modified or "", reverse=True)
        candidates.sort(key=lambda m: m.in_trash)
        note = db.get_note_by_pk(candidates[0].pk)
    return note, [m for m in matches if m.pk != note.pk]


def live_note_id_beside_trashed_twins(db, name: str) -> Optional[str]:
    """The AppleScript id of the one live note titled ``name`` when copies in
    Recently Deleted share its title, else None (#83).

    AppleScript's by-title lookups (`show note "X"`, `first note whose name`)
    can bind a trashed copy, so a write to such a title must address the live
    note by id. None means "address it by title as before": no trashed copy,
    no live note, or several live notes (the ambiguity guard refuses those).
    """
    matches = db.find_title_matches(name)
    live = [m for m in matches if not m.in_trash]
    if len(live) != 1 or len(matches) == 1:
        return None
    store = db.store_uuid()
    if not store:
        # Never fall back to the title here: it could bind the trashed copy.
        raise UIScriptingError(
            f"Can't address '{name}' safely: copies in Recently Deleted share its "
            "title and the Notes store id needed to target the live note by id "
            "wasn't found."
        )
    return coredata_id(store, "ICNote", live[0].pk)


def require_unique_title(db, name: str) -> None:
    """Refuse a write to a title more than one note carries (#83).

    The local twin of the remote queue's accept check. Only live notes count:
    one live note beside copies in Recently Deleted is not ambiguous, and the
    UI write path addresses that live note by its AppleScript id
    (live_note_id_beside_trashed_twins), never by title, so a trashed copy
    can't take the write. With no live note the trashed copies count, as
    before. A single note whose title differs only in case is not ambiguous
    either (AppleScript matches titles case-insensitively). Raises
    AmbiguousNoteTitleError naming every match, or NoteNotFoundError.
    """
    found = db.find_title_matches(name)
    if not found:
        raise NoteNotFoundError(f"Note '{name}' not found")
    matches = [m for m in found if not m.in_trash] or found
    if len(matches) > 1:
        fix = "rename one first"
        if any(m.in_trash for m in matches):
            fix += " (or empty them from Recently Deleted)"
        raise AmbiguousNoteTitleError(
            f"{len(matches)} notes are titled '{name}' (titles match "
            "case-insensitively, Recently Deleted included). Writes open a note "
            f"by its title, so not even its id can pick one of them; {fix}. "
            "Matches:\n" + describe_matches(matches),
            matches,
        )


def resolve_unique_note(db, name: str) -> TitleMatch:
    """The one note titled ``name``, or a refusal naming every candidate.

    Raises NoteNotFoundError (no note, or only a different-case title),
    AmbiguousNoteTitleError (several), NoteInTrashError, EncryptedNoteError.
    """
    matches = db.find_title_matches(name)
    if not matches:
        raise NoteNotFoundError(
            f"Note '{name}' not found (0 notes have that title). "
            "Use list_notes or search_notes to find the exact title."
        )
    if len(matches) > 1:
        fix = "Rename one first"
        if any(m.in_trash for m in matches):
            fix += " (a copy in Recently Deleted counts too: empty it from Recently Deleted)"
        raise AmbiguousNoteTitleError(
            f"{len(matches)} notes are titled '{name}' (titles match "
            "case-insensitively, Recently Deleted included), so the target is "
            f"ambiguous. {fix}. Matches:\n" + describe_matches(matches),
            matches,
        )
    match = matches[0]
    if unicodedata.normalize("NFC", match.title) != unicodedata.normalize("NFC", name):
        raise NoteNotFoundError(
            f"Note '{name}' not found under that exact title. The only near match "
            f"is '{match.title}' ({match.folder}, modified {match.modified or 'unknown'}); "
            "use its exact title."
        )
    if match.in_trash:
        raise NoteInTrashError(
            f"Note '{name}' is already in Recently Deleted. Restore or remove it "
            "in Notes directly."
        )
    if match.locked:
        raise EncryptedNoteError(
            f"Note '{name}' is password-protected (locked); unlock it in Notes first."
        )
    return match


def _bare(text: str) -> str:
    return _PREFIX_RE.sub("", text).strip().casefold()


def _bare_path(path: str) -> str:
    return "/".join(_bare(part) for part in path.split("/"))


def resolve_folder(db, folder: str) -> FolderMatch:
    """The existing folder ``folder`` names. Never creates one.

    Tried in order, case-insensitively, and the first tier with any match
    decides: the full path ("Other/🗄️ Archive"), the folder's own name
    ("🗄️ Archive"), then either with leading emoji/symbols ignored
    ("Archive", "Other/Archive"). More than one match in that tier is refused.
    """
    wanted = folder.strip()
    if not wanted:
        raise FolderNotFoundError("Folder name cannot be empty")
    folders = db.get_move_folders()
    key = wanted.casefold()
    bare_key = _bare_path(wanted)
    tiers = (
        lambda f: f.path.casefold() == key,
        lambda f: f.name.casefold() == key,
        lambda f: (
            bare_key != ""
            and (_bare_path(f.path) == bare_key or _bare(f.name) == bare_key)
        ),
    )
    for tier in tiers:
        hits = [f for f in folders if tier(f)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            listing = "\n".join(
                f"- '{f.path}'" for f in sorted(hits, key=lambda f: f.path)
            )
            raise AmbiguousFolderError(
                f"{len(hits)} folders match '{folder}'; use the full path. Matches:\n"
                + listing,
                hits,
            )
    raise FolderNotFoundError(
        f"Folder '{folder}' not found (no folder is created; Recently Deleted and "
        "smart folders can't be move targets). Use list_folders to see folders."
    )


def coredata_id(store_uuid: str, kind: str, pk: int) -> str:
    """AppleScript id of a note or folder: x-coredata://<store>/<kind>/p<pk>.

    Validated strictly: the id is interpolated into an AppleScript string.
    """
    if not isinstance(store_uuid, str) or not _STORE_RE.match(store_uuid):
        raise ValueError(f"not a Notes store id: {store_uuid!r}")
    if kind not in _ID_KINDS:
        raise ValueError(f"not a note/folder kind: {kind!r}")
    if not isinstance(pk, int) or isinstance(pk, bool) or pk < 1:
        raise ValueError(f"not a primary key: {pk!r}")
    return f"x-coredata://{store_uuid}/{kind}/p{pk}"


def preview_text(resolved_text: str) -> str:
    """The first ~200 characters of a note's body (title line dropped),
    whitespace collapsed, so a caller can confirm it hit the right note."""
    body = " ".join(body_text(resolved_text or "").split())
    if len(body) <= PREVIEW_CHARS:
        return body
    return body[:PREVIEW_CHARS] + "…"


def read_preview(reader, match: TitleMatch) -> Optional[str]:
    """Body preview of ``match`` read by pk through ``reader`` (a
    NoteContentReader); None on any failure - a courtesy read, never a
    reason to refuse or fail a delete. Shared by stdio and the remote queue."""
    try:
        content = reader.get_note_content_for_note(match.as_note())
    except Exception:
        return None
    return preview_text(content.resolved_text)
