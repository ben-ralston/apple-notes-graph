"""Section edit engine (issues #184, #240): keys, lost-update checks, verify, one undo.

Headless and CI-visible. A fake executor edits an in-memory paragraph model
(whole-note copy and paste through real Notes richtext bytes, re-stamping every
paragraph and re-minting ids as Notes does); a fake database encodes that model
as real ZDATA, so ``load_snapshot``, ``content_changes`` and the fragment builders
all run for real. No keystrokes are sent and the real NoteStore is never touched.
"""

import copy
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apple_notes import section_edit, ui_operations
from apple_notes.exceptions import (
    SectionEditDamagedError,
    SectionEditRolledBackError,
    WrongNoteOpenError,
)
from apple_notes.richtext import (
    NotesRichtextParser,
    NotesRichtextSerializer,
    link_target_uuids,
    markdown_to_richtext,
)
from apple_notes.richtext.section_fragment import LinkTarget, synthesize_link
from apple_notes.section_edit import SectionRequest, SectionResult, run_section_edit
from apple_notes.sections import (
    MediaCapExceeded,
    NoteBusyError,
    SectionError,
    load_snapshot,
    resolve_section,
)
from section_fakes import LINK_UTI, encode_note, para

TEMPLATE = (
    Path(__file__).parent / "fixtures" / "notes_richtext_sample_with_great_sentence.bin"
).read_bytes()
NAME = "NG Section Test Engine"
PK = 4242
CMD = "command down"
SELECT_ALL, COPY, PASTE, UNDO = (0, CMD), (8, CMD), (9, CMD), (6, CMD)
LEFT = (123, None)
UUID = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"
OTHER_UUID = "0A0A0A0A-1111-2222-3333-444444444444"
LINKS = {"target": LinkTarget(UUID, "Target", "_51e1")}
PREFIX = {1: "## ", 2: "### ", 103: "- [ ] "}


def _prefix(p):
    if p["ptype"] == 103 and p.get("checked"):
        return "- [x] "
    return PREFIX.get(p["ptype"], "")


BACKUP = "/tmp/ng-backups/NoteStore-20261030.sqlite"
_PARSER = NotesRichtextParser()


def _std_paras():
    return [
        para("Engine scratch title", 0),
        para("Alpha", 1),
        para("first body line"),
        para("second body line"),
        para("Beta", 1),
        para("beta body text"),
    ]


def _blank_paras():
    return [
        para("Engine scratch title", 0),
        para("Blank heading", 1),
        para(""),
        para("Next", 1),
        para("next body text"),
    ]


class FakeNote:
    """Notes' live editor state, and what it has saved to the 'database'."""

    def __init__(self, paras, atts=None):
        self.live = copy.deepcopy(paras)
        self.atts = dict(atts or {})
        self.version = 0
        self.flushes = True
        self.saved = (copy.deepcopy(self.live), dict(self.atts), 0)
        # Staged saves: [reads_left, paras, atts]; the head one lands on the
        # fingerprint read that takes reads_left to 0 (a save in two steps).
        self.later = []

    def tick(self):
        if self.later:
            self.later[0][0] -= 1
            if self.later[0][0] <= 0:
                _, paras, atts = self.later.pop(0)
                self.change(paras, atts)

    def change(self, paras, atts=None):
        self.version += 1
        self.live, self.atts = (
            copy.deepcopy(paras),
            dict(atts if atts is not None else self.atts),
        )
        if self.flushes:
            self.saved = (copy.deepcopy(self.live), dict(self.atts), self.version)

    def saved_texts(self):
        return [p["text"] for p in self.saved[0]]

    def render(self, pk):
        return [_prefix(p) + p["text"] for p in self.saved[0]]


class FakeNoteDB:
    """The NotesDatabase reads the engine makes, served from ``FakeNote.saved``."""

    def __init__(self, note, fp_hook=None):
        self.note = note
        self.fp_hook = fp_hook  # runs before each fingerprint read

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def note_fingerprint(self, pk):
        if self.fp_hook:
            self.fp_hook()
        self.note.tick()
        return f"v{self.note.saved[2]}"

    def get_note_data(self, pk):
        return encode_note(self.note.saved[0])

    def get_attachments_for_note(self, pk):
        return self.note.saved[1]

    def get_block_attachment_sizes(self, pk):
        return {}

    def get_table_data(self, identifier):
        return None


class FakeClipboard:
    def __init__(self, log):
        self.log = log
        self.contents = b"user clipboard"
        self.writes = []
        self.hold = 0

    def clear(self):
        self.log.append(("clear",))
        self.contents = None

    def read_notes_richtext(self):
        if self.hold > 0:  # Notes publishes a large copy late
            self.hold -= 1
            return None
        return self.contents

    def write_notes_richtext(self, data):
        self.writes.append(data)
        self.contents = data


def _style_of(run):
    """(ptype, checked, indent) of a fragment run, as para() takes them."""
    style = run.raw_data.get("2") if run is not None and run.raw_data else None
    if not isinstance(style, dict):
        return None, None, None
    check = style.get("5")
    checked = bool(check.get("2", 0)) if isinstance(check, dict) else None
    return style.get("1"), checked, style.get("4") or None


class FakeExecutor:
    """Records calls; models select-all, copy, whole-note paste and undo."""

    NOTE_LOAD_DELAY = 1.0

    def __init__(self, note, clipboard, log):
        self.note, self.clip, self.log = note, clipboard, log
        self.selected = False
        self.copy_override = "real"  # bytes / None served by Cmd+C instead
        self.copies = 0
        self.on_copy = None  # callable(copy_number): change the note before a copy
        self.on_paste = None  # callable(paras) -> mutate the pasted result
        self.raise_on = {}  # key -> exception raised after that key ran
        self.raise_before = {}  # key -> exception raised before it lands
        self.sends = []  # each send_section_keys call's keys, as one tuple
        self.undo_restores = True
        self.on_undo = None  # callable(paras) -> mutate the restored state
        self.after_paste = None  # callable(paras) -> a later second save
        self.undo_first = None  # callable(paras) -> an intermediate undo save
        self.undo_state = None

    def open_note(self, name):
        self.log.append(("open", name))

    def _focus_note_content(self):
        self.log.append(("focus",))

    def send_section_keys(self, keys, expect_pk, *, what):
        assert expect_pk == PK
        self.sends.append(tuple(keys))
        for key in keys:
            self.log.append(("key", key))
            if key in self.raise_before:
                raise self.raise_before[key]
            if key == SELECT_ALL:
                self.selected = True
            elif key == LEFT:
                self.selected = False
            elif key == COPY:
                self.copies += 1
                if self.on_copy:
                    self.on_copy(self.copies)
                self.clip.contents = (
                    self._copy() if self.copy_override == "real" else self.copy_override
                )
            elif key == PASTE:
                assert self.selected, "a paste with no selection would insert a copy"
                self._paste()
            elif key == UNDO:
                if self.undo_restores and self.undo_state is not None:
                    paras, atts = copy.deepcopy(self.undo_state)
                    if self.on_undo:
                        self.on_undo(paras)
                    if self.undo_first:
                        first = copy.deepcopy(paras)
                        self.undo_first(first)
                        self.note.change(first, atts)
                        self.note.later.append([2, paras, atts])
                    else:
                        self.note.change(paras, atts)
                    # Undoing a paste over a selection re-selects what it restored.
                    self.selected = True
            else:
                raise AssertionError(f"unexpected key {key}")
            if key in self.raise_on:
                raise self.raise_on[key]

    # -- model -------------------------------------------------------------
    def _copy(self):
        md_lines, targets, n = [], [], 0
        for p in self.note.live:
            ids = iter(p["atts"])
            out = ""
            for ch in p["text"]:
                if ch == "￼":
                    ident, _uti = next(ids)
                    out += "{__NG_LINK_%d__}" % n
                    targets.append((n, self.note.atts[ident]))
                    n += 1
                else:
                    out += ch
            md_lines.append(_prefix(p) + out if out else out)
        richtext = _PARSER.parse(
            markdown_to_richtext("\n".join(md_lines), TEMPLATE).data
        )
        for k, row in targets:
            uuid = re.search(r"([0-9A-Fa-f-]{36})", row["target_url"]).group(1)
            richtext = synthesize_link(
                richtext,
                "{__NG_LINK_%d__}" % k,
                LinkTarget(uuid, row["alt_text"], "_o"),
            )
        return NotesRichtextSerializer().serialize(richtext)

    def _paste(self):
        frag = _PARSER.parse(self.clip.contents)
        link_ids = [a for a in frag.attachments if a.type_uti == LINK_UTI]
        targets = {
            (a.raw_data.get("2") or a.uuid.decode()): u
            for a, u in zip(link_ids, link_target_uuids(frag))
        }
        atts = dict(self.note.atts)
        paras, pos, run_pos = [], 0, []
        acc = 0
        for run in frag.runs:
            run_pos.append((acc, acc + run.length, run))
            acc += run.length
        stamp = 50 + self.note.version  # every paragraph re-stamped, as in Notes
        for line in frag.text.split("\n"):
            lo, hi = pos, pos + len(line) + 1
            runs = [r for s, e, r in run_pos if s < hi and e > lo]
            ptype, checked, indent = _style_of(runs[0] if runs else None)
            p = para(line, ptype, ts=stamp, checked=checked, indent=indent)
            for s, e, r in run_pos:
                info = r.raw_data.get("12") if r.raw_data else None
                if lo <= s < lo + len(line) and isinstance(info, dict):
                    ident, uti = info["1"], info["2"]
                    p["atts"].append((ident, uti))
                    if uti == LINK_UTI:
                        atts[ident] = {
                            "type_uti": LINK_UTI,
                            "target_url": f"applenotes:note/{targets[ident].lower()}",
                            "alt_text": "Target",
                        }
            paras.append(p)
            pos = hi
        self.undo_state = (copy.deepcopy(self.note.live), dict(self.note.atts))
        self.selected = False
        if self.on_paste:
            self.on_paste(paras)
        self.note.change(paras, atts)
        if self.after_paste:
            second = copy.deepcopy(paras)
            self.after_paste(second)
            self.note.later.append([2, second, atts])

    def keys(self):
        return [entry[1] for entry in self.log if entry[0] == "key"]


class Rig:
    def __init__(self, paras=None, atts=None):
        self.log = []
        self.note = FakeNote(paras or _std_paras(), atts)
        self.clip = FakeClipboard(self.log)
        self.ex = FakeExecutor(self.note, self.clip, self.log)
        self.sleeps = []
        self.fp_hook = None

    def db_factory(self):
        self.log.append(("db",))
        return FakeNoteDB(self.note, self.fp_hook)

    def request(self, operation, heading="Alpha", **fields):
        snap = load_snapshot(FakeNoteDB(self.note), PK, self.note.render)
        section = resolve_section(snap, NAME, heading=heading, section_text=None)
        return SectionRequest(
            name=NAME,
            pk=PK,
            operation=operation,
            heading=heading,
            section_text=None,
            body=fields.get("body"),
            old_text=fields.get("old_text"),
            new_text=fields.get("new_text"),
            links=LINKS,
            expected_fingerprint=section.fingerprint,
            backup_path=BACKUP,
        )

    def run(self, req, **kw):
        return run_section_edit(
            self.ex, self.clip, self.db_factory, req,
            render=self.note.render, sleep=self.sleeps.append, **kw,
        )  # fmt: skip


def _no_destructive(keys):
    return not {PASTE, UNDO} & set(keys)


def _written(rig):
    """The one payload the engine put on the pasteboard, as text."""
    (payload,) = rig.clip.writes
    return _PARSER.parse(payload).text


# --- happy paths --------------------------------------------------------------


def test_replace_happy_path_key_order():
    rig = Rig()
    result = rig.run(rig.request("replace", body="new line one\nnew line two"))
    assert rig.ex.sends == [
        (SELECT_ALL, COPY), (LEFT,), (SELECT_ALL, COPY), (LEFT,), (SELECT_ALL, PASTE),
    ]  # fmt: skip
    assert rig.log.index(("clear",)) < rig.log.index(("key", COPY))
    assert rig.note.saved_texts() == [
        "Engine scratch title", "Alpha", "new line one", "new line two", "Beta", "beta body text",
    ]  # fmt: skip
    assert result.first_paragraph == 2 and result.paragraph_count == 2
    assert result.post_fingerprint
    assert rig.sleeps[0] == FakeExecutor.NOTE_LOAD_DELAY


def test_note_is_not_left_selected_while_the_edit_is_prepared():
    """One stray keypress would replace a selected note; the NoteStore wait and
    the build can take seconds."""
    rig = Rig()
    selected_during_db_reads = []

    def watch():
        selected_during_db_reads.append(rig.ex.selected)

    req = rig.request("replace", body="replacement text")
    rig.fp_hook = watch
    original = rig.ex.send_section_keys

    def stop_watching(keys, pk, *, what):
        if PASTE in keys:
            rig.fp_hook = None
        original(keys, pk, what=what)

    rig.ex.send_section_keys = stop_watching
    rig.run(req)
    assert selected_during_db_reads and not any(selected_during_db_reads)


def test_paste_replaces_the_whole_note_payload():
    rig = Rig()
    rig.run(rig.request("replace", body="new line one"))
    assert _written(rig) == (
        "Engine scratch title\nAlpha\nnew line one\nBeta\nbeta body text"
    )


def test_before_state_read_after_focus():
    rig = Rig()
    rig.run(rig.request("replace", body="replacement text"))
    names = [entry[0] for entry in rig.log]
    assert names.index("open") < names.index("focus") < names.index("db")


def test_section_changed_since_check_refused_before_keys():
    rig = Rig()
    req = rig.request("replace", body="x y z")
    paras = _std_paras()
    paras[3] = para("second body line edited elsewhere")
    rig.note.change(paras)
    with pytest.raises(SectionError) as err:
        rig.run(req)
    assert str(err.value) == (
        f"Section in '{NAME}' changed since it was checked; nothing was written. "
        "Read the note again and retry."
    )
    assert _no_destructive(rig.ex.keys()) and rig.ex.keys()[-1] == LEFT


def test_delete_pastes_the_note_without_the_section():
    rig = Rig()
    result = rig.run(rig.request("delete"))
    keys = rig.ex.keys()
    assert keys[-1] == PASTE and UNDO not in keys
    assert _written(rig) == "Engine scratch title\nAlpha\nBeta\nbeta body text"
    assert rig.note.saved_texts() == [
        "Engine scratch title",
        "Alpha",
        "Beta",
        "beta body text",
    ]
    assert result == SectionResult(2, 0, None)


def test_delete_section_at_note_end():
    rig = Rig()
    result = rig.run(rig.request("delete", heading="Beta"))
    assert rig.ex.keys()[-1] == PASTE
    assert rig.note.saved_texts() == [
        "Engine scratch title", "Alpha", "first body line", "second body line", "Beta",
    ]  # fmt: skip
    assert result == SectionResult(5, 0, None)


def test_delete_leaving_a_blank_line_rolls_back():
    rig = Rig()
    rig.ex.on_paste = lambda paras: paras.insert(2, para(""))
    with pytest.raises(SectionEditRolledBackError, match="paragraph"):
        rig.run(rig.request("delete"))
    assert rig.ex.keys().count(UNDO) == 1
    assert rig.note.saved_texts() == [p["text"] for p in _std_paras()]


def test_append_reports_edit_range_and_verifies_kept_paragraph():
    rig = Rig()
    result = rig.run(rig.request("append", body="third body line"))
    assert rig.note.saved_texts()[2:5] == [
        "first body line",
        "second body line",
        "third body line",
    ]
    assert result.first_paragraph == 3 and result.paragraph_count == 2

    # The kept paragraph is inside the edit range: a change to it fails intent.
    rig = Rig()
    rig.ex.on_paste = lambda paras: paras[3].update(text="second body LINE")
    with pytest.raises(SectionEditRolledBackError, match="text"):
        rig.run(rig.request("append", body="third body line"))
    assert rig.ex.keys().count(UNDO) == 1


def test_find_replace_splices_into_the_copied_note():
    rig = Rig()
    result = rig.run(rig.request("find_replace", old_text="second", new_text="2nd"))
    assert "first body line\n2nd body line\nBeta" in _written(rig)
    assert rig.note.saved_texts()[2:4] == ["first body line", "2nd body line"]
    assert result.first_paragraph == 2 and result.paragraph_count == 2


def test_replace_with_new_link_verifies_target():
    rig = Rig()
    rig.run(rig.request("replace", body="see [[Target]] now"))
    paras, atts, _ = rig.note.saved
    (ident, uti) = paras[2]["atts"][0]
    assert uti == LINK_UTI
    assert atts[ident]["target_url"] == f"applenotes:note/{UUID.lower()}"


def test_explicit_body_style_counts_as_body():
    rig = Rig()

    def explicit(paras):
        for p in paras[2:4]:
            p["ptype"] = 0

    rig.ex.on_paste = explicit
    rig.run(rig.request("replace", body="new line one\nnew line two"))
    assert rig.ex.keys().count(UNDO) == 0


# --- #240: the misplacement this design removes -------------------------------


def _games_paras():
    """The #240 layout: link-only lines, one paragraph with a soft break."""
    return [
        para("Games", 0),
        para("￼", atts=[("H", LINK_UTI)]),
        para(""),
        para("￼", atts=[("B", LINK_UTI)]),
        para("￼\r￼", atts=[("S", LINK_UTI), ("D", LINK_UTI)]),
        para("￼", atts=[("L", LINK_UTI)]),
    ]


def _games_atts():
    uuid = lambda n: f"{n:08d}-0000-0000-0000-000000000000"  # noqa: E731
    token = "applenotes://showNote?identifier={}"
    return {
        "H": {"type_uti": LINK_UTI, "target_url": f"applenotes:note/{uuid(1)}", "alt_text": "Home"},
        "B": {"type_uti": LINK_UTI, "target_url": f"applenotes:note/{uuid(2)}", "alt_text": "Blue"},
        "S": {"type_uti": LINK_UTI, "target_url": token.format(uuid(3)), "alt_text": "Spirit"},
        "D": {"type_uti": LINK_UTI, "target_url": token.format(uuid(4)), "alt_text": "Duck"},
        "L": {"type_uti": LINK_UTI, "target_url": token.format(uuid(5)), "alt_text": "Last"},
    }  # fmt: skip


def _games_request(rig, operation="append", **fields):
    snap = load_snapshot(FakeNoteDB(rig.note), PK, lambda pk: [f"line {i}" for i in range(6)])
    section = resolve_section(snap, NAME, heading=None, section_text="line 3")
    return SectionRequest(
        name=NAME, pk=PK, operation=operation, heading=None, section_text="line 3",
        body=fields.get("body"), old_text=None, new_text=None, links=LINKS,
        expected_fingerprint=section.fingerprint, backup_path=BACKUP,
    )  # fmt: skip


def _games_rig():
    rig = Rig(_games_paras(), _games_atts())
    rig.note.render = lambda pk: [f"line {i}" for i in range(len(rig.note.saved[0]))]
    return rig


def _targets(rig):
    paras, atts, _ = rig.note.saved
    return [re.search(r"([0-9a-f-]{36})", atts[i]["target_url"]).group(1)[:8]
            for p in paras for i, _uti in p["atts"]]  # fmt: skip


def test_append_above_soft_break_paragraph_lands_in_place():
    rig = _games_rig()
    result = rig.run(_games_request(rig, body="[[Target]]"))
    assert UNDO not in rig.ex.keys()
    assert [p["text"] for p in rig.note.saved[0]] == [
        "Games", "￼", "", "￼", "￼", "￼\r￼", "￼",
    ]  # fmt: skip
    assert result.first_paragraph == 3 and result.paragraph_count == 2


def test_section_with_hand_made_links_outside_keeps_their_targets():
    rig = _games_rig()
    rig.run(_games_request(rig, body="[[Target]]"))
    assert _targets(rig) == [
        "00000001", "00000002", UUID[:8].lower(), "00000003", "00000004", "00000005",
    ]  # fmt: skip


def test_replacing_a_link_only_line_replaces_that_line():
    rig = _games_rig()
    rig.run(_games_request(rig, "replace", body="plain words"))
    assert [p["text"] for p in rig.note.saved[0]][3:5] == ["plain words", "￼\r￼"]
    assert _targets(rig) == ["00000001", "00000003", "00000004", "00000005"]


def test_edit_inside_a_section_holding_a_hand_made_link_keeps_it():
    rig = _games_rig()
    snap = load_snapshot(FakeNoteDB(rig.note), PK, rig.note.render)
    section = resolve_section(snap, NAME, heading=None, section_text="line 5")
    req = SectionRequest(
        name=NAME, pk=PK, operation="append", heading=None, section_text="line 5",
        body="after it", old_text=None, new_text=None, links=LINKS,
        expected_fingerprint=section.fingerprint, backup_path=BACKUP,
    )  # fmt: skip
    rig.run(req)
    assert UNDO not in rig.ex.keys()
    assert _targets(rig)[-1] == "00000005"


# --- #237 and every whole-note paste: what is not damage ------------------------


def test_outside_restamped_and_reminted_is_success():
    """Every paste re-stamps all paragraphs and re-mints ids (see the fake)."""
    rig = Rig()
    rig.run(rig.request("replace", body="replacement text"))
    assert UNDO not in rig.ex.keys()
    assert {p["ts"] for p in rig.note.saved[0]} == {50}


def test_title_run_merge_is_success():
    rig = Rig()
    rig.ex.on_paste = lambda paras: paras[0].update(no_style=True)
    rig.run(rig.request("replace", body="replacement text"))
    assert UNDO not in rig.ex.keys()


# --- lost updates: refused with nothing written --------------------------------


def test_editor_ahead_of_notestore_refused_nothing_pasted():
    rig = Rig()
    req = rig.request("replace", body="never pasted")
    rig.note.flushes = False
    paras = _std_paras()
    paras[5] = para("typed by the owner and not saved yet")
    rig.note.change(paras)
    with pytest.raises(SectionError, match="hasn't saved"):
        rig.run(req)
    keys = rig.ex.keys()
    assert _no_destructive(keys) and keys[-1] == LEFT
    assert rig.clip.writes == []
    # Bounded by the wait, in the fake's polling steps.
    polled = sum(rig.sleeps) - FakeExecutor.NOTE_LOAD_DELAY - section_edit.COPY_SETTLE
    assert section_edit.EDITOR_MATCH_TIMEOUT <= polled <= section_edit.EDITOR_MATCH_TIMEOUT + 1


def test_notestore_catching_up_inside_the_wait_goes_ahead():
    rig = Rig()
    paras = _std_paras()
    paras[5] = para("typed a moment ago")
    req = rig.request("replace", body="replacement text")
    rig.note.flushes = False
    rig.note.change(paras)
    rig.note.flushes = True

    def stale_then_saved(reads=[]):
        reads.append(1)
        if len(reads) == 4:
            rig.note.saved = (copy.deepcopy(rig.note.live), dict(rig.note.atts), rig.note.version)

    rig.note.saved = (copy.deepcopy(_std_paras()), {}, 0)
    rig.fp_hook = stale_then_saved
    rig.run(req)
    assert rig.note.saved_texts()[2] == "replacement text"
    assert rig.note.saved_texts()[-1] == "typed a moment ago"


def test_checked_state_ahead_in_editor_refused():
    rig = Rig(_checklist_paras())
    req = rig.request("replace", body="never pasted")
    rig.note.flushes = False
    paras = _checklist_paras()
    paras[3] = para("done list item", 103, checked=False)
    rig.note.change(paras)
    with pytest.raises(SectionError, match="hasn't saved"):
        rig.run(req)
    assert rig.clip.writes == []


def test_note_changed_between_copies_refused_and_collapsed():
    rig = Rig()
    req = rig.request("replace", body="never pasted")

    def typed(copy_number):
        if copy_number == 2:
            paras = copy.deepcopy(rig.note.live)
            paras[5]["text"] += " typed by the owner"
            rig.note.change(paras)

    rig.ex.on_copy = typed
    with pytest.raises(SectionError, match="changed while"):
        rig.run(req)
    keys = rig.ex.keys()
    assert _no_destructive(keys) and keys[-1] == LEFT
    assert rig.clip.writes == []


def test_formatting_only_change_between_copies_refused():
    rig = Rig()
    req = rig.request("replace", body="never pasted")

    def bold(copy_number):
        if copy_number == 2:
            paras = copy.deepcopy(rig.note.live)
            paras[5]["ptype"] = 1  # same text, different style
            rig.note.change(paras)

    rig.ex.on_copy = bold
    with pytest.raises(SectionError, match="changed while"):
        rig.run(req)
    assert rig.clip.writes == []


def test_refusal_after_select_all_always_collapses():
    rig = Rig()
    req = rig.request("replace", body="never pasted")
    rig.ex.copy_override = None
    with pytest.raises(SectionError):
        rig.run(req)
    assert rig.ex.keys()[-1] == LEFT and rig.ex.selected is False


def test_payload_is_built_from_the_latest_copy():
    """A table cell edited between the copies is invisible to their comparison
    (its text is U+FFFC), so the payload must carry the later state, or the
    paste silently reverts the edit (review of #240). Modelled with a link's
    alt text, which the comparison also doesn't read and the payload does carry."""
    rig = _games_rig()
    req = _games_request(rig, body="plain words")

    def edited_between(copy_number):
        if copy_number == 2:
            rig.note.atts["L"] = dict(rig.note.atts["L"], alt_text="Edited since")

    rig.ex.on_copy = edited_between
    rig.run(req)
    (payload,) = rig.clip.writes
    alts = [a.raw_data.get("7") for a in _PARSER.parse(payload).attachments]
    assert "Edited since" in alts


def test_kind_ahead_in_editor_refused():
    """A just-made heading: NoteStore lags, and resolving against that state
    or comparing with it later would misread the owner's own edit as damage."""
    rig = Rig()
    req = rig.request("replace", body="never pasted")
    rig.note.flushes = False
    paras = _std_paras()
    paras[5] = para("beta body text", 1)
    rig.note.change(paras)
    with pytest.raises(SectionError, match="hasn't saved"):
        rig.run(req)
    assert rig.clip.writes == []


def _bold_a_word_in_the_copy(rig):
    """Copies that carry a formatting change NoteStore hasn't saved."""
    plain = rig.ex._copy

    def bolded():
        richtext = _PARSER.parse(plain())
        richtext.runs[-1].raw_data["5"] = 1
        typedef = richtext._protobuf_typedef["5"]["message_typedef"]
        typedef.setdefault("5", {"type": "int", "name": ""})
        return NotesRichtextSerializer().serialize(richtext)

    rig.ex._copy = bolded


def test_formatting_ahead_in_editor_refused():
    """the owner bolded a word and NoteStore hasn't saved it: same text, same styles.
    Going ahead would compare the paste with a state missing his edit, and report
    a false "may be damaged" whose advice would lose it (review of #240)."""
    rig = Rig()
    req = rig.request("replace", body="never pasted")
    _bold_a_word_in_the_copy(rig)
    with pytest.raises(NoteBusyError, match="hasn't saved"):
        rig.run(req)
    assert rig.clip.writes == []


def test_copy_never_published_is_tried_once_more():
    """On a large note about 1 copy in 10 never publishes (measured at 52 MB)."""
    rig = Rig()
    real = rig.ex._copy
    served = []

    def first_copy_lost():
        served.append(1)
        return None if len(served) == 1 else real()

    rig.ex._copy = first_copy_lost
    rig.run(rig.request("replace", body="replacement text"))
    assert rig.note.saved_texts()[2] == "replacement text"
    assert len(served) == 3


def test_copy_unparseable_is_tried_once_more():
    rig = Rig()
    real = rig.ex._copy
    served = []

    def first_copy_corrupt():
        served.append(1)
        return b"not a plist" if len(served) == 1 else real()

    rig.ex._copy = first_copy_corrupt
    rig.run(rig.request("replace", body="replacement text"))
    assert rig.note.saved_texts()[2] == "replacement text"


def test_transient_refusals_are_note_busy():
    rig = Rig()
    req = rig.request("replace", body="never pasted")
    rig.note.flushes = False
    paras = _std_paras()
    paras[5] = para("typed and not saved")
    rig.note.change(paras)
    with pytest.raises(NoteBusyError):
        rig.run(req)

    rig = Rig()
    req = rig.request("replace", body="never pasted")
    rig.fp_hook = lambda: rig.note.change(copy.deepcopy(rig.note.live))
    with pytest.raises(NoteBusyError):
        rig.run(req)

    rig = Rig()
    req = rig.request("replace", body="never pasted")

    def typed(copy_number):
        if copy_number == 2:
            paras = copy.deepcopy(rig.note.live)
            paras[5]["text"] += " typed"
            rig.note.change(paras)

    rig.ex.on_copy = typed
    with pytest.raises(NoteBusyError):
        rig.run(req)


def test_rollback_leaves_the_note_unselected():
    """Undo re-selects what it restored; the next keypress would replace it."""
    rig = Rig()
    rig.ex.on_paste = _damage_outside
    with pytest.raises(SectionEditRolledBackError):
        rig.run(rig.request("replace", body="replacement text"))
    keys = rig.ex.keys()
    assert keys[keys.index(UNDO) + 1 :] == [LEFT]
    assert rig.ex.selected is False


def test_damaged_rollback_leaves_the_note_unselected():
    rig = Rig()
    rig.ex.on_paste = _damage_outside
    rig.ex.undo_restores = False
    with pytest.raises(SectionEditDamagedError):
        rig.run(rig.request("replace", body="replacement text"))
    assert rig.ex.keys()[-1] == LEFT


# --- flush window scales with the paste -----------------------------------------


@pytest.mark.parametrize(
    "size, expected",
    [(0, 3.0), (9_999_999, 3.0 + 9_999_999 / 10_000_000), (51_000_000, 8.1)],
)
def test_flush_window_grows_with_the_payload(size, expected):
    assert section_edit._flush_window(12, 0.25, size) == pytest.approx(expected)


def test_flush_timeout_message_includes_the_scaled_window(monkeypatch):
    rig = Rig()
    rig.note.flushes = False
    size = len(rig.ex._copy())
    monkeypatch.setattr(section_edit, "FLUSH_BYTES_PER_SECOND", size / 5)
    with pytest.raises(SectionEditRolledBackError) as err:
        rig.run(rig.request("replace", body="replacement text"))
    seconds = float(re.search(r"within ([0-9.]+)s", str(err.value)).group(1))
    assert seconds > 8.5  # 3.0 + SETTLE_EXTRA 1.5 + about 5 for this payload


# --- copying: late publish, deadline, size cap ----------------------------------


def test_copy_published_late_is_read():
    rig = Rig()
    rig.clip.hold = 12
    rig.run(rig.request("replace", body="replacement text"))
    assert rig.note.saved_texts()[2] == "replacement text"


def test_copy_never_published_refused_at_the_deadline():
    rig = Rig()
    rig.ex.copy_override = None
    with pytest.raises(NoteBusyError, match="couldn't be read"):
        rig.run(rig.request("replace", body="never pasted"))
    assert rig.ex.copies == 2
    polled = sum(rig.sleeps) - FakeExecutor.NOTE_LOAD_DELAY
    assert section_edit.COPY_TIMEOUT <= polled <= section_edit.COPY_TIMEOUT + 1
    assert section_edit.COPY_TIMEOUT == 30.0


def test_copy_at_payload_cap_allowed(monkeypatch):
    rig = Rig()
    size = len(rig.ex._copy())
    monkeypatch.setattr(section_edit, "MAX_COPY_BYTES", size)
    rig.run(rig.request("replace", body="replacement text"))
    assert rig.note.saved_texts()[2] == "replacement text"


def test_copy_over_payload_cap_refused_with_nothing_written(monkeypatch):
    rig = Rig()
    monkeypatch.setattr(section_edit, "MAX_COPY_BYTES", len(rig.ex._copy()) - 1)
    with pytest.raises(MediaCapExceeded, match="update_note_content"):
        rig.run(rig.request("replace", body="never pasted"))
    keys = rig.ex.keys()
    assert _no_destructive(keys) and keys[-1] == LEFT
    assert section_edit.MAX_COPY_BYTES != 100_000_000  # the patched value


def test_payload_cap_default():
    assert section_edit.MAX_COPY_BYTES == 100_000_000


# --- pre-paste refusals ------------------------------------------------------


@pytest.mark.parametrize("copied", [None, b"not a plist at all"])
def test_unparseable_copy_aborts_without_undo(copied):
    rig = Rig()
    rig.ex.copy_override = copied
    with pytest.raises(SectionError) as err:
        rig.run(rig.request("replace", body="never pasted"))
    assert "couldn't be read" in str(err.value)
    keys = rig.ex.keys()
    assert LEFT in keys and _no_destructive(keys)
    assert rig.clip.writes == []


def test_fragment_round_trip_failure_aborts_before_paste(monkeypatch):
    from apple_notes.richtext import MarkdownConversionError

    def fail(*args):
        raise MarkdownConversionError("Failed to parse template: quirk")

    monkeypatch.setattr(section_edit, "build_replacement", fail)
    rig = Rig()
    with pytest.raises(SectionError, match="Couldn't build the edit") as err:
        rig.run(rig.request("replace", body="never pasted"))
    assert str(err.value).endswith("Nothing was changed.")
    keys = rig.ex.keys()
    assert LEFT in keys and _no_destructive(keys)
    assert rig.clip.writes == []


def test_error_before_paste_key_sends_no_undo():
    rig = Rig()
    rig.ex.raise_on = {COPY: WrongNoteOpenError("NOTE_CHANGED: p1")}
    with pytest.raises(WrongNoteOpenError):
        rig.run(rig.request("replace", body="never pasted"))
    assert _no_destructive(rig.ex.keys())


def test_widened_find_replace_refused_before_paste():
    rig = Rig(_blank_paras())
    with pytest.raises(SectionError) as err:
        rig.run(
            rig.request(
                "find_replace", heading="Blank heading", old_text="x", new_text="y"
            )
        )
    assert str(err.value) == "old_text not found in the section."
    assert _no_destructive(rig.ex.keys())


def test_widened_replace_builds_append_on_kept_paragraph():
    rig = Rig(_blank_paras())
    result = rig.run(
        rig.request("replace", heading="Blank heading", body="fresh words here")
    )
    assert rig.note.saved_texts() == [
        "Engine scratch title", "Blank heading", "fresh words here", "Next", "next body text",
    ]  # fmt: skip
    assert [p["ptype"] for p in rig.note.saved[0][1:3]] == [1, None]
    assert result.first_paragraph == 1 and result.paragraph_count == 2


def test_widened_append_keeps_blank_line():
    rig = Rig(_blank_paras())
    result = rig.run(
        rig.request("append", heading="Blank heading", body="fresh words here")
    )
    assert rig.note.saved_texts()[1:4] == ["Blank heading", "", "fresh words here"]
    assert result.first_paragraph == 1 and result.paragraph_count == 3


# --- verify failure + undo ----------------------------------------------------


def _damage_outside(paras):
    paras[1]["text"] = "Alpha damaged"


def test_outside_change_triggers_single_undo_and_rollback_error():
    rig = Rig()
    rig.ex.on_paste = _damage_outside
    with pytest.raises(SectionEditRolledBackError) as err:
        rig.run(rig.request("replace", body="replacement text"))
    message = str(err.value)
    assert message.startswith(f"Section edit in '{NAME}' failed verification (")
    assert "outside the section changed: paragraph 1" in message
    assert message.endswith("; it was undone and the note is unchanged.")
    assert rig.ex.keys().count(UNDO) == 1
    assert rig.note.saved_texts() == [p["text"] for p in _std_paras()]


@pytest.mark.parametrize(
    "mutate, problem",
    [
        (lambda paras: paras[2].update(text="something else"), "text"),
        (lambda paras: paras[2].update(ptype=103), "style"),
        (lambda paras: paras.insert(3, para("extra line")), "paragraph"),
    ],
)
def test_intent_mismatch_rolls_back(mutate, problem):
    rig = Rig()
    rig.ex.on_paste = mutate
    with pytest.raises(SectionEditRolledBackError, match=problem):
        rig.run(rig.request("replace", body="replacement text"))
    assert rig.ex.keys().count(UNDO) == 1


def test_link_target_mismatch_rolls_back():
    rig = Rig()

    original_paste = rig.ex._paste

    def paste_with_wrong_target():
        original_paste()
        paras, atts, _ = rig.note.saved
        ident = paras[2]["atts"][0][0]
        atts = dict(atts)
        atts[ident] = dict(
            atts[ident], target_url=f"applenotes:note/{OTHER_UUID.lower()}"
        )
        rig.note.change(paras, atts)

    rig.ex._paste = paste_with_wrong_target
    with pytest.raises(SectionEditRolledBackError, match="link"):
        rig.run(rig.request("replace", body="see [[Target]] now"))
    assert rig.ex.keys().count(UNDO) == 1


def test_undo_not_restoring_raises_damaged_and_logs(monkeypatch):
    rig = Rig()
    rig.ex.on_paste = _damage_outside
    rig.ex.undo_restores = False
    calls = []
    monkeypatch.setattr(
        section_edit.unified_log, "log_error", lambda *a, **k: calls.append((a, k))
    )
    with pytest.raises(SectionEditDamagedError) as err:
        rig.run(rig.request("replace", body="replacement text"))
    message = str(err.value)
    assert message.startswith(
        f"Section edit in '{NAME}' failed verification and undo did not restore it ("
    )
    assert message.endswith(
        f"). The note may be damaged; restore from the backup: {BACKUP}."
    )
    assert rig.ex.keys().count(UNDO) == 1
    assert len(calls) == 1
    logged = json.dumps(calls[0], default=str)
    for secret in (NAME, "Alpha", "body line", "replacement text", BACKUP):
        assert secret not in logged


def test_undo_failure_log_names_changed_field_paths(monkeypatch):
    # #224/#225: the log must name what changed outside the section and what
    # still differs after the undo, by paragraph index and field, never note text.
    rig = Rig()
    rig.ex.on_paste = _damage_outside
    rig.ex.undo_restores = False
    calls = []
    monkeypatch.setattr(
        section_edit.unified_log, "log_error", lambda *a, **k: calls.append((a, k))
    )
    with pytest.raises(SectionEditDamagedError) as err:
        rig.run(rig.request("replace", body="replacement text"))
    assert "paragraph 1 ('Alpha'): text" in str(err.value)
    (_message, extras), _kwargs = calls[0]
    assert extras["outside_fields"][0].startswith("paragraph 1: text")
    assert extras["undo_diff_fields"]
    assert all(line.startswith("paragraph ") for line in extras["undo_diff_fields"])
    logged = json.dumps(extras, default=str)
    for secret in (NAME, "Alpha", "body line", "replacement text", BACKUP):
        assert secret not in logged


def test_flush_timeout_rolls_back():
    rig = Rig()
    rig.note.flushes = False
    with pytest.raises(SectionEditRolledBackError, match="save"):
        rig.run(
            rig.request("replace", body="replacement text"),
            flush_tries=3,
            flush_interval=0.5,
        )
    assert rig.ex.keys().count(UNDO) == 1
    # Verify waits the flush window + settle extra (no save ever lands); the
    # rollback then settles on the unchanged note. Both stay bounded.
    polled = sum(rig.sleeps) - FakeExecutor.NOTE_LOAD_DELAY - 2 * section_edit.COPY_SETTLE
    window = 3 * 0.5 + section_edit.SETTLE_EXTRA
    assert window <= polled <= 2 * (window + section_edit.SETTLE_GAP)


def test_error_after_paste_key_landed_undoes():
    # A guard refusal can't happen after the key in its one-key chunk (see
    # test_guard_refusal_of_paste_key_sends_no_undo); a timeout can.
    from apple_notes.exceptions import NotesNotRespondingError

    rig = Rig()
    rig.ex.raise_on = {PASTE: NotesNotRespondingError("timed out")}
    with pytest.raises(SectionEditRolledBackError) as err:
        rig.run(rig.request("replace", body="replacement text"))
    assert isinstance(err.value.__cause__, NotesNotRespondingError)
    assert rig.ex.keys().count(UNDO) == 1
    assert rig.note.saved_texts() == [p["text"] for p in _std_paras()]


# --- fix round 1: undo honesty, interrupts, blank input, styles, stable read --


def test_undo_send_failure_never_reports_rolled_back(monkeypatch):
    from apple_notes.exceptions import NotesNotRespondingError

    rig = Rig()
    rig.note.flushes = False  # Notes hung: nothing reaches the database
    rig.ex.undo_restores = False
    rig.ex.raise_on = {
        PASTE: NotesNotRespondingError("timed out"),
        UNDO: NotesNotRespondingError("timed out"),
    }
    calls = []
    monkeypatch.setattr(
        section_edit.unified_log, "log_error", lambda *a, **k: calls.append((a, k))
    )
    with pytest.raises(SectionEditDamagedError) as err:
        rig.run(rig.request("replace", body="replacement text"))
    message = str(err.value)
    assert "outcome is unknown" in message
    assert message.endswith(f"restore from the backup: {BACKUP}.")
    assert isinstance(err.value.__cause__, NotesNotRespondingError)
    assert rig.ex.keys().count(UNDO) == 1
    assert len(calls) == 1
    (_message, extras), _kwargs = calls[0]
    assert extras["undo_error_type"] == "NotesNotRespondingError"
    assert extras["undo_diff_fields"] == ["not_read"]  # the undo never ran


def test_paste_send_failure_then_undo_timeout_reports_unknown_not_rolled_back():
    from apple_notes.exceptions import NotesNotRespondingError

    # The paste key's guard timed out, so Cmd+V may never have landed; the
    # Cmd+Z that follows can undo the user's previous edit, and Notes hasn't
    # saved anything yet. A stale, unchanged database proves nothing.
    rig = Rig()
    rig.note.flushes = False
    rig.ex.raise_before = {PASTE: NotesNotRespondingError("timed out")}
    with pytest.raises(SectionEditDamagedError) as err:
        rig.run(rig.request("replace", body="replacement text"))
    message = str(err.value)
    assert "outcome is unknown" in message
    assert message.endswith(f"restore from the backup: {BACKUP}.")
    assert isinstance(err.value.__cause__, NotesNotRespondingError)
    assert rig.ex.keys().count(UNDO) == 1


def test_paste_send_failure_with_observed_restore_is_rolled_back():
    from apple_notes.exceptions import NotesNotRespondingError

    rig = Rig()  # paste landed and flushed before the send raised
    rig.ex.raise_on = {PASTE: NotesNotRespondingError("timed out")}
    with pytest.raises(SectionEditRolledBackError):
        rig.run(rig.request("replace", body="replacement text"))


def test_interrupt_after_paste_attempts_single_undo_and_reraises():
    rig = Rig()
    interrupt = KeyboardInterrupt()
    rig.ex.raise_on = {PASTE: interrupt}
    with pytest.raises(KeyboardInterrupt) as err:
        rig.run(rig.request("replace", body="replacement text"))
    assert err.value is interrupt
    assert rig.ex.keys().count(UNDO) == 1
    assert rig.note.saved_texts() == [p["text"] for p in _std_paras()]


def test_interrupt_before_paste_sends_no_undo():
    rig = Rig()
    rig.ex.raise_on = {COPY: KeyboardInterrupt()}
    with pytest.raises(KeyboardInterrupt):
        rig.run(rig.request("replace", body="never pasted"))
    assert _no_destructive(rig.ex.keys())


@pytest.mark.parametrize(
    "operation, fields, message",
    [
        ("replace", {"body": ""}, "body can't be blank"),
        ("replace", {"body": "  \n "}, "body can't be blank"),
        ("append", {"body": None}, "body can't be blank"),
        ("find_replace", {"old_text": " ", "new_text": "x"}, "old_text can't be blank"),
    ],
)
def test_blank_body_refused_before_keys(operation, fields, message):
    rig = Rig()
    with pytest.raises(SectionError, match=message):
        rig.run(rig.request(operation, **fields))
    assert rig.log == []  # not even opened


def test_find_replace_empty_new_text_deletes_the_match():
    rig = Rig()
    rig.run(rig.request("find_replace", old_text="second ", new_text=""))
    text = _written(rig)
    assert "first body line\nbody line" in text and "{}" not in text
    assert rig.note.saved_texts()[3] == "body line"


def test_edit_leaving_section_empty_refused_before_paste():
    rig = Rig()
    with pytest.raises(SectionError, match="would leave the section .* empty"):
        rig.run(
            rig.request(
                "find_replace", heading="Beta", old_text="beta body text", new_text=""
            )
        )
    keys = rig.ex.keys()
    assert LEFT in keys and _no_destructive(keys)
    assert rig.clip.writes == []


def _checklist_paras():
    paras = _std_paras()
    paras[3] = para("done list item", 103, checked=True)
    return paras


def test_checked_state_kept_through_find_replace():
    rig = Rig(_checklist_paras())
    rig.run(rig.request("find_replace", old_text="list item", new_text="list entry"))
    kept = rig.note.saved[0][3]
    assert (kept["text"], kept["ptype"], kept["checked"]) == (
        "done list entry",
        103,
        True,
    )


def test_lost_checked_state_rolls_back():
    rig = Rig(_checklist_paras())
    rig.ex.on_paste = lambda paras: paras[3].update(checked=False)
    with pytest.raises(SectionEditRolledBackError, match="style"):
        rig.run(
            rig.request("find_replace", old_text="list item", new_text="list entry")
        )
    assert rig.ex.keys().count(UNDO) == 1


def test_lost_indent_rolls_back():
    rig = Rig()
    rig.ex.on_paste = lambda paras: paras[2].update(indent=1)
    with pytest.raises(SectionEditRolledBackError, match="style"):
        rig.run(rig.request("replace", body="replacement text"))


def test_before_state_retried_when_note_changes_mid_read():
    rig = Rig()
    req = rig.request("replace", body="replacement text")
    reads = []

    def change_once():
        reads.append(1)
        if len(reads) == 2:  # between the first pair's fingerprint reads
            rig.note.change(copy.deepcopy(rig.note.live))

    rig.fp_hook = change_once
    rig.run(req)
    names = [entry[0] for entry in rig.log]
    assert names[: rig.log.index(("key", PASTE))].count("db") >= 2
    assert rig.note.saved_texts()[2] == "replacement text"


def test_before_state_never_stable_refused_before_paste():
    rig = Rig()
    req = rig.request("replace", body="replacement text")
    rig.fp_hook = lambda: rig.note.change(copy.deepcopy(rig.note.live))
    with pytest.raises(SectionError, match="kept changing while it was read"):
        rig.run(req)
    assert _no_destructive(rig.ex.keys())


def test_fragment_link_ids_normalized_and_unreadable_targets_are_none():
    from apple_notes.richtext import Attachment, AttributeRun, NotesRichtext

    token = json.dumps(
        {
            "tokenContentIdentifierKey": f"applenotes:note/{UUID.lower()}?ownerIdentifier=o"
        }
    )
    fragment = NotesRichtext(
        text="￼ ￼",
        runs=[
            AttributeRun(
                1, raw_data={"1": 1, "12": {"1": b"ID1", "2": LINK_UTI.encode()}}
            ),
            AttributeRun(1, raw_data={"1": 1}),
            AttributeRun(1, raw_data={"1": 1, "12": {"1": "ID2", "2": LINK_UTI}}),
        ],
        attachments=[
            Attachment(b"ID1", LINK_UTI, raw_data={"2": b"ID1", "34": token}),
            Attachment(b"ID2", LINK_UTI, raw_data={"2": "ID2", "34": "not json"}),
        ],
    )
    styles, links = section_edit._fragment_styles_and_links(fragment)
    assert links == [UUID.lower(), None]
    assert styles == [("body", 0, None)]


# --- final review: a guard refusal of the destructive key itself -----------


@pytest.mark.parametrize(
    "operation, fields, error",
    [
        ("replace", {"body": "never pasted"}, WrongNoteOpenError("NOTE_CHANGED: p1")),
        ("delete", {}, None),  # NotesNotFrontmostError, built below
    ],
)
def test_guard_refusal_of_paste_key_sends_no_undo(monkeypatch, operation, fields, error):
    from apple_notes.exceptions import NotesNotFrontmostError

    error = error or NotesNotFrontmostError("Apple Notes is not frontmost")
    calls = []
    monkeypatch.setattr(
        section_edit.unified_log, "log_error", lambda *a, **k: calls.append((a, k))
    )
    rig = Rig()
    rig.ex.raise_before = {PASTE: error}
    with pytest.raises(type(error)) as err:
        rig.run(rig.request(operation, **fields))
    assert err.value is error
    keys = rig.ex.keys()
    assert UNDO not in keys
    assert keys[keys.index(PASTE) + 1 :] == [LEFT]  # best-effort collapse, nothing else
    assert (SELECT_ALL, PASTE) in rig.ex.sends
    assert calls == []
    assert rig.note.version == 0


@pytest.mark.parametrize(
    "operation, fields",
    [("replace", {"body": "never pasted"}), ("delete", {})],
)
def test_modal_refusal_of_paste_key_sends_no_undo(monkeypatch, operation, fields):
    """A dialog open in Notes (issue #199) refuses the paste before it runs, so
    it takes the guard-refusal path: no Cmd+Z, error re-raised. The error is
    built by the real classifier from the real sentinel."""
    from apple_notes.exceptions import NotesModalOpenError, NotesNotFrontmostError
    from apple_notes.ui_scripting import MODAL_SENTINEL, UIScriptingExecutor

    with pytest.raises(NotesModalOpenError) as classified:
        UIScriptingExecutor._raise_keystroke_failure(
            MagicMock(),
            f'execution error: {MODAL_SENTINEL}: "Delete Note?" (-2700)',
            "paste the section",
        )
    error = classified.value
    assert isinstance(error, NotesNotFrontmostError)
    calls = []
    monkeypatch.setattr(
        section_edit.unified_log, "log_error", lambda *a, **k: calls.append((a, k))
    )
    rig = Rig()
    rig.ex.raise_before = {PASTE: error}
    with pytest.raises(NotesModalOpenError) as err:
        rig.run(rig.request(operation, **fields))
    assert err.value is error
    keys = rig.ex.keys()
    assert UNDO not in keys
    assert keys[keys.index(PASTE) + 1 :] == [LEFT]
    assert calls == []
    assert rig.note.version == 0


def test_not_responding_on_paste_key_still_takes_undo_path():
    from apple_notes.exceptions import NotesNotRespondingError

    rig = Rig()
    rig.ex.raise_before = {PASTE: NotesNotRespondingError("timed out")}
    with pytest.raises(SectionEditDamagedError, match="outcome is unknown"):
        rig.run(rig.request("replace", body="replacement text"))
    assert rig.ex.keys().count(UNDO) == 1


# --- live run: Notes drops an explicit empty paragraph style on undo --------


def test_undo_dropping_empty_paragraph_style_still_rolls_back():
    rig = Rig()
    rig.ex.on_paste = _damage_outside  # force the failure path
    # Text and stamps come back exactly; only paragraph 3's style (empty once
    # its uuid 2 -> 9 is ignored) is gone, as Notes did in the live run.
    rig.ex.on_undo = lambda paras: paras[3].update(no_style=True)
    with pytest.raises(SectionEditRolledBackError):
        rig.run(rig.request("replace", body="replacement text"))
    assert rig.ex.keys().count(UNDO) == 1
    assert rig.note.saved[0][3]["no_style"] is True


# --- live run 2: settled states (R21) and the total count (R22) -------------


def _ends_with_empty_section():
    return _std_paras() + [para("Gamma", 1)]  # empty section under the last heading


def _extra_empty_final(paras):
    paras.append(para("", no_style=True))  # no text, no runs


def test_rollback_waits_for_settled_state():
    rig = Rig()
    rig.ex.on_paste = _damage_outside  # force the failure path
    rig.ex.undo_first = _damage_outside  # Notes saves the undo in two steps
    with pytest.raises(SectionEditRolledBackError):
        rig.run(rig.request("replace", body="replacement text"))
    assert rig.ex.keys().count(UNDO) == 1
    assert rig.note.saved_texts() == [p["text"] for p in _std_paras()]


def test_verify_requires_stable_pass():
    rig = Rig()  # the paste lands correctly, then a second save adds junk
    rig.ex.after_paste = lambda paras: paras.insert(4, para("junk paragraph"))
    with pytest.raises(SectionEditRolledBackError, match="paragraph"):
        rig.run(rig.request("replace", body="replacement text"))
    assert rig.ex.keys().count(UNDO) == 1


def test_settle_window_is_bounded():
    rig = Rig()
    rig.ex.after_paste = lambda paras: paras.insert(4, para("junk paragraph"))
    with pytest.raises(SectionEditRolledBackError):
        rig.run(rig.request("replace", body="replacement text"))
    polled = sum(rig.sleeps) - FakeExecutor.NOTE_LOAD_DELAY - 2 * section_edit.COPY_SETTLE
    window = 12 * 0.25 + section_edit.SETTLE_EXTRA
    assert polled <= 2 * (window + section_edit.SETTLE_GAP)


def test_extra_empty_final_paragraph_tolerated_at_note_end():
    rig = Rig(_ends_with_empty_section())
    rig.ex.on_paste = _extra_empty_final
    result = rig.run(rig.request("append", heading="Gamma", body="- [ ] gamma task"))
    assert rig.note.saved_texts()[-3:] == ["Gamma", "gamma task", ""]
    assert rig.ex.keys().count(UNDO) == 0
    assert result.first_paragraph == 6 and result.paragraph_count == 2


def test_extra_empty_final_paragraph_tolerated_for_an_edit_elsewhere():
    """Every paste reaches the note's end now: a note ending in a list gains
    the empty final paragraph whichever section was edited (measured live)."""
    rig = Rig()
    rig.ex.on_paste = _extra_empty_final
    rig.run(rig.request("append", body="third body line"))
    assert rig.ex.keys().count(UNDO) == 0
    assert rig.note.saved_texts()[-2:] == ["beta body text", ""]


def test_extra_empty_final_paragraph_tolerated_for_delete():
    rig = Rig()
    rig.ex.on_paste = _extra_empty_final
    rig.run(rig.request("delete", heading="Beta"))
    assert rig.ex.keys().count(UNDO) == 0


def test_extra_empty_final_not_tolerated_when_the_note_already_ended_blank():
    paras = _std_paras() + [para("", no_style=True)]
    rig = Rig(paras)
    rig.ex.on_paste = _extra_empty_final
    with pytest.raises(SectionEditRolledBackError, match="paragraph"):
        rig.run(rig.request("append", body="third body line"))


def test_extra_paragraph_mid_note_fails_verify():
    rig = Rig()
    rig.ex.on_paste = lambda paras: paras.insert(4, para("", no_style=True))
    with pytest.raises(SectionEditRolledBackError, match="paragraph"):
        rig.run(rig.request("append", body="third body line"))


@pytest.mark.parametrize(
    "extra",
    [
        [para("junk at the end")],
        [para("", no_style=True), para("", no_style=True)],
    ],
)
def test_extra_nonempty_final_paragraph_fails_verify(extra):
    rig = Rig(_ends_with_empty_section())
    rig.ex.on_paste = lambda paras: paras.extend(copy.deepcopy(extra))
    with pytest.raises(SectionEditRolledBackError, match="paragraph"):
        rig.run(rig.request("append", heading="Gamma", body="- [ ] gamma task"))


# --- R24: trailing newlines in body ------------------------------------------


@pytest.mark.parametrize(
    "operation, body", [("append", "- [ ] gamma task\n"), ("replace", "new text\r\n\n")]
)
def test_body_trailing_newline_does_not_add_blank_paragraph(operation, body):
    rig = Rig()
    rig.run(rig.request(operation, body=body))
    text = _written(rig)
    assert not text.endswith("\n") and "\r" not in text
    assert "" not in rig.note.saved_texts()


def test_body_of_only_newlines_still_refused():
    rig = Rig()
    with pytest.raises(SectionError, match="body can't be blank"):
        rig.run(rig.request("replace", body="\n\r\n"))
    assert rig.log == []


def test_find_replace_new_text_newline_kept():
    rig = Rig()
    rig.run(rig.request("find_replace", old_text="second ", new_text="2nd\n"))
    assert "first body line\n2nd\nbody line" in _written(rig)


def test_flush_timeout_message_names_the_real_window():
    rig = Rig()
    rig.note.flushes = False
    with pytest.raises(SectionEditRolledBackError, match=r"within 4\.5s"):
        rig.run(rig.request("replace", body="replacement text"))


# --- ui_operations lifecycle wrapper -----------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        SectionResult(2, 1, "fp"),
        SectionError("refused"),
        SectionEditRolledBackError("rolled back"),
        RuntimeError("boom"),
    ],
)
def test_clipboard_restored_on_every_path(outcome):
    from contextlib import contextmanager

    order = []

    @contextmanager
    def fake_lock(*args, **kwargs):
        order.append("lock")
        try:
            yield
        finally:
            order.append("unlock")

    clipboard = MagicMock()
    clipboard.save_state.side_effect = lambda: order.append("save") or {"saved": True}
    clipboard.restore_state.side_effect = lambda state: order.append("restore")
    run = MagicMock()

    def fake_run(*args, **kwargs):
        order.append("run")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    run.side_effect = fake_run
    req = Rig().request("replace", body="anything")
    with (
        patch("apple_notes.edit_lock.edit_lock", fake_lock),
        patch(
            "apple_notes.session_lock.require_ui_scripting_session",
            side_effect=lambda: order.append("session"),
        ),
        patch.object(ui_operations, "_verify_content") as verify_content,
        patch.object(ui_operations, "UIScriptingExecutor") as executor_cls,
        patch.object(ui_operations, "ClipboardManager", return_value=clipboard),
        patch.object(ui_operations, "NoteContentReader"),
        patch.object(ui_operations, "run_section_edit", run),
    ):
        if isinstance(outcome, Exception):
            with pytest.raises(type(outcome)):
                ui_operations.execute_edit_note_section(req)
        else:
            assert ui_operations.execute_edit_note_section(req) == outcome
    assert order == ["lock", "session", "save", "run", "restore", "unlock"]
    clipboard.restore_state.assert_called_once_with({"saved": True})
    verify_content.assert_not_called()
    args, kwargs = run.call_args
    assert args[0] is executor_cls.return_value and args[1] is clipboard
    assert args[2] is ui_operations.NotesDatabase and args[3] is req
    assert callable(kwargs["render"])


def test_capped_log_list_keeps_the_event_bounded():
    # The vendored logger replaces an over-long json_extra wholesale, which
    # would drop the event's other fields; diagnostic lists are capped.
    lines = [f"paragraph {i}: runs [13]" for i in range(30)]
    capped = section_edit._capped(lines)
    assert capped[:20] == lines[:20]
    assert capped[20:] == ["... 10 more"]
    assert section_edit._capped(lines[:5]) == lines[:5]


def test_undo_never_read_logs_not_read_marker(monkeypatch):
    # The paste send raised and Notes never saved the undo, so the note was
    # never re-read: an empty undo_diff_fields would read as "no difference".
    from apple_notes.exceptions import NotesNotRespondingError

    rig = Rig()
    rig.note.flushes = False
    rig.ex.raise_before = {PASTE: NotesNotRespondingError("timed out")}
    calls = []
    monkeypatch.setattr(
        section_edit.unified_log, "log_error", lambda *a, **k: calls.append((a, k))
    )
    with pytest.raises(SectionEditDamagedError):
        rig.run(rig.request("replace", body="replacement text"))
    (_message, extras), _kwargs = calls[0]
    assert extras["undo_diff_fields"] == ["not_read"]
