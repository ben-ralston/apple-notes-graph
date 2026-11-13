"""Accept, apply and undo queued remote writes (issue #169).

``accept`` validates a remote write call (the stdio tool's own rules), resolves
its note to a single UUID and records a base fingerprint. ``apply`` re-checks
for conflicts and then calls the *stdio tool function itself*, so a queued
write goes through exactly the local pipeline: backup, markdown conversion,
edit_lock, write-guard overlay + lead countdown, frontmost guards and
verify-after-write. ``undo`` (CLI only) puts back the journaled prior content.

The applier never takes ``edit_lock`` itself: the stdio path does, and flock is
not reentrant across file descriptors. Two drainers are kept apart by the
queue's drain lock instead (write_worker / cli).

Errors are classified by exception type along the ``__cause__``/``__context__``
chain - the stdio tools wrap everything in ``ToolError(str(e))`` - never by
message text.
"""

import time
from typing import Callable, Optional

from apple_notes.database import NotesDatabase
from apple_notes.exceptions import (
    AmbiguousFolderError,
    AppleNotesError,
    AmbiguousNoteTitleError,
    EditLockTimeoutError,
    EncryptedNoteError,
    FolderNotFoundError,
    NoteInTrashError,
    NoteNotFoundError,
    NotesModalOpenError,
    NotesNotFrontmostError,
    NotesNotRespondingError,
    PartialWriteError,
    ProtobufParseError,
    SectionEditDamagedError,
    SectionEditRolledBackError,
    SessionLockedError,
    UIScriptingError,
    WriteVerificationError,
)
from apple_notes.note_content import NoteContentReader
from apple_notes.note_target import (
    is_note_id,
    read_preview,
    resolve_folder,
    resolve_unique_note,
    title_for_write,
)
from apple_notes.protobuf_parser import NoteProtobufParser
from apple_notes.richtext.markdown_converter import MarkdownConversionError
from apple_notes.richtext.section_fragment import FragmentError
from apple_notes.relink import RelinkError, plan_relink
from apple_notes.sections import (
    MediaCapExceeded,
    NoteBusyError,
    SectionError,
    without_final_empty_paragraph,
    load_snapshot,
    plan_edit,
    refuse_heavy_media,
    resolve_section,
)
from apple_notes.title_scope import body_text
from apple_notes.unified_log import _chain
from mcp.server.fastmcp.exceptions import ToolError

from .write_queue import APPLIED_STATUSES, CapExceeded, WriteQueue

MAX_NOTE_NAME_LENGTH = 255
# Pure-AppleScript writes resolved by the shared exact-one-note guard (#229,
# #230) instead of the UI-write title checks; they journal no prior content
# and can't be undone here.
TARGET_TOOLS = ("delete_note", "move_note")
_TARGET_REFUSALS = (
    NoteNotFoundError,
    AmbiguousNoteTitleError,
    NoteInTrashError,
    EncryptedNoteError,
    FolderNotFoundError,
    AmbiguousFolderError,
)
VALID_OPERATIONS = ("append", "replace", "find_replace")
MAX_REASON_CHARS = 300
# Paragraphs a remote delete/replace/find_replace may remove or rewrite (a
# remote section edit can't be undone for deletes, and a tiny replace body
# would otherwise wipe a huge section). Append removes nothing: exempt.
MAX_REMOTE_SECTION_DELETE = 50
POLL_INTERVAL = 0.25
POLL_TRIES = 20  # ~5 s for Notes to persist the write to NoteStore.sqlite
PARTIAL_POLL_TRIES = 8  # ~2 s: did a failed attempt land after all?

_REQUEUE = (SessionLockedError, EditLockTimeoutError)
# Attachments the markdown read -> write round trip preserves ({#tag}, [[Note]]);
# anything else (images, drawings, files, tables) blocks a markdown undo.
ROUND_TRIP_ATTACHMENTS = frozenset(
    {
        "com.apple.notes.inlinetextattachment.hashtag",
        "com.apple.notes.inlinetextattachment.link",
    }
)
_RETRY_ONCE = (NotesNotFrontmostError, NotesNotRespondingError)
# A dialog open in Notes (issue #199) is waited out, never counted as an
# attempt: it is the user's screen, not a failure of the write. No title: the
# reason reaches the remote caller, and dialog text can name any note.
MODAL_WAIT_REASON = (
    "waiting: a dialog is open in Notes; the write applies once it is closed"
)


class SectionCapExceeded(SectionError):
    """A remote section edit would touch more paragraphs than the remote cap."""


class _PostReadUnavailable(Exception):
    """The post-write snapshot couldn't be read; nothing is journaled."""


class WriteRefused(Exception):
    """A remote write (or an undo) was refused; the message is for the caller."""

    def __init__(self, message: str, cap: Optional[str] = None):
        super().__init__(message)
        self.cap = cap


def db_checklist_state(db, pk: int, item_text: str) -> Optional[tuple[int, bool]]:
    """(position, checked) of the first checklist run containing item_text.

    Mirrors richtext.operations.toggle_checkbox, which flips the first
    attribute run that contains the text AND is a checklist paragraph (run
    granularity, not lines: formatting inside an item splits runs).
    """
    data = db.get_note_data(pk)
    if not data:
        return None
    parser = NoteProtobufParser()
    text, runs = parser.parse(data)
    pos = 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        length = parser.get_run_length(run)
        para = run.get(parser.FIELD_PARAGRAPH_STYLE)
        checklist = para.get(parser.FIELD_CHECKLIST) if isinstance(para, dict) else None
        if isinstance(checklist, dict) and item_text in text[pos : pos + length]:
            return pos, checklist.get(parser.FIELD_CHECKLIST_CHECKED, 0) == 1
        pos += length
    return None


def _short(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:MAX_REASON_CHARS]


# Tools whose title check counts live notes only (#83): one live note beside
# copies in Recently Deleted is not ambiguous, because the stdio tool they
# apply through addresses that live note by AppleScript id. Every other tool
# keeps counting Recently Deleted (its path can still bind a trashed copy).
LIVE_TITLE_TOOLS = frozenset({"update_note_content", "toggle_checklist_item"})


def _title_count(db, tool: str, name: str) -> int:
    if tool in LIVE_TITLE_TOOLS:
        return db.count_live_notes_titled(name)
    return db.count_notes_titled(name)


class WriteApplier:
    def __init__(
        self,
        queue: WriteQueue,
        *,
        db_factory: Callable = NotesDatabase,
        reader_factory: Callable = NoteContentReader,
        checklist_state: Callable = db_checklist_state,
        stdio=None,
        delete_note: Optional[Callable[[str], object]] = None,
        sleep: Callable[[float], None] = time.sleep,
        log=None,
    ):
        self.queue = queue
        self._db = db_factory
        self._reader = reader_factory
        self._checklist_state = checklist_state
        self._stdio = stdio
        self._delete_note = delete_note
        self._sleep = sleep
        self._log = log

    # ------------------------------------------------------------ seams
    @property
    def stdio(self):
        if self._stdio is None:
            from apple_notes_mcp import server

            self._stdio = server
        return self._stdio

    def _delete(self, name: str) -> None:
        if self._delete_note is None:
            from apple_notes import write_service

            self._delete_note = write_service.delete_note_by_name
        try:
            self._delete_note(name)
        except AppleNotesError as e:
            # The shared delete guards (#229) refuse a locked, trashed or
            # now-ambiguous note: a refusal for the CLI, never a traceback.
            raise WriteRefused(f"undo failed: {e}") from None

    def _event(self, action: str, rec: dict, **extra) -> None:
        """requests.log metadata only: never titles, arguments or contents."""
        if self._log is not None:
            self._log.event(
                "write",
                action=action,
                write_id=rec.get("id"),
                tool=rec.get("tool"),
                operation=rec.get("operation"),
                **extra,
            )

    # ------------------------------------------------------------ accept
    def accept(self, tool: str, args: dict, subject: Optional[str] = None) -> dict:
        """Validate, resolve and queue one remote write. Raises WriteRefused."""
        args = {k: v for k, v in args.items() if v is not None}
        meta = {"tool": tool, "operation": self._operation(tool, args)}
        try:
            name = self._validate(tool, args)
            # Cheap caps first: no NoteStore scan or note parse for refused calls.
            self.queue.check_static_caps({"operation": meta["operation"], "args": args})
            if tool != "add_note" and is_note_id(name):
                # An id addresses its note's title (#83); the queued write then
                # runs every title guard below, at accept and again at apply.
                with self._db() as db:
                    try:
                        name = title_for_write(db, name)
                    except NoteInTrashError as e:
                        raise WriteRefused(str(e)) from None
                args["name"] = name
            record = self._resolve(tool, args, name)
            # After the note checks, as in the stdio tools: a missing note or
            # shared title is the more basic answer.
            self._refuse_untypeable(tool, args)
            target = record.pop("target", None)
            record.update(
                tool=tool, operation=meta["operation"], args=args, subject=subject
            )
            wid = self.queue.admit(record)
        except CapExceeded as e:
            self._event("deny", meta, cap=e.cap)
            raise WriteRefused(str(e), cap=e.cap) from None
        except WriteRefused:
            self._event("deny", meta, cap=None)
            raise
        self._event("accept", {**meta, "id": wid})
        accepted = {"write_id": wid, "queued_ahead": self.queue.queued_ahead(wid)}
        if target is not None:
            accepted["note"] = target
        return accepted

    @staticmethod
    def _operation(tool: str, args: dict) -> str:
        if tool == "update_note_content":
            return str(args.get("operation", ""))
        if tool == "edit_note_section":
            return "section_delete" if args.get("operation") == "delete" else tool
        return tool

    def _validate(self, tool: str, args: dict) -> str:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            raise WriteRefused("Note name cannot be empty or whitespace-only")
        if tool == "add_note":
            if len(name) > MAX_NOTE_NAME_LENGTH:
                raise WriteRefused(
                    f"Note name too long (max {MAX_NOTE_NAME_LENGTH} characters, got {len(name)})"
                )
            if "\n" in name or "\r" in name:
                raise WriteRefused("Note name cannot contain newlines")
            folder = args.get("folder", "Notes")
            if folder != "Notes":
                raise WriteRefused(
                    f"Only 'Notes' folder is supported. Got '{folder}'. "
                    "Move notes to other folders manually after creation."
                )
        elif tool == "update_note_content":
            operation = args.get("operation")
            if operation not in VALID_OPERATIONS:
                raise WriteRefused(
                    f"Invalid operation '{operation}'. Must be one of: {set(VALID_OPERATIONS)}"
                )
            old_text = args.get("old_text")
            if operation == "find_replace":
                if old_text is None:
                    raise WriteRefused(
                        "old_text is required for find_replace operation"
                    )
                if old_text == "":
                    raise WriteRefused(
                        "old_text cannot be empty for find_replace operation"
                    )
            elif old_text is not None:
                raise WriteRefused("old_text is only valid for find_replace operation")
        elif tool == "toggle_checklist_item":
            item = args.get("item_text")
            if not isinstance(item, str) or not item.strip():
                raise WriteRefused("item_text cannot be empty or whitespace-only")
        elif tool == "edit_note_section":
            from apple_notes_mcp.server import _validate_section_args

            try:
                _validate_section_args(
                    args.get("operation"),
                    args.get("heading"),
                    args.get("section_text"),
                    args.get("body"),
                    args.get("old_text"),
                    args.get("new_text"),
                )
            except ToolError as e:
                raise WriteRefused(str(e)) from None
        elif tool == "delete_note":
            pass
        elif tool == "move_note":
            folder = args.get("folder")
            if not isinstance(folder, str) or not folder.strip():
                raise WriteRefused("Folder name cannot be empty or whitespace-only")
        elif tool == "replace_note_link":
            for key in ("old_target", "new_target"):
                value = args.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise WriteRefused(f"{key} cannot be empty or whitespace-only")
        else:
            raise WriteRefused(f"{tool} is not a remote write tool")
        return name

    @staticmethod
    def _refuse_untypeable(tool: str, args: dict) -> None:
        """Refuse at accept a tag/table the apply would refuse (issue #264).

        The stdio write paths refuse these before any paste: an untypeable
        table ("Nothing was written", #122/#260) for add_note and
        update_note_content, any new tag or table for edit_note_section (v1
        limit). Run here, the caller hears it now instead of finding it later
        in get_write_status. Both depend on the markdown alone; the apply-time
        checks stay.
        """
        from apple_notes.ui_operations import _typed_actions
        from apple_notes_mcp.server import _refuse_new_tags_and_tables

        try:
            if tool in ("add_note", "update_note_content"):
                if isinstance(args.get("body"), str):
                    _typed_actions(args["body"])
            elif tool == "edit_note_section":
                # Links don't matter: tags and tables are refused whatever
                # the note links resolve to.
                _refuse_new_tags_and_tables({}, args.get("body"), args.get("new_text"))
        except (UIScriptingError, FragmentError) as e:
            raise WriteRefused(str(e)) from None
        except MarkdownConversionError:
            # Unchanged: the apply converts against the note's own richtext
            # for replace/find_replace and reports any failure there.
            return

    def _preview(self, match) -> Optional[str]:
        return read_preview(self._reader(), match)

    def _resolve_target(self, tool: str, args: dict, name: str) -> dict:
        """Accept-time guards for delete_note / move_note (#229, #230).

        The shared exact-one-note check, plus the destination folder for a
        move (queued as its resolved full path, so the apply moves exactly
        what was accepted). Returns the record plus a ``target`` summary the
        caller sees at once, to confirm it addressed the right note.
        """
        with self._db() as db:
            try:
                match = resolve_unique_note(db, name)
                folder = (
                    resolve_folder(db, args["folder"]) if tool == "move_note" else None
                )
            except _TARGET_REFUSALS as e:
                raise WriteRefused(str(e)) from None
            record = {
                "note_title": name,
                "note_uuid": match.uuid,
                "base_fingerprint": db.note_fingerprint(match.pk),
            }
        if folder is None:
            record["target"] = {
                "title": match.title,
                "folder": match.folder,
                "modified": match.modified,
                "preview": self._preview(match),
            }
            return record
        if folder.pk == match.folder_pk:
            raise WriteRefused(
                f"'{name}' is already in '{folder.path}'; nothing to move."
            )
        args["folder"] = folder.path
        record["target"] = {
            "title": match.title,
            "old_folder": match.folder,
            "new_folder": folder.path,
        }
        return record

    def _resolve(self, tool: str, args: dict, name: str) -> dict:
        if tool in TARGET_TOOLS:
            return self._resolve_target(tool, args, name)
        with self._db() as db:
            count = _title_count(db, tool, name)
            if tool == "add_note":
                if count:
                    raise WriteRefused(
                        f"A note titled '{name}' already exists (titles match case-insensitively, "
                        "Recently Deleted included). Choose another title."
                    )
                return {"note_title": name}
            if count == 0:
                raise WriteRefused(
                    f"Note '{name}' not found. Use list_notes to see available notes."
                )
            if count > 1:
                raise WriteRefused(
                    f"{count} notes are titled '{name}' (case-insensitive, Recently Deleted "
                    "included). Remote writes need a unique title; rename one first."
                )
            try:
                note = db.get_note_by_name(name)
            except NoteNotFoundError:
                raise WriteRefused(
                    f"Note '{name}' not found under that exact title; use the note's exact title "
                    "(case-sensitive) as list_notes shows it."
                ) from None
            content = self._content(note, name)
            record = {
                "note_title": name,
                "note_uuid": note.uuid,
                "base_fingerprint": db.note_fingerprint(note.pk),
            }
            # Only update_note_content is a whole-note find_replace: a section
            # find_replace is scoped to its section and checked by the stdio tool.
            if (
                tool == "update_note_content"
                and args.get("operation") == "find_replace"
            ):
                self._find_replace_guard(content, args["old_text"], WriteRefused)
            if tool == "edit_note_section":
                try:
                    _, section = self._section_state(db, note, args)
                except SectionError as e:
                    raise WriteRefused(str(e)) from None
                record["section_fingerprint"] = section.fingerprint
            if tool == "toggle_checklist_item":
                state = self._toggle_state(db, note, args["item_text"])
                if state is None:
                    raise WriteRefused(
                        f"No checklist item containing that text in '{name}'."
                    )
                record["item_position"], current = state
                record["desired_checked"] = not current
            if tool == "replace_note_link":
                plan = self._relink_plan(db, note, args, WriteRefused)
                # Queue the resolved uuids, not titles: the apply re-points
                # exactly what was accepted, and undo can swap them back.
                args["old_target"] = plan.old_uuid
                args["new_target"] = plan.new_uuid
            return record

    @staticmethod
    def _relink_plan(db, note, args: dict, error):
        try:
            return plan_relink(
                db,
                note,
                args["old_target"],
                args["new_target"],
                bool(args.get("replace_all", False)),
            )
        except RelinkError as e:
            raise error(str(e)) from None
        except EncryptedNoteError:
            raise error(
                f"Note '{note.name}' is encrypted and cannot be edited."
            ) from None
        except ProtobufParseError as e:
            raise error(f"Failed to parse note content for editing: {e}") from None

    def _section_snapshot(self, db, note, error):
        try:
            render = self._reader().paragraph_renderer(note)
            return load_snapshot(db, note.pk, render)
        except SectionError as e:
            raise error(str(e)) from None
        except EncryptedNoteError:
            raise error(
                f"Note '{note.name}' is encrypted and cannot be edited."
            ) from None
        except ProtobufParseError as e:
            raise error(f"Failed to parse note content for editing: {e}") from None

    def _section_state(self, db, note, args: dict):
        """(snapshot, section) the call addresses, with the stdio tool's own
        media, resolve + plan checks and the remote size cap.  Raises SectionError
        (SectionCapExceeded / MediaCapExceeded for the caps); snapshot read errors
        as WriteRefused."""
        refuse_heavy_media(db, note.pk, note.name)
        snap = self._section_snapshot(db, note, WriteRefused)
        section = resolve_section(
            snap,
            note.name,
            heading=args.get("heading"),
            section_text=args.get("section_text"),
        )
        plan_edit(snap, section, args["operation"], note.name)
        operation = args["operation"]
        count = section.b - section.a
        if operation != "append" and count > MAX_REMOTE_SECTION_DELETE:
            raise SectionCapExceeded(
                f"Remote section {operation} is limited to {MAX_REMOTE_SECTION_DELETE} "
                f"paragraphs; this section has {count}."
            )
        return snap, section

    @staticmethod
    def _rendered_text(snap, a: int, b: int) -> str:
        return "\n".join(r if r is not None else "" for r in snap.rendered[a:b])

    @staticmethod
    def _section_has_attachments(snap, a: int, b: int) -> bool:
        """True if [a, b) holds a table (unrenderable) or any attachment a
        markdown round trip would not keep (images, files, drawings)."""
        for i in range(a, b):
            if snap.rendered[i] is None:
                return True
            if any(
                att.uti not in ROUND_TRIP_ATTACHMENTS for att in snap.paragraphs[i].atts
            ):
                return True
        return False

    @staticmethod
    def _outside(snap, a: int, b: int):
        """Everything outside [a, b): text + rendered lines before and after."""
        key = [(p.text, r) for p, r in zip(snap.paragraphs, snap.rendered)]
        return key[:a], key[b:]

    def _post_section(self, rec: dict, claim_snap, section):
        """(fingerprint, text) of the section as the write left it, or None
        unless the span is PROVEN: an in-section edit moves only the section's
        end by the net paragraph change, so the span is [a, b + delta) and the
        paragraphs outside it must equal the claim snapshot's outside [a, b)
        (a paragraph added/removed elsewhere shifts the span: journal nothing)."""
        args, name = rec["args"], rec["note_title"]
        try:
            with self._db() as db:
                note = db.get_note_by_name(name)
                snap = self._section_snapshot(db, note, _PostReadUnavailable)
            # A paste ending in a list adds one empty final paragraph, wherever
            # the section is. Above the note's end it isn't part of the span; at
            # the end it is, because undo resolves the section with it included.
            if (
                section.b < len(claim_snap.paragraphs)
                and not snap.paragraphs[-1].text
                and claim_snap.paragraphs[-1].text
            ):
                snap = without_final_empty_paragraph(snap)
            a = section.a
            b = section.b + len(snap.paragraphs) - len(claim_snap.paragraphs)
            if not 1 <= a <= b <= len(snap.paragraphs):
                return None
            if self._outside(snap, a, b) != self._outside(
                claim_snap, section.a, section.b
            ):
                return None
            if args.get("heading") is not None:
                post = resolve_section(
                    snap, name, heading=args["heading"], section_text=None
                )
            else:
                text = self._rendered_text(snap, a, b)
                post = resolve_section(snap, name, heading=None, section_text=text)
            if (post.a, post.b) != (a, b) or a == b and args.get("heading") is None:
                return None
            if any(r is None for r in snap.rendered[a:b]):
                return None
            return post.fingerprint, self._rendered_text(snap, a, b)
        except (
            _PostReadUnavailable,
            SectionError,
            NoteNotFoundError,
            EncryptedNoteError,
            ProtobufParseError,
        ):
            return None  # expected: the span can't be proven; undo will refuse
        except Exception as e:
            # The write already landed and was confirmed: never let a journaling
            # bug turn it into 'failed' (that invites a duplicate resubmit).
            self._event("journal_error", rec, error=type(e).__name__)
            return None

    def _content(self, note, name: str):
        try:
            return self._reader().get_note_content_for_note(note)
        except EncryptedNoteError:
            raise WriteRefused(
                f"Note '{name}' is encrypted and cannot be edited."
            ) from None
        except ProtobufParseError as e:
            raise WriteRefused(
                f"Failed to parse note content for editing: {e}"
            ) from None

    def _toggle_state(self, db, note, item_text: str):
        try:
            return self._checklist_state(db, note.pk, item_text)
        except EncryptedNoteError:
            raise WriteRefused(
                f"Note '{note.name}' is encrypted and cannot be edited."
            ) from None

    @staticmethod
    def _find_replace_guard(content, old_text: str, error) -> None:
        """The stdio tool's exactly-once + raw-text checks, as one function."""
        count = body_text(content.resolved_text).count(old_text)
        if count == 0:
            raise error(
                "old_text not found in note body. Verify the text matches exactly (case-sensitive)."
            )
        if count > 1:
            raise error(
                f"old_text found {count} times in note body. "
                "Provide more surrounding context to uniquely identify the text."
            )
        if body_text(content.raw_text).count(old_text) == 0:
            raise error(
                "old_text was found in the rendered note but not in the underlying note data "
                "(a table cell, or rendered link/formatting markup). Use the plain text form."
            )

    # ------------------------------------------------------------ apply
    def apply(self, wid: str) -> str:
        """Apply one queued write; return its status afterwards.

        A record that can't be claimed (already applying or final) is left
        alone. A ``finally`` guarantees nothing stays 'applying'.
        """
        if not self.queue.claim(wid):
            rec = self.queue.get(wid)
            return rec["status"] if rec else "missing"
        rec = self.queue.get(wid)
        try:
            self._apply_claimed(rec)
        except Exception as e:  # unexpected: never leave the record claimed
            self.queue.finish(wid, "failed", f"internal error: {type(e).__name__}")
            self._event("fail", rec, error=type(e).__name__)
        finally:
            if self.queue.get(wid)["status"] == "applying":
                self.queue.finish(wid, "failed", "internal error: left applying")
        return self.queue.get(wid)["status"]

    def _conflict(self, rec: dict, reason: str) -> None:
        self.queue.finish(rec["id"], "conflict", reason)
        self._event("conflict", rec)

    def _apply_target(self, rec: dict) -> None:
        """Apply a queued delete_note / move_note (#229, #230).

        Re-runs the accept guards against the note as it is now: a note that
        went to Recently Deleted, gained a same-titled twin, got locked or was
        replaced by another note of that title is a conflict, never a guess.
        Edits to the note's text in between don't block it.
        """
        args, name, tool = rec["args"], rec["note_title"], rec["tool"]
        with self._db() as db:
            try:
                match = resolve_unique_note(db, name)
            except NoteInTrashError:
                return self._conflict(rec, "the note is already in Recently Deleted")
            except AmbiguousNoteTitleError as e:
                return self._conflict(
                    rec, f"{len(e.matches)} notes now share this title"
                )
            except NoteNotFoundError:
                return self._conflict(
                    rec, "the note no longer exists under this exact title"
                )
            except EncryptedNoteError:
                return self._conflict(
                    rec, "the note is now locked (password-protected)"
                )
            if match.uuid != rec["note_uuid"]:
                return self._conflict(rec, "a different note now has this title")
            claim_fp = db.note_fingerprint(match.pk)
            if tool == "move_note":
                try:
                    folder = resolve_folder(db, args["folder"])
                except (FolderNotFoundError, AmbiguousFolderError) as e:
                    return self._conflict(
                        rec, f"the destination folder changed: {_short(e)}"
                    )
                # The queued folder is the full path accepted; a looser tier
                # (a renamed folder's namesake elsewhere) is never a stand-in.
                if folder.path != args["folder"]:
                    return self._conflict(
                        rec, "the destination folder changed since the move was queued"
                    )
                if folder.pk == match.folder_pk:
                    self.queue.finish(
                        rec["id"],
                        "applied",
                        "already in that folder; nothing to do",
                        post_fingerprint=claim_fp,
                        noop=True,
                    )
                    self._event("apply", rec, noop=True)
                    return None
        self.queue.update(
            rec["id"],
            claim_fingerprint=claim_fp,
            base_changed=claim_fp != rec["base_fingerprint"],
        )
        try:
            self._call_stdio(tool, args)
        except Exception as e:
            return self._handle_error(rec, e, claim_fp)
        post_fp = self._await_target_state(rec)
        if post_fp is None:
            self.queue.finish(
                rec["id"],
                "applied",
                "applied; Notes hasn't shown the change yet (not confirmed)",
            )
            self._event("apply", rec, confirmed=False)
            return None
        self.queue.finish(rec["id"], "applied", None, post_fingerprint=post_fp)
        self._event("apply", rec)
        return None

    def _await_target_state(self, rec: dict) -> Optional[str]:
        """Poll until NoteStore shows the note in Recently Deleted (delete) or
        in the destination folder (move); its fingerprint, or None."""
        for attempt in range(POLL_TRIES):
            with self._db() as db:
                for m in db.find_title_matches(rec["note_title"]):
                    if m.uuid != rec["note_uuid"]:
                        continue
                    done = (
                        m.in_trash
                        if rec["tool"] == "delete_note"
                        else m.folder == rec["args"]["folder"] and not m.in_trash
                    )
                    if done:
                        return db.note_fingerprint(m.pk) or "present"
            if attempt < POLL_TRIES - 1:
                self._sleep(POLL_INTERVAL)
        return None

    def _apply_claimed(self, rec: dict) -> None:
        args, name, tool = rec["args"], rec["note_title"], rec["tool"]
        if tool in TARGET_TOOLS:
            return self._apply_target(rec)
        claim_fp = None
        with self._db() as db:
            count = _title_count(db, tool, name)
            if tool == "add_note":
                if count:
                    return self._conflict(rec, "a note with this title now exists")
            else:
                if count != 1:
                    return self._conflict(
                        rec,
                        "the note no longer exists"
                        if count == 0
                        else f"{count} notes now share this title",
                    )
                try:
                    note = db.get_note_by_name(name)
                except NoteNotFoundError:
                    return self._conflict(
                        rec, "the note no longer exists under this exact title"
                    )
                if note.uuid != rec["note_uuid"]:
                    return self._conflict(rec, "a different note now has this title")
                claim_fp = db.note_fingerprint(note.pk)
                try:
                    content = self._reader().get_note_content_for_note(note)
                    toggle_state = (
                        self._checklist_state(db, note.pk, args["item_text"])
                        if tool == "toggle_checklist_item"
                        else None
                    )
                except (EncryptedNoteError, ProtobufParseError):
                    return self._conflict(
                        rec, "the note is now locked (password-protected) or unreadable"
                    )
                operation = rec["operation"]
                if operation == "replace" and claim_fp != rec["base_fingerprint"]:
                    return self._conflict(
                        rec, "the note changed since the write was queued"
                    )
                section_snap = section = None
                if tool == "edit_note_section":
                    try:
                        section_snap, section = self._section_state(db, note, args)
                    except (SectionCapExceeded, MediaCapExceeded) as e:
                        return self._conflict(rec, str(e))
                    except (SectionError, WriteRefused):
                        return self._conflict(
                            rec, "the section changed since the write was queued"
                        )
                    if section.fingerprint != rec["section_fingerprint"]:
                        return self._conflict(
                            rec, "the section changed since the write was queued"
                        )
                if tool == "update_note_content" and operation == "find_replace":
                    try:
                        self._find_replace_guard(content, args["old_text"], ValueError)
                    except ValueError as e:
                        return self._conflict(rec, f"no longer a unique match: {e}")
                relink_prior = 0
                if tool == "replace_note_link":
                    try:
                        plan = self._relink_plan(db, note, args, ValueError)
                    except ValueError as e:
                        return self._conflict(rec, f"no longer matches: {e}")
                    relink_prior = plan.prior_new_count
                if tool == "toggle_checklist_item":
                    state = toggle_state
                    if state is None:
                        return self._conflict(
                            rec, "the checklist item is no longer in the note"
                        )
                    if state[1] == rec["desired_checked"]:
                        self.queue.finish(
                            rec["id"],
                            "applied",
                            "already in the requested state; nothing to do",
                            post_fingerprint=claim_fp,
                            noop=True,
                        )
                        self._event("apply", rec, noop=True)
                        return None
                prior_markdown = content.resolved_text
                prior_has_attachments = any(
                    getattr(a, "type_uti", "") not in ROUND_TRIP_ATTACHMENTS
                    for a in (getattr(content, "attachments", None) or [])
                )
                if section is not None:
                    prior_markdown = self._rendered_text(
                        section_snap, section.a, section.b
                    )
                    prior_has_attachments = self._section_has_attachments(
                        section_snap, section.a, section.b
                    )
                self.queue.update(
                    rec["id"],
                    claim_fingerprint=claim_fp,
                    prior_markdown=prior_markdown,
                    prior_has_attachments=prior_has_attachments,
                    base_changed=claim_fp != rec["base_fingerprint"],
                    relink_prior_new_count=relink_prior,
                )

        try:
            self._call_stdio(tool, args)
        except Exception as e:
            return self._handle_error(rec, e, claim_fp)

        post_fp, uuid = self._await_post_state(rec, claim_fp)
        if post_fp is None:
            # Notes persists lazily (or the note moved): the write ran without
            # an error, so it is applied - just unconfirmed. Never "failed":
            # that invites a resubmit (a duplicate append, an un-ticked item).
            self.queue.finish(
                rec["id"],
                "applied",
                "applied; Notes hasn't shown the change yet (not confirmed)",
            )
            self._event("apply", rec, confirmed=False)
            return None
        if tool == "toggle_checklist_item":
            try:
                with self._db() as db:
                    note = db.get_note_by_name(name)
                    state = self._checklist_state(db, note.pk, args["item_text"])
            except (NoteNotFoundError, EncryptedNoteError, ProtobufParseError):
                self.queue.finish(
                    rec["id"],
                    "applied",
                    "applied; Notes hasn't shown the change yet (not confirmed)",
                    post_fingerprint=post_fp,
                )
                self._event("apply", rec, confirmed=False)
                return None
            if state is None or state[1] != rec["desired_checked"]:
                self.queue.finish(
                    rec["id"],
                    "failed",
                    "the toggle hit a different item; check the note",
                    post_fingerprint=post_fp,
                )
                self._event("fail", rec, error="toggle_mismatch")
                return None
        fields = {"post_fingerprint": post_fp}
        if tool == "edit_note_section" and rec["operation"] != "section_delete":
            post = self._post_section(rec, section_snap, section)
            if post is not None:
                fields["post_section_fingerprint"], fields["post_section_text"] = post
        if uuid:
            fields["note_uuid"] = uuid
        self.queue.finish(rec["id"], "applied", None, **fields)
        self._event("apply", rec)
        return None

    def _call_stdio(self, tool: str, args: dict) -> None:
        fn = getattr(self.stdio, tool)
        fn(**args)

    def _await_post_state(self, rec: dict, claim_fp: Optional[str]):
        """Poll until NoteStore shows the write; (fingerprint|None, new uuid|None)."""
        for attempt in range(POLL_TRIES):
            with self._db() as db:
                if rec["tool"] == "add_note":
                    if db.count_notes_titled(rec["note_title"]) == 1:
                        try:
                            note = db.get_note_by_name(rec["note_title"])
                        except NoteNotFoundError:
                            note = None
                        if note is not None:
                            return db.note_fingerprint(note.pk), note.uuid
                else:
                    try:
                        note = db.get_note_by_name(rec["note_title"])
                    except NoteNotFoundError:
                        note = None  # renamed/moved by the write: unconfirmed
                    if note is not None:
                        fp = db.note_fingerprint(note.pk)
                        if fp != claim_fp:
                            return fp, None
            if attempt < POLL_TRIES - 1:
                self._sleep(POLL_INTERVAL)
        return None, None

    def _current_fp(self, rec: dict) -> Optional[str]:
        with self._db() as db:
            if rec["tool"] == "add_note":
                return (
                    "absent"
                    if db.count_notes_titled(rec["note_title"]) == 0
                    else "present"
                )
            try:
                return db.note_fingerprint(db.get_note_by_name(rec["note_title"]).pk)
            except NoteNotFoundError:
                return None

    def _changed_since(self, rec: dict, expected: Optional[str]) -> bool:
        """Did a failed attempt change the note? Polls briefly: Notes may flush late."""
        for attempt in range(PARTIAL_POLL_TRIES):
            if self._current_fp(rec) != expected:
                return True
            if attempt < PARTIAL_POLL_TRIES - 1:
                self._sleep(POLL_INTERVAL)
        return False

    def _finish_unverified(
        self, rec: dict, reason: str, claim_fp: Optional[str]
    ) -> None:
        """The write changed the note but isn't fully confirmed (#200).

        Final and never retried (a resubmit would duplicate what landed);
        the post-write fingerprint is captured so it can be undone.
        """
        post_fp, uuid = self._await_post_state(rec, claim_fp)
        fields = {"post_fingerprint": post_fp}
        if uuid:
            fields["note_uuid"] = uuid
        self.queue.finish(rec["id"], "applied_unverified", reason, **fields)
        self._event("apply", rec, verified=False)

    def _handle_error(self, rec: dict, exc: Exception, claim_fp: Optional[str]) -> None:
        chain = _chain(exc)
        wid = rec["id"]
        # A section write whose verify failed is terminal: Notes may persist a
        # timed-out paste late, so a retry could apply the edit twice. (A
        # rolled-back edit was restored; whether to retry is the caller's call.)
        for e in chain:
            if isinstance(e, (SectionEditDamagedError, SectionEditRolledBackError)):
                self.queue.finish(wid, "failed", _short(exc))
                self._event("fail", rec, error=type(e).__name__)
                return None
        # The outermost verification error decides: ui_operations raises a
        # plain WriteVerificationError FROM a link error when nothing changed.
        verification = next(
            (e for e in chain if isinstance(e, WriteVerificationError)), None
        )
        if isinstance(verification, PartialWriteError):
            self._finish_unverified(rec, _short(exc), claim_fp)
            return None
        if any(isinstance(e, _REQUEUE) for e in chain):
            self.queue.requeue(
                wid, reason="waiting: the Mac is locked or another write is running"
            )
            self._event("requeue", rec)
            return None
        modal = any(isinstance(e, NotesModalOpenError) for e in chain)
        if modal or any(isinstance(e, _RETRY_ONCE) for e in chain):
            expected = "absent" if rec["tool"] == "add_note" else claim_fp
            if self._changed_since(rec, expected):
                self._finish_unverified(
                    rec,
                    "the note changed during a failed attempt; check it manually before retrying",
                    claim_fp,
                )
            elif modal:
                self.queue.requeue(wid, reason=MODAL_WAIT_REASON)
                self._event("requeue", rec, error="modal")
            elif rec["attempts"] >= 1:
                self.queue.finish(
                    wid, "failed", f"gave up after a retry: {_short(exc)}"
                )
                self._event("fail", rec, error="focus")
            else:
                self.queue.requeue(
                    wid, count_attempt=True, reason=f"will retry: {_short(exc)}"
                )
                self._event("requeue", rec, error="focus")
            return None
        # The section edit changed nothing and says a retry is safe.
        if any(isinstance(e, NoteBusyError) for e in chain):
            if rec["attempts"] >= 1:
                self.queue.finish(
                    wid, "failed", f"gave up after a retry: {_short(exc)}"
                )
                self._event("fail", rec, error="note_busy")
            else:
                self.queue.requeue(
                    wid, count_attempt=True, reason=f"will retry: {_short(exc)}"
                )
                self._event("requeue", rec, error="note_busy")
            return None
        self.queue.finish(wid, "failed", _short(exc))
        self._event("fail", rec, error=type(chain[-1]).__name__)
        return None

    # ------------------------------------------------------------ undo
    def _undo_section_target(self, db, note, rec: dict) -> dict:
        """How to address the written section now; refuses unless it still
        holds exactly what the write left (text edits only, no tables/images)."""
        args = rec["args"]
        if args is None or rec["prior_markdown"] is None:
            raise WriteRefused(
                "the journal for this write was purged (older than 7 days)"
            )
        if rec["prior_has_attachments"]:
            raise WriteRefused(
                "the section had attachments (images, drawings, files, tables) before this "
                "write; a markdown undo would turn them into labels. Restore it by hand (the "
                "pre-write database backup is in ~/Library/Application Support/notes-graph-backup)"
            )
        if rec["prior_markdown"] == "":
            raise WriteRefused("the section was empty before this write; undo by hand")
        if rec["post_section_fingerprint"] is None or (
            args.get("heading") is None and rec["post_section_text"] is None
        ):
            raise WriteRefused(
                "the section's post-write state wasn't recorded; undo by hand"
            )
        changed = WriteRefused(
            "the section changed after this write; undoing would clobber the later edits"
        )
        target = (
            {"heading": args["heading"]}
            if args.get("heading") is not None
            else {"section_text": rec["post_section_text"]}
        )
        snap = self._section_snapshot(db, note, WriteRefused)
        try:
            section = resolve_section(
                snap,
                note.name,
                heading=target.get("heading"),
                section_text=target.get("section_text"),
            )
        except SectionError:
            raise changed from None
        if section.fingerprint != rec["post_section_fingerprint"]:
            raise changed
        return target

    def undo(self, wid: str) -> str:
        """Put back what an applied write replaced (local CLI only)."""
        rec = self.queue.get(wid)
        if rec is None:
            raise WriteRefused(f"no write {wid}")
        if rec["status"] not in APPLIED_STATUSES:
            raise WriteRefused(
                f"only applied writes can be undone ({wid} is {rec['status']})"
            )
        if rec["noop"]:
            # The write changed nothing (e.g. the owner had already ticked the item):
            # undoing it must not reverse the owner's own change.
            self.queue.finish(wid, "undone", "nothing to undo: the write was a no-op")
            self._event("undo", rec, noop=True)
            return "undone"
        if rec["operation"] == "section_delete":
            raise WriteRefused(
                "Section deletes can't be undone remotely; restore from the pre-write backup."
            )
        if rec["tool"] == "delete_note":
            raise WriteRefused(
                "Deletes can't be undone here: recover the note from Recently Deleted "
                "in Notes (it stays there for 30 days)."
            )
        if rec["tool"] == "move_note":
            raise WriteRefused(
                "Moves can't be undone here: move the note back with move_note or in Notes."
            )
        if rec["post_fingerprint"] is None:
            raise WriteRefused(
                "post-write state unknown (Notes never showed the change); undo by hand"
            )
        name, tool = rec["note_title"], rec["tool"]
        with self._db() as db:
            if _title_count(db, tool, name) != 1:
                raise WriteRefused("the note's title is no longer unique; undo by hand")
            note = db.get_note_by_name(name)
            if tool == "edit_note_section":
                # Edits elsewhere in the note are fine; only the section counts.
                if note.uuid != rec["note_uuid"]:
                    raise WriteRefused(
                        "the note changed after this write; undoing would clobber the later edits"
                    )
                section_target = self._undo_section_target(db, note, rec)
            elif (
                note.uuid != rec["note_uuid"]
                or db.note_fingerprint(note.pk) != rec["post_fingerprint"]
            ):
                raise WriteRefused(
                    "the note changed after this write; undoing would clobber the later edits"
                )
            if (
                tool == "toggle_checklist_item"
                and rec["status"] == "applied_unverified"
                and rec["args"] is not None
            ):
                # Unconfirmed: the change may have been the owner's own typing, not the
                # toggle. Toggling "back" an item that never flipped would set a
                # state nobody asked for.
                state = self._checklist_state(db, note.pk, rec["args"]["item_text"])
                if state is None or state[1] != rec["desired_checked"]:
                    raise WriteRefused(
                        "the toggle was never confirmed and the item isn't in the "
                        "requested state; undo by hand"
                    )
        reason = "undone by the owner"
        if tool == "edit_note_section":
            try:
                self.stdio.edit_note_section(
                    name=name,
                    operation="replace",
                    body=rec["prior_markdown"],
                    **section_target,
                )
            except ToolError as e:
                raise WriteRefused(f"undo failed: {e}") from None
            # The engine strips trailing newlines from body (R24), so blank
            # paragraphs that ended the section are not restored: say so.
            lines = rec["prior_markdown"].split("\n")
            blanks = 0
            while lines and not lines[-1].strip():
                lines.pop()
                blanks += 1
            if blanks:
                reason = (
                    f"undone by the owner; {blanks} trailing blank "
                    f"{'line' if blanks == 1 else 'lines'} not restored"
                )
        elif tool == "add_note":
            self._delete(name)
        elif tool == "toggle_checklist_item":
            if rec["args"] is None:
                raise WriteRefused(
                    "the journal for this write was purged (older than 7 days)"
                )
            self.stdio.toggle_checklist_item(
                name=name, item_text=rec["args"]["item_text"]
            )
        elif tool == "replace_note_link":
            args = rec["args"]
            if args is None:
                raise WriteRefused(
                    "the journal for this write was purged (older than 7 days)"
                )
            if rec["relink_prior_new_count"]:
                # A reverse relink would also flip the links that already
                # pointed at the new target before this write.
                raise WriteRefused(
                    "the note already linked the new target before this write; undo by hand"
                )
            try:
                self.stdio.replace_note_link(
                    name=name,
                    old_target=args["new_target"],
                    new_target=args["old_target"],
                    replace_all=True,
                )
            except ToolError as e:
                raise WriteRefused(f"undo failed: {e}") from None
        else:
            if rec["prior_markdown"] is None:
                raise WriteRefused(
                    "the journal for this write was purged (older than 7 days)"
                )
            if rec["prior_has_attachments"]:
                raise WriteRefused(
                    "the note had attachments (images, drawings, files) before this write; a "
                    "markdown undo would turn them into labels. Restore it by hand (the "
                    "pre-write database backup is in ~/Library/Application Support/notes-graph-backup)"
                )
            self.stdio.update_note_content(
                name=name, body=body_text(rec["prior_markdown"]), operation="replace"
            )
        self.queue.finish(wid, "undone", reason)
        self._event("undo", rec)
        return "undone"


__all__ = ["WriteApplier", "WriteRefused", "db_checklist_state"]
