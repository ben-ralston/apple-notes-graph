"""Shared UI scripting orchestration for Apple Notes write operations.

Extracts the clipboard-save → UI scripting → pending-actions → clipboard-restore
pattern used by both the MCP server (local) and the UI helper daemon (remote).
"""

import time
from typing import Callable

from . import edit_lock, profiling, session_lock
from .applescript import AppleScriptExecutor
from .clipboard import ClipboardManager
from .database import NotesDatabase
from .exceptions import (
    ClipboardError,
    LinksNotConfirmedError,
    PartialWriteError,
    UIScriptingError,
    WriteVerificationError,
)
from .note_content import NoteContentReader
from .section_edit import SectionRequest, SectionResult, run_section_edit
from .ui_scripting import UIScriptingExecutor, refuse_untypeable_actions
from .write_indicator import write_indicator
from collections.abc import Mapping

from .richtext import (
    NotesRichtextParser,
    NotesRichtextSerializer,
    PendingKeyboardAction,
    checklist_item_checked,
    markdown_to_richtext_new,
    link_target_uuids,
    relink,
    toggle_checkbox,
)
from .richtext.section_fragment import LinkTarget, markdown_to_richtext_linked
from .richtext.models import NotesRichtext
from .richtext.parser import NotesRichtextParseError
from .write_verify import (
    differs,
    placeholder_count,
    unconfirmed_append,
    unconfirmed_links,
    unconfirmed_find_replace,
    unconfirmed_replace,
)


VERIFY_POLL_TIMEOUT = 2.0  # Max wall-clock wait for the read-back to confirm (#177)
VERIFY_POLL_INTERVAL = 0.2  # Delay between read-backs

Check = Callable[[NotesRichtext], list[str]]


def _parse(data) -> NotesRichtext | None:
    """Parse Notes richtext; None for missing or unparseable data."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    try:
        return NotesRichtextParser().parse(bytes(data))
    except NotesRichtextParseError:
        return None


def _read_back(
    executor: UIScriptingExecutor, clipboard: ClipboardManager
) -> tuple[NotesRichtext | None, bool]:
    """Copy the note back out of the editor: (parsed richtext | None, refused).

    Runs after the paste landed, so any UI scripting failure of the copy
    (lost the front, not answering, a timeout) is "couldn't read back", never
    an error a caller would retry — that would duplicate the write. The
    trailing Escape (closing the selection) can't discard data already read.
    Same steps as ``_copy_back_richtext``, which other callers let raise.
    """
    clipboard.clear()
    try:
        executor.select_all_and_copy()
    except WriteVerificationError:
        raise
    except UIScriptingError:
        return None, True
    try:
        data = clipboard.read_notes_richtext()
    except ClipboardError:
        data = None
    try:
        executor._send_key_code(executor.ESCAPE_KEY_CODE)
    except UIScriptingError:
        pass
    return _parse(data), False


def _unchanged_error(what: str, name: str) -> WriteVerificationError:
    return WriteVerificationError(
        f"{what} reported no error but note {name!r} is unchanged afterward "
        f"(re-checked for {VERIFY_POLL_TIMEOUT:g}s) — the write did not take effect."
    )


def _verify_content(
    executor: UIScriptingExecutor,
    clipboard: ClipboardManager,
    name: str,
    *,
    before_rt: NotesRichtext | None,
    before_html: str,
    check: Check,
    what: str,
) -> None:
    """Confirm the write's content is in the note (issues #200, #196).

    Copies the note back out of the editor and passes when ``check`` finds
    nothing unconfirmed AND the note changed. "Changed" is the copy-back's
    text or checked states differing from ``before_rt``, or else the body
    HTML differing from ``before_html`` (it sees formatting-only edits such as
    bolding a word; it can't see checked state, #196, which is why it is only
    the second signal). Otherwise:

    - nothing changed → WriteVerificationError (the write didn't land; a
      retry is safe);
    - changed, but never readable → PartialWriteError ("couldn't read back");
    - changed, but items unconfirmed → PartialWriteError naming them.

    Re-copies every VERIFY_POLL_INTERVAL until VERIFY_POLL_TIMEOUT in case the
    editor lags (issue #177); a refused copy stops the poll (each refusal can
    cost the 5s frontmost deadline). Timed as the ``verify`` span (#141).
    """
    with profiling.span("verify"):
        deadline = time.monotonic() + VERIFY_POLL_TIMEOUT
        after: NotesRichtext | None = None
        items: list[str] = []
        while True:
            got, refused = _read_back(executor, clipboard)
            if got is not None:
                after, items = got, check(got)
                if not items and (
                    differs(before_rt, got)
                    or executor._note_body_html(name) != before_html
                ):
                    return
            remaining = deadline - time.monotonic()
            if refused or remaining <= 0:
                break
            time.sleep(min(VERIFY_POLL_INTERVAL, remaining))

        changed = (after is not None and differs(before_rt, after)) or (
            executor._note_body_html(name) != before_html
        )
        if not changed:
            raise _unchanged_error(what, name)
        if after is not None and not items:
            return  # content confirmed; the change showed only in a late HTML read
        if after is None:
            raise PartialWriteError(
                f"{what} changed note {name!r}, but it couldn't be read back to "
                "confirm the new content. Don't retry the whole write; check the "
                "note."
            )
        raise PartialWriteError(
            f"{what} changed note {name!r}, but part of it isn't confirmed: "
            f"{'; '.join(items)}. Don't retry the whole write; check the note and "
            "fix what's missing."
        )


def _copy_back_richtext(
    executor: UIScriptingExecutor, clipboard: ClipboardManager
) -> bytes | None:
    """Copy the open note back out of the editor; None if the copy didn't land.

    For verifying writes body HTML can't see (links, checked state). The
    clipboard is cleared first: it still holds the payload we just pasted, so
    a copy that silently went nowhere would otherwise read back as success.
    """
    clipboard.clear()
    executor.select_all_and_copy()
    data = clipboard.read_notes_richtext()
    executor._send_key_code(executor.ESCAPE_KEY_CODE)
    return data


def _typed_actions(markdown: str) -> list[PendingKeyboardAction]:
    """The tags and tables ``markdown`` will type, refused before any GUI
    step if one can't be typed (issue #122).

    Replace and find_replace only learn their pending actions while
    converting against the copied note, after the GUI has started; the
    actions' shape depends on the markdown alone, so check it up front.
    """
    actions = markdown_to_richtext_new(markdown).pending_actions
    refuse_untypeable_actions(actions)
    return actions


def _refuse_stale_placeholder(
    before_html: str | None, typed: bool, replaced: str = ""
) -> None:
    """Refuse to type tags/tables into a note that already holds a literal
    placeholder (issue #122).

    A write that failed part way leaves one behind, and Find would select
    that first: the new tag or table would land in the old spot while this
    write's placeholder stayed literal. Called before any paste.

    ``replaced`` is find_replace's old_text: placeholders inside it go away
    with it, which is the repair this refusal asks for. Counted, not
    stripped from the HTML, so an escaped ``&`` or a paragraph break in
    old_text doesn't defeat the exemption.
    """
    if typed and placeholder_count(before_html) > placeholder_count(replaced):
        raise UIScriptingError(
            "This note already holds a literal {__NG_…__} placeholder from an "
            "earlier write that didn't finish, so a new tag or table could "
            "land in the wrong place. Remove that placeholder first, or "
            "write without tags/tables. Nothing was written."
        )


def execute_create_note(
    name: str,
    richtext_data: bytes,
    folder: str,
    pending_actions: list[PendingKeyboardAction],
) -> None:
    """Full create cycle: clipboard save, UI scripting, pending actions, restore."""
    refuse_untypeable_actions(pending_actions)  # before any paste (#212)
    with edit_lock.edit_lock():
        # Re-check inside the lock: acquiring it can wait up to ~30s, during
        # which the GUI session could become locked. Checking here keeps the
        # guard adjacent to the keystrokes (issue #96 / #90).
        session_lock.require_ui_scripting_session()
        executor = UIScriptingExecutor()
        clipboard = ClipboardManager()
        state = clipboard.save_state()
        absent = WriteVerificationError(
            f"add_note reported no error but note {name!r} does not exist "
            f"afterward — the write did not take effect."
        )
        payload = _parse(richtext_data)
        try:
            with write_indicator("add_note", name):
                executor.create_note_with_richtext(name, richtext_data, folder)
                if pending_actions:
                    executor.apply_pending_keyboard_actions(pending_actions)
                # Verify-after-write (issue #90): confirm the note now exists
                # rather than reporting success if the keystrokes landed
                # nowhere. A pre-existing same-name note can mask a failed
                # create, but the focus guard already guarantees the keystrokes
                # reached Notes, so this is a backstop.
                if not executor._note_exists(name):
                    raise absent
                if payload is not None and link_target_uuids(payload):
                    _verify_create_links(executor, clipboard, name, payload)
        finally:
            clipboard.restore_state(state)


def _verify_create_links(
    executor: UIScriptingExecutor,
    clipboard: ClipboardManager,
    name: str,
    payload: NotesRichtext,
) -> None:
    """The new note must hold every pasted link, by target uuid (issue #212).

    Only runs when the payload has links (the copy-back costs keystrokes).
    The note exists by now, so a miss is partial: a retried add_note would
    duplicate the note.
    """
    with profiling.span("verify"):
        deadline = time.monotonic() + VERIFY_POLL_TIMEOUT
        items: list[str] | None = None
        while True:
            got, refused = _read_back(executor, clipboard)
            if got is not None:
                items = unconfirmed_links(got, payload)
                if not items:
                    return
            remaining = deadline - time.monotonic()
            if refused or remaining <= 0:
                break
            time.sleep(min(VERIFY_POLL_INTERVAL, remaining))
    if items is None:
        raise LinksNotConfirmedError(
            f"add_note created note {name!r}, but it couldn't be read back to "
            "confirm its note links. Don't retry the whole write; check the note."
        )
    raise LinksNotConfirmedError(
        f"add_note created note {name!r}, but {'; '.join(items)}. Don't retry "
        "the whole write; check the note and fix the missing link(s) with "
        "update_note_content(find_replace)."
    )


def execute_append_to_note(
    name: str,
    richtext_data: bytes,
    pending_actions: list[PendingKeyboardAction],
) -> None:
    """Full append cycle: clipboard save, UI scripting, pending actions, restore.

    Verified in the editor (#200): the appended text must end the note, and
    the note must have changed (body HTML: there is no before copy-back, the
    note isn't open yet).
    """
    what = "update_note_content(append)"
    refuse_untypeable_actions(pending_actions)  # before any paste (#212)
    with edit_lock.edit_lock():
        # Re-check inside the lock: acquiring it can wait up to ~30s, during
        # which the GUI session could become locked. Checking here keeps the
        # guard adjacent to the keystrokes (issue #96 / #90).
        session_lock.require_ui_scripting_session()
        executor = UIScriptingExecutor()
        before = executor._note_body_html(name)
        _refuse_stale_placeholder(before, bool(pending_actions))
        clipboard = ClipboardManager()
        state = clipboard.save_state()
        try:
            with write_indicator(what, name):
                executor.append_richtext_to_note(name, richtext_data)
                if pending_actions:
                    executor.apply_pending_keyboard_actions(pending_actions)
                payload = _parse(richtext_data)
                _verify_content(
                    executor,
                    clipboard,
                    name,
                    before_rt=None,
                    before_html=before,
                    check=(lambda a: unconfirmed_append(a, payload))
                    if payload
                    else (lambda a: []),
                    what=what,
                )
        finally:
            clipboard.restore_state(state)


def execute_replace_note(
    name: str,
    body_markdown: str,
    links: Mapping[str, LinkTarget],
) -> None:
    """Full replace cycle: read template from clipboard, convert, paste.

    Reads the note's current richtext template via the clipboard (requires GUI
    session), converts the markdown body using that template, and pastes the
    result back. Verified in the editor (#200, #196): the body must equal what
    was pasted, checked states included, so a checkbox-only replace confirms
    even though body HTML can't show it.

    Args:
        name: The exact note title
        body_markdown: The new body content as markdown
        links: Lower-case note title → LinkTarget; those [[links]] are pasted
            as synthesized attachments (issue #212), others stay plain text
    """
    what = "update_note_content(replace)"
    # Before the GUI: the paste below replaces the body (issue #122).
    _typed_actions(body_markdown)

    with edit_lock.edit_lock():
        # Re-check inside the lock: acquiring it can wait up to ~30s, during
        # which the GUI session could become locked. Checking here keeps the
        # guard adjacent to the keystrokes (issue #96 / #90).
        session_lock.require_ui_scripting_session()
        executor = UIScriptingExecutor()
        before = executor._note_body_html(name)
        clipboard = ClipboardManager()
        state = clipboard.save_state()
        try:
            with write_indicator(what, name):
                pending_actions: list[PendingKeyboardAction] = []
                captured: dict[str, bytes] = {}

                def transform(template: bytes) -> bytes:
                    captured["before"] = template
                    result = markdown_to_richtext_linked(
                        body_markdown, template, links
                    )
                    pending_actions.extend(result.pending_actions)
                    captured["payload"] = result.data
                    return result.data

                executor.replace_note_richtext(name, transform)
                if pending_actions:
                    executor.apply_pending_keyboard_actions(pending_actions)
                payload = _parse(captured.get("payload"))
                _verify_content(
                    executor,
                    clipboard,
                    name,
                    before_rt=_parse(captured.get("before")),
                    before_html=before,
                    check=(lambda a: unconfirmed_replace(a, payload))
                    if payload
                    else (lambda a: []),
                    what=what,
                )
        finally:
            clipboard.restore_state(state)


def execute_toggle_checklist_item(name: str, item_text: str) -> None:
    """Toggle a checklist item: read richtext, parse, toggle, serialize, write back.

    Verification (issue #177): AppleScript body HTML renders a checklist item
    as a plain ``<li>`` whatever its checked state, so a body-HTML check
    can't see a toggle (it reported every landed toggle as failed, inviting a
    retry that toggled the item back). Instead, after the paste the note is
    copied again and the item's checked state checked in the editor itself,
    as ``execute_replace_note_link`` does for links — re-copied every
    VERIFY_POLL_INTERVAL until VERIFY_POLL_TIMEOUT in case the editor lags.
    """
    with edit_lock.edit_lock():
        # Re-check inside the lock: acquiring it can wait up to ~30s, during
        # which the GUI session could become locked. Checking here keeps the
        # guard adjacent to the keystrokes (issue #96 / #90).
        session_lock.require_ui_scripting_session()
        executor = UIScriptingExecutor()
        # Not used for verification (body HTML has no checked state): this
        # read raises EncryptedNoteError / NoteNotFoundError before any
        # keystrokes are sent (issue #126).
        executor._note_body_html(name)
        clipboard = ClipboardManager()
        state = clipboard.save_state()
        try:
            with write_indicator("toggle_checklist_item", name):
                parser = NotesRichtextParser()
                serializer = NotesRichtextSerializer()
                expected: bool | None = None

                def transform(template: bytes) -> bytes:
                    nonlocal expected
                    richtext = parser.parse(template)
                    toggled = toggle_checkbox(richtext, item_text)
                    expected = checklist_item_checked(toggled, item_text)
                    if expected is None:
                        raise UIScriptingError(
                            f"Could not read back the toggled state of {item_text!r} "
                            f"in note '{name}'. Nothing was written."
                        )
                    return serializer.serialize(toggled)

                executor.transform_full_note_richtext(name, transform)

                with profiling.span("verify"):
                    deadline = time.monotonic() + VERIFY_POLL_TIMEOUT
                    while True:
                        pasted = _copy_back_richtext(executor, clipboard)
                        after = (
                            checklist_item_checked(parser.parse(pasted), item_text)
                            if pasted
                            else None
                        )
                        if after == expected:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise WriteVerificationError(
                                f"toggle_checklist_item reported no error but the "
                                f"checked state of {item_text!r} in note {name!r} "
                                f"did not change — the write did not take effect."
                            )
                        time.sleep(min(VERIFY_POLL_INTERVAL, remaining))
        finally:
            clipboard.restore_state(state)


def execute_replace_note_link(
    name: str,
    old_uuid: str,
    new_uuid: str,
    new_alt: str,
    expected_count: int,
) -> None:
    """Re-point the note's links to old_uuid at new_uuid (issue #183).

    Copies the full note richtext, rewrites the matching link attachment
    entries (``richtext.relink``) and pastes it back; Notes rebuilds the links
    from the pasted fields, so there are no Cmd+K/Find keystrokes. The copied
    richtext must still hold exactly ``expected_count`` matching links (what
    the database guard saw) or nothing is pasted.

    Verification: body HTML can't see a relink (Notes renders a link as
    ``<br>`` there), so after the paste the note is copied again and its link
    targets checked in the editor itself; a paste that silently didn't land
    raises WriteVerificationError. NoteStore confirmation (which can lag) is
    the caller's job (``relink.await_relinked``).
    """
    with edit_lock.edit_lock():
        # Re-check inside the lock: acquiring it can wait up to ~30s, during
        # which the GUI session could become locked (issue #96 / #90).
        session_lock.require_ui_scripting_session()
        executor = UIScriptingExecutor()
        clipboard = ClipboardManager()
        state = clipboard.save_state()
        try:
            with write_indicator("replace_note_link", name):
                parser = NotesRichtextParser()
                serializer = NotesRichtextSerializer()
                old_key, new_key = old_uuid.upper(), new_uuid.upper()
                before_new = 0

                def transform(template: bytes) -> bytes:
                    nonlocal before_new
                    richtext = parser.parse(template)
                    targets = link_target_uuids(richtext)
                    found = targets.count(old_key)
                    before_new = targets.count(new_key)
                    if found != expected_count:
                        raise UIScriptingError(
                            f"Note '{name}' changed since it was checked: it now has "
                            f"{found} matching link(s), expected {expected_count}. "
                            "Nothing was written; retry."
                        )
                    relinked, _count = relink(richtext, old_uuid, new_uuid, new_alt)
                    return serializer.serialize(relinked)

                executor.transform_full_note_richtext(name, transform)

                with profiling.span("verify"):
                    pasted = _copy_back_richtext(executor, clipboard)
                after = link_target_uuids(parser.parse(pasted)) if pasted else []
                if (
                    old_key in after
                    or after.count(new_key) < before_new + expected_count
                ):
                    raise WriteVerificationError(
                        f"replace_note_link reported no error but the paste did not re-point "
                        f"the link(s) in note '{name}' — the write did not take effect."
                    )
        finally:
            clipboard.restore_state(state)


def execute_find_replace_multiline(
    name: str,
    old_text: str,
    replace_text: str,
    links: Mapping[str, LinkTarget] | None = None,
) -> None:
    """Find old_text in the note and replace via richtext binary manipulation.

    Uses the richtext round-trip approach: copies the entire note as richtext binary,
    performs find/replace on the text within the binary (preserving all formatting and
    table attachments), then pastes the modified richtext back.

    The replacement text is parsed as markdown — bold (**text**), italic (*text*),
    and other inline formatting are applied. Plain text replacements inherit the
    existing formatting at the replacement position.

    Args:
        name: The exact note title
        old_text: Text to find (can contain newlines)
        replace_text: Replacement text (supports markdown formatting)
        links: Optional lower-case note title → LinkTarget; those [[links]] are
            pasted as synthesized attachments (issue #212)
    """
    what = "update_note_content(find_replace)"
    typed = _typed_actions(replace_text)  # before the GUI (issue #122)
    with edit_lock.edit_lock():
        # Re-check inside the lock: acquiring it can wait up to ~30s, during
        # which the GUI session could become locked. Checking here keeps the
        # guard adjacent to the keystrokes (issue #96 / #90).
        session_lock.require_ui_scripting_session()
        executor = UIScriptingExecutor()
        before = executor._note_body_html(name)
        _refuse_stale_placeholder(before, bool(typed), replaced=old_text)
        clipboard = ClipboardManager()
        state = clipboard.save_state()
        try:
            with write_indicator(what, name):
                # Filled by the executor: the note as copied ("before") and
                # the full modified richtext it pasted ("expected").
                capture: dict[str, bytes] = {}
                count = executor._find_replace_via_notes_richtext(
                    name,
                    old_text,
                    replace_text,
                    links=links,
                    capture=capture,
                )
                if count == 0:
                    raise UIScriptingError(
                        f"find_replace made 0 replacements — old_text not found in "
                        f"note richtext. The text may be inside a table cell or "
                        f"contain formatting markers not present in the raw text."
                    )
                # count > 0 means the richtext was edited; confirm the paste
                # landed and the note now matches it (issues #90, #200).
                expected = _parse(capture.get("expected"))
                _verify_content(
                    executor,
                    clipboard,
                    name,
                    before_rt=_parse(capture.get("before")),
                    before_html=before,
                    check=(lambda a: unconfirmed_find_replace(a, expected, old_text))
                    if expected
                    else (lambda a: []),
                    what=what,
                )
        finally:
            clipboard.restore_state(state)


def execute_edit_note_section(req: SectionRequest) -> SectionResult:
    """Edit one section of a note by whole-note paste (issues #184, #240).

    The usual write lifecycle around ``section_edit.run_section_edit``, which
    does its own database verify and one-step undo. Deliberately no
    ``_verify_content``: the generic whole-note check can't tell an edit inside
    the section from damage outside it, and the engine's checks are stricter.
    """
    with edit_lock.edit_lock():
        # Re-check inside the lock (issue #96 / #90).
        session_lock.require_ui_scripting_session()
        executor = UIScriptingExecutor()
        clipboard = ClipboardManager()
        reader = NoteContentReader()

        def render(pk: int) -> list[str | None]:
            with NotesDatabase() as db:
                note = db.get_note_by_pk(pk)
            return reader.paragraph_lines(note)

        state = clipboard.save_state()
        try:
            with write_indicator("edit_note_section", req.name):
                return run_section_edit(
                    executor, clipboard, NotesDatabase, req, render=render
                )
        finally:
            clipboard.restore_state(state)


def execute_move_note(note_id: str, folder_id: str) -> None:
    """Move a note into an existing folder, both by AppleScript id (#230).

    Pure AppleScript like delete: no clipboard, keystrokes or focus, so no
    session-lock preflight, write-guard overlay or edit_lock.
    """
    AppleScriptExecutor().move_note_by_id(note_id, folder_id)


def execute_delete_note(name: str) -> None:
    """Delete a note by name (moves to Recently Deleted).

    Pure AppleScript (`tell application "Notes" ... delete`); no clipboard
    or CGEvent keystrokes. Apple Events route to Notes.app independently of
    `loginwindow`, so this works over SSH even when the GUI is locked. No
    session-lock preflight needed.
    """
    executor = UIScriptingExecutor()
    executor.delete_note(name)
