"""The section edit engine: one guarded Notes session per edit (#184, #240).

The whole note travels; only the section changes:

1. open + focus the note, copy all of it;
2. read the before-state from the database, once it matches the editor;
   re-resolve the section and check its fingerprint;
3. slice the section out of the copy, build the new content, splice it back;
4. copy again and compare (someone may have just edited the note);
5. paste the whole note over itself;
6. wait for the flush and verify: nothing outside changed at content level, the
   section matches the intent;
7. on failure, Cmd+Z exactly once and prove the whole note is back.

Undo runs only once a paste has been sent; everything before that aborts with
nothing changed. The caller holds the edit lock, the write indicator and the
clipboard save/restore (``ui_operations.execute_edit_note_section``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from . import unified_log
from .exceptions import (
    NotesNotFrontmostError,
    SectionEditDamagedError,
    SectionEditRolledBackError,
)
from .richtext import (
    MarkdownConversionError,
    NotesRichtext,
    NotesRichtextParser,
    NotesRichtextSerializer,
    link_target_uuids,
    replace_range,
    slice_richtext,
)
from .richtext.operations import LINK_UTI
from .richtext.parser import NotesRichtextParseError
from .richtext.serializer import NotesRichtextSerializeError
from .richtext.section_fragment import (
    LinkTarget,
    build_append,
    build_find_replace,
    build_replacement,
    first_paragraph,
)
from .sections import (
    EditPlan,
    MediaCapExceeded,
    NoteBusyError,
    NoteSnapshot,
    SectionError,
    content_changes,
    link_uuid,
    load_snapshot,
    note_formatting,
    plan_edit,
    resolve_section,
    richtext_formatting,
    run_style,
    section_fingerprint,
    whole_note_content_changes,
    without_final_empty_paragraph,
)

_CMD = "command down"
SELECT_ALL, COPY, PASTE, UNDO = (0, _CMD), (8, _CMD), (9, _CMD), (6, _CMD)
LEFT = (123, None)
COPY_SETTLE = 0.3  # pasteboard settle after Cmd+C (spike: copy+read ~0.5s)
COPY_POLL = 0.25
# Notes publishes a large copy late: embedded media, 3.9s at 51 MB (#240 spike).
COPY_TIMEOUT = 30.0
# Scans keep their data outside the media cap's file sizes; 25 MB x the measured
# 3.7 payload ratio.
MAX_COPY_BYTES = 100_000_000
# How long NoteStore gets to catch up with the editor before the edit is refused.
EDITOR_MATCH_TIMEOUT = 6.0
# A bigger paste takes Notes longer to save (a 51 MB note measured 2.8s).
FLUSH_BYTES_PER_SECOND = 10_000_000
# R21: verify and rollback decide on a SETTLED state, i.e. their check passes
# on two reads at least SETTLE_GAP apart (Notes can save an edit or an undo
# in two steps). The settle may run SETTLE_EXTRA past the flush budget.
SETTLE_GAP = 0.5
SETTLE_EXTRA = 1.5
_ATTACHMENT_INFO = "12"
# Diagnostic lines per log list: the vendored logger replaces an over-long
# json_extra wholesale, which would drop the event's other fields too.
MAX_LOG_LINES = 20


def _flush_window(flush_tries: int, flush_interval: float, payload_size: int) -> float:
    """Seconds Notes gets to save the paste before settling starts to count."""
    return flush_tries * flush_interval + payload_size / FLUSH_BYTES_PER_SECOND


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LOG_LINES:
        return list(lines)
    return list(lines[:MAX_LOG_LINES]) + [f"... {len(lines) - MAX_LOG_LINES} more"]


@dataclass(frozen=True)
class SectionRequest:
    name: str
    pk: int
    operation: str  # replace | append | find_replace | delete
    heading: str | None
    section_text: str | None
    body: str | None
    old_text: str | None
    new_text: str | None
    links: Mapping[str, LinkTarget]  # keyed by lower-case title
    expected_fingerprint: str
    backup_path: str


@dataclass(frozen=True)
class SectionResult:
    first_paragraph: int
    paragraph_count: int
    post_fingerprint: str | None


class _VerifyFailed(Exception):
    def __init__(self, category: str, problem: str, fields: tuple[str, ...] = ()):
        super().__init__(problem)
        self.category, self.problem = category, problem
        # Redacted "paragraph N: runs [2.3]" lines for the log (#224, #225).
        self.fields = fields


def run_section_edit(
    executor,
    clipboard,
    db_factory: Callable,
    req: SectionRequest,
    *,
    render: Callable[[int], list[str | None]],
    sleep: Callable[[float], None] = time.sleep,
    flush_tries: int = 12,
    flush_interval: float = 0.25,
) -> SectionResult:
    """Edit one section of the open note; see the module docstring."""
    req = _strip_body_newlines(req)
    name, pk = req.name, req.pk
    _refuse_blank_input(req)

    def keys(sequence, what: str) -> None:
        executor.send_section_keys(tuple(sequence), pk, what=what)

    def read_state():
        with db_factory() as db:
            return db.note_fingerprint(pk), load_snapshot(db, pk, render)

    def settle(previous: str | None, check, *, accept_unchanged: bool):
        """Poll until ``check(snapshot)`` returns None on two reads at least
        SETTLE_GAP apart. Until the fingerprint moves off ``previous``,
        states are only checked once the flush window is over, and only if
        ``accept_unchanged``. Returns (fingerprint, settled snapshot or None,
        last check failure, whether a change was seen); bounded by the flush
        window + SETTLE_EXTRA (+ one SETTLE_GAP)."""
        window = _flush_window(flush_tries, flush_interval, len(payload))
        fingerprint, changed, failure, elapsed = previous, False, None, 0.0
        while elapsed + flush_interval <= window + SETTLE_EXTRA + 1e-9:
            sleep(flush_interval)
            elapsed += flush_interval
            with db_factory() as db:
                fingerprint = db.note_fingerprint(pk)
                changed = changed or fingerprint != previous
                if not changed and not (accept_unchanged and elapsed >= window):
                    continue
                snapshot = load_snapshot(db, pk, render)
            failure = check(snapshot)
            if failure is not None:
                continue
            sleep(SETTLE_GAP)
            elapsed += SETTLE_GAP
            fingerprint, snapshot = read_state()
            failure = check(snapshot)
            if failure is None:
                return fingerprint, snapshot, None, changed
        return fingerprint, None, failure, changed

    def verify_check(snapshot):
        try:
            _verify(before, snapshot, plan, fragment)
        except _VerifyFailed as failed:
            return failed
        return None

    def copy_once(timeout: float) -> NotesRichtext | None:
        """The note as copied, or None if no readable copy appears in time."""
        clipboard.clear()
        keys([SELECT_ALL, COPY], "copy the note")
        sleep(COPY_SETTLE)
        waited, data = COPY_SETTLE, clipboard.read_notes_richtext()
        while not data and waited < timeout:
            sleep(COPY_POLL)
            waited += COPY_POLL
            data = clipboard.read_notes_richtext()
        # Don't leave the note selected while the edit is prepared: one stray
        # keypress would replace it.
        keys([LEFT], "collapse the selection")
        if data and len(data) > MAX_COPY_BYTES:
            raise MediaCapExceeded(
                f"'{name}' is too large for a section edit: it copies as "
                f"{len(data) / 1e6:.0f} MB of images and files. Use "
                "update_note_content with operation 'append' to add to the end of "
                "the note. Nothing was changed."
            )
        try:
            return NotesRichtextParser().parse(data) if data else None
        except NotesRichtextParseError:
            return None

    def copy_note() -> NotesRichtext:
        # A large note's copy sometimes never publishes (about 1 in 10 at 52 MB).
        copied = copy_once(COPY_TIMEOUT / 2) or copy_once(COPY_TIMEOUT / 2)
        if copied is None:
            raise NoteBusyError(
                f"'{name}' couldn't be read from Notes. Nothing was changed."
            )
        return copied

    def read_before(copied: NotesRichtext) -> tuple[str, NoteSnapshot]:
        """The before-state once it holds still and matches the editor. Read
        after open + focus (Notes normalizes a note when it displays it), and
        polled because NoteStore saves lazily: resolving the section against a
        state the editor has moved past would write over someone's edit."""
        elapsed, stable = 0.0, False
        while True:
            with db_factory() as db:
                fp = db.note_fingerprint(pk)
                snapshot = load_snapshot(db, pk, render)
                stable = db.note_fingerprint(pk) == fp
            if stable and _editor_matches(copied, snapshot):
                return fp, snapshot
            if elapsed >= EDITOR_MATCH_TIMEOUT:
                break
            sleep(flush_interval)
            elapsed += flush_interval
        if not stable:
            raise NoteBusyError(
                f"Note '{name}' kept changing while it was read; nothing was "
                "written. Retry in a moment."
            )
        raise NoteBusyError(
            f"Notes hasn't saved '{name}' yet; nothing was written. Retry in a moment."
        )

    executor.open_note(name)
    sleep(executor.NOTE_LOAD_DELAY)
    executor._focus_note_content()
    # A key send can fail with the whole note still selected: any refusal from
    # here until the paste collapses the selection.
    try:
        first = copy_note()
        seen, before = read_before(first)
        section = resolve_section(
            before, name, heading=req.heading, section_text=req.section_text
        )
        if section.fingerprint != req.expected_fingerprint:
            raise SectionError(
                f"Section in '{name}' changed since it was checked; nothing was "
                "written. Read the note again and retry."
            )
        plan = plan_edit(before, section, req.operation, name)
        if plan.keep_first and req.operation == "find_replace":
            raise SectionError("old_text not found in the section.")
        second = copy_note()
        if _copy_content(first) != _copy_content(second):
            raise NoteBusyError(
                f"'{name}' changed while the edit was being prepared; nothing was "
                "written. Retry in a moment."
            )
        # From the later copy: an edit the comparison can't see (a table cell's
        # text is U+FFFC here) must travel in the paste, never be reverted by it.
        payload, fragment = _build_payload(second, plan, req)
        clipboard.write_notes_richtext(payload)
    except BaseException:
        _collapse(keys)
        raise

    delete = req.operation == "delete"
    # Only a send that returned proves the key landed; if the send itself
    # raised, Cmd+V may never have run and Cmd+Z may undo an older edit.
    sent = refused = False
    try:
        try:
            # The guards (frontmost, open dialog #199, note identity) run BEFORE
            # each key, so a refusal from them proves Cmd+V never ran. Nothing
            # changed; an undo now could only hit an older edit (or the owner's own
            # typing), so collapse and re-raise. Select-all goes first in the
            # same script: a click since the copy must not turn the paste into
            # an insert.
            keys([SELECT_ALL, PASTE], "paste the note")
        except NotesNotFrontmostError:  # includes WrongNoteOpenError
            refused = True
            _collapse(keys)
            raise
        sent = True
        seen, after, failure, changed = settle(
            seen, verify_check, accept_unchanged=False
        )
        if not changed:
            raise _VerifyFailed(
                "flush_timeout",
                "Notes didn't save the edit within "
                f"{_flush_window(flush_tries, flush_interval, len(payload)) + SETTLE_EXTRA:.1f}s",
            )
        if after is None:
            raise failure
    except Exception as exc:
        if refused:
            raise
        _roll_back(keys, settle, req, before, seen, exc, sent)
    except BaseException:
        # Interrupted after the destructive key: still try the one undo,
        # then re-raise the interrupt itself, unchanged. Never after a
        # refusal: the key provably didn't run.
        if refused:
            raise
        try:
            _roll_back(keys, settle, req, before, seen, None, sent)
        except Exception:
            pass
        raise

    if delete:
        return SectionResult(plan.edit_a, 0, None)
    count = fragment.text.count("\n") + 1
    return SectionResult(
        plan.edit_a, count, section_fingerprint(after, plan.edit_a, plan.edit_a + count)
    )


def _strip_body_newlines(req: SectionRequest) -> SectionRequest:
    """R24: a body's trailing newlines would paste an extra blank paragraph
    (the fragment's own empty last line), so they go before conversion.
    find_replace's new_text is kept as given."""
    if req.operation in ("replace", "append") and req.body:
        body = req.body
        while body.endswith(("\n", "\r")):
            body = body[:-1]
        return replace(req, body=body)
    return req


def _refuse_blank_input(req: SectionRequest) -> None:
    """Blank content would paste nothing; a no-op paste followed by the
    failure path's Cmd+Z would undo someone else's edit."""
    if req.operation in ("replace", "append") and not (req.body or "").strip():
        raise SectionError(
            "body can't be blank; use operation 'delete' to remove a section."
        )
    if req.operation == "find_replace" and not (req.old_text or "").strip():
        raise SectionError("old_text can't be blank.")


def _collapse(keys) -> None:
    """Best-effort: a selection left live would let the next keypress replace
    the note. Never masks the error being raised."""
    try:
        keys([LEFT], "collapse the selection")
    except Exception:
        pass


def _str(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _editor_matches(copied: NotesRichtext, snapshot: NoteSnapshot) -> bool:
    """Does the database hold what the editor shows: text, paragraph styles,
    formatting and link targets in order? The title's kind isn't compared: an unstyled
    first line reads as body in one and title in the other."""
    if copied.text != "".join(p.text for p in snapshot.paragraphs):
        return False
    styles, links = _fragment_styles_and_links(copied)
    if styles[1:] != [(p.kind, p.indent, p.checked) for p in snapshot.paragraphs[1:]]:
        return False
    if richtext_formatting(copied) != note_formatting(snapshot):
        return False
    return links == [
        link_uuid(att) for p in snapshot.paragraphs for att in p.atts if att.uti == LINK_UTI
    ]


def _copy_content(richtext: NotesRichtext) -> tuple:
    """A copy, ignoring what differs between two copies of an unchanged note:
    their raw bytes never match (issue #240 spike)."""
    return (
        richtext.text,
        richtext_formatting(richtext),
        _fragment_styles_and_links(richtext),
    )


def _build_payload(
    copied: NotesRichtext, plan: EditPlan, req: SectionRequest
) -> tuple[bytes, NotesRichtext | None]:
    """The whole note with the section changed, and the new section's richtext
    (None for delete). Serialized and re-parsed here: nothing is sent yet."""
    name, op, links = req.name, req.operation, req.links
    serializer = NotesRichtextSerializer()
    try:
        fragment = None
        if op == "delete":
            changed = slice_richtext(copied, 0, 0)
        else:
            section = serializer.serialize(slice_richtext(copied, plan.start, plan.end))
            if op == "find_replace":
                data = build_find_replace(section, req.old_text, req.new_text, links)
            elif op == "append" or plan.keep_first:
                base = first_paragraph(section) if op == "replace" else section
                data = build_append(base, req.body, links)
            else:
                data = build_replacement(section, req.body, links)
            fragment = changed = NotesRichtextParser().parse(data)
        payload = serializer.serialize(replace_range(copied, plan.start, plan.end, changed))
        NotesRichtextParser().parse(payload)
    except (
        NotesRichtextParseError,
        NotesRichtextSerializeError,
        MarkdownConversionError,
    ) as exc:
        raise SectionError(
            f"Couldn't build the edit for '{name}': the new content didn't "
            "round-trip as Notes richtext. Nothing was changed."
        ) from exc
    if fragment is not None and not fragment.text:
        # e.g. find_replace of the section's whole text with "": that's a
        # delete, and the caller should say so.
        raise SectionError(
            f"That edit would leave the section in '{name}' empty; use "
            "operation 'delete' instead. Nothing was changed."
        )
    return payload, fragment


def _fragment_styles_and_links(fragment: NotesRichtext) -> tuple[list, list]:
    """Per-paragraph (kind, indent, checked) from each paragraph's first run,
    like ``load_snapshot``, and the link target uuids in text order (None for
    a link whose target can't be read, matching ``link_uuid``)."""
    link_atts = [a for a in fragment.attachments if a.type_uti == LINK_UTI]
    targets = {}
    for att, target in zip(link_atts, link_target_uuids(fragment)):
        targets[_str(att.raw_data.get("2") or att.uuid)] = target.lower() or None
    kinds, links, pos = [], [], 0
    spans, start = [], 0
    for run in fragment.runs:
        spans.append((start, start + run.length, run))
        start += run.length
    for line in fragment.text.split("\n"):
        lo, hi = pos, pos + len(line) + 1
        first = next((r for s, e, r in spans if s < hi and e > lo), None)
        kinds.append(run_style(first.raw_data if first is not None else None))
        pos = hi
    for _s, _e, run in spans:
        info = run.raw_data.get(_ATTACHMENT_INFO) if run.raw_data else None
        if isinstance(info, dict) and _str(info.get("2")) == LINK_UTI:
            links.append(targets.get(_str(info.get("1"))))
    return kinds, links


def _verify(
    before: NoteSnapshot, after: NoteSnapshot, plan: EditPlan, fragment
) -> None:
    """Raise _VerifyFailed unless the outside is unchanged at content level and
    the edit range holds exactly the fragment (or is gone, for delete)."""
    a, b = plan.edit_a, plan.edit_b
    n = len(before.paragraphs)
    k = 0 if fragment is None else fragment.text.count("\n") + 1
    want = n - (b - a) + k
    # R22: the one tolerance. A paste that ends in a list or checklist leaves
    # exactly one extra EMPTY final paragraph with no runs. Every paste reaches
    # the note's end now, so it can follow any edit, unless the note already
    # ended blank.
    if len(after.paragraphs) == want + 1 and before.paragraphs[-1].text:
        last = after.paragraphs[-1]
        # `not last.run_sigs` is always true for a zero-length final paragraph
        # (load_snapshot gives it no runs); the real guard is `not last.text`.
        if not last.text and not last.run_sigs and not last.atts:
            after = without_final_empty_paragraph(after)
    diff = content_changes(
        before, after, a, b, prev_newline_removed=plan.prev_newline_removed
    )
    if diff:
        raise _VerifyFailed(
            "outside_changed",
            f"outside the section changed: {diff[0]}",
            tuple(c.redacted() for c in diff),
        )
    if len(after.paragraphs) != want:
        raise _VerifyFailed(
            "count_mismatch",
            f"the note has {len(after.paragraphs)} paragraphs, expected {want}",
        )
    if fragment is None:
        return
    got = after.paragraphs[a : a + k]
    ends_line = before.paragraphs[b - 1].text.endswith("\n")
    tail = "\n" if ends_line else ""
    if "".join(p.text for p in got) != fragment.text + tail:
        raise _VerifyFailed(
            "intent_mismatch", "the section's text doesn't match the edit"
        )
    styles, links = _fragment_styles_and_links(fragment)
    if [(p.kind, p.indent, p.checked) for p in got] != styles:
        raise _VerifyFailed(
            "intent_mismatch", "the section's paragraph styles don't match the edit"
        )
    got_links = [link_uuid(att) for p in got for att in p.atts if att.uti == LINK_UTI]
    if got_links != links:
        raise _VerifyFailed(
            "intent_mismatch", "the section's link targets don't match the edit"
        )


def _roll_back(keys, settle, req, before, seen, exc, sent) -> None:
    """Cmd+Z once, then prove the whole note is back; always raises.

    "Rolled back" needs an undo key sent without error and a post-undo state
    equal to the before-state on two reads SETTLE_GAP apart (R21: Notes can
    save the undo in two steps). A post-undo poll that sees no new save counts
    as that state only when the destructive key's send returned (``sent``):
    Notes then holds paste + undo in order. If that send raised, Cmd+V may
    never have landed and the undo may have hit an older edit, so only an
    observed save that equals the before-state counts; anything less is
    reported as an unknown outcome."""
    outside_fields: list[str] = []
    if isinstance(exc, _VerifyFailed):
        category, problem, cause = exc.category, exc.problem, None
        outside_fields = list(exc.fields)
    elif exc is None:
        category, problem, cause = "interrupted", "interrupted", None
    else:
        category, problem, cause = (
            "exception",
            f"{type(exc).__name__} after the paste",
            exc,
        )
    undo_error = None
    # "not_read" unless a post-undo state was actually compared.
    undo_diff_fields: list[str] = ["not_read"]
    try:
        keys([UNDO], "undo the section edit")
    except Exception as error:
        undo_error = error
    if undo_error is None:
        def unchanged(snapshot):
            changes = whole_note_content_changes(before, snapshot)
            return [c.redacted() for c in changes] or None

        try:
            _, now, still, _ = settle(seen, unchanged, accept_unchanged=sent)
            restored = now is not None
            if not restored and isinstance(still, list):
                undo_diff_fields = still
        except Exception:
            restored = False
        # Undoing a paste re-selects what it restored; the next keypress
        # would replace the note.
        _collapse(keys)
        if restored:
            raise SectionEditRolledBackError(
                f"Section edit in '{req.name}' failed verification ({problem}); "
                "it was undone and the note is unchanged."
            ) from cause
    # Type info only: never titles, note text or paths (#170 redaction).
    unified_log.log_error(
        "edit_note_section: undo did not restore the note",
        {
            "action": "edit_note_section",
            "operation": req.operation,
            "failure": category,
            "cause_type": type(cause).__name__ if cause else None,
            "undo_error_type": type(undo_error).__name__ if undo_error else None,
            "destructive_key_confirmed": sent,
            # Paragraph indices + run field paths only, never text (#224, #225).
            "outside_fields": _capped(outside_fields),
            "undo_diff_fields": _capped(undo_diff_fields),
        },
    )
    if undo_error is not None or not sent:
        why = "the undo couldn't be sent" if undo_error else "its undo couldn't be confirmed"
        raise SectionEditDamagedError(
            f"Section edit in '{req.name}' failed verification ({problem}) and "
            f"{why}, so the outcome is unknown. The note may be damaged; "
            f"restore from the backup: {req.backup_path}."
        ) from cause
    raise SectionEditDamagedError(
        f"Section edit in '{req.name}' failed verification and undo did not restore "
        f"it ({problem}). The note may be damaged; restore from the backup: "
        f"{req.backup_path}."
    ) from cause
