"""Content-level note comparison for whole-note section edits (issues #240, #237).

A whole-note paste re-mints attachment ids and re-stamps every run, so a section
edit proves "nothing else changed" on content: what a person can see in the note.
Headless and CI-visible.
"""

import json

import pytest

from apple_notes import folder_scope, sections
from apple_notes.database import NotesDatabase
from apple_notes.sections import (
    MAX_NOTE_MEDIA_BYTES,
    TABLE_UTI,
    AttachmentRef,
    MediaCapExceeded,
    NoteBusyError,
    SectionError,
    refuse_heavy_media,
    NoteSnapshot,
    Paragraph,
    content_changes,
    load_snapshot,
    section_fingerprint,
    whole_note_content_changes,
)
from section_fakes import LINK_UTI, FakeDB, encode_note, para

TAG_UTI = "com.apple.notes.inlinetextattachment.hashtag"
JPEG = "public.jpeg"


def sig(length, **fields):
    return f"{length}:{json.dumps(fields, sort_keys=True)}"


def p(text, *sigs, index=1, kind="body", indent=0, checked=None, atts=()):
    return Paragraph(
        index, 0, text, kind, indent, checked, tuple(atts), sigs or (sig(len(text)),)
    )


def snap(*paragraphs):
    return NoteSnapshot(1, tuple(paragraphs), (None,) * len(paragraphs))


def diff(old, new):
    """Differences when only `old` / `new` (at index 1) differ, under a title."""
    title = p("T\n", index=0, kind="title")
    return whole_note_content_changes(snap(title, old), snap(title, new))


def link(target, *, ident="a", alt="Name"):
    return AttachmentRef(ident, LINK_UTI, target, alt, None)


UUID = "applenotes:note/aaaa-1?ownerIdentifier=_o"
HAND_MADE = "applenotes://showNote?identifier=AAAA-1"


def test_restamped_runs_are_no_content_change():
    old = p("hello\n", sig(6, **{"2": {"1": 0, "3": 1}, "13": 100}))
    new = p("hello\n", sig(6, **{"2": {"1": 0}, "13": 200}))
    assert diff(old, new) == []


def test_reissued_attachment_and_checklist_ids_are_no_content_change():
    old = p("￼\n", sig(1, **{"12": {"1": "A"}}), sig(1), atts=[link(UUID, ident="A")])
    new = p("￼\n", sig(1, **{"12": {"1": "B"}}), sig(1), atts=[link(UUID, ident="B")])
    assert diff(old, new) == []
    old = p("x\n", sig(2, **{"2": {"5": {"1": "id-1", "2": 1}}}), checked=True)
    new = p("x\n", sig(2, **{"2": {"5": {"1": "id-2", "2": 1}}}), checked=True)
    assert diff(old, new) == []


def test_run_split_or_merge_is_no_content_change():
    """#237: a title going from 3 runs to 2 with the same attributes."""
    style = {"2": {"1": 0}}
    old = p("★ Books\n", sig(1, **style), sig(6, **style), sig(1, **style))
    new = p("★ Books\n", sig(7, **style), sig(1, **style))
    assert diff(old, new) == []


def test_formatting_change_is_a_content_change():
    old = p("hello\n", sig(6))
    new = p("hello\n", sig(5, **{"5": 1}), sig(1))
    changes = diff(old, new)
    assert [c.index for c in changes] == [1]
    assert "formatting" in changes[0].fields[0]


def test_moved_formatting_boundary_is_a_content_change():
    old = p("hello\n", sig(2, **{"5": 1}), sig(4))
    new = p("hello\n", sig(3, **{"5": 1}), sig(3))
    assert diff(old, new)


@pytest.mark.parametrize(
    "old, new, field",
    [
        (p("a\n"), p("b\n"), "text"),
        (p("a\n", kind="heading"), p("a\n"), "kind"),
        (p("a\n", indent=1), p("a\n"), "indent"),
        (p("a\n", kind="checklist", checked=True), p("a\n", kind="checklist", checked=False), "checked"),
    ],
)  # fmt: skip
def test_paragraph_level_change_is_a_content_change(old, new, field):
    changes = diff(old, new)
    assert changes and field in changes[0].fields


def test_link_target_change_is_a_content_change():
    other = "applenotes:note/bbbb-2?ownerIdentifier=_o"
    assert diff(p("￼\n", atts=[link(UUID)]), p("￼\n", atts=[link(other)]))


def test_link_renamed_alt_same_target_is_no_change():
    assert diff(p("￼\n", atts=[link(UUID, alt="Old")]), p("￼\n", atts=[link(UUID, alt="New")])) == []


def test_hand_made_link_compared_by_uuid():
    other = "applenotes://showNote?identifier=BBBB-2"
    assert diff(p("￼\n", atts=[link(HAND_MADE)]), p("￼\n", atts=[link(HAND_MADE)])) == []
    assert diff(p("￼\n", atts=[link(HAND_MADE)]), p("￼\n", atts=[link(other)]))


def test_link_with_no_readable_target_falls_back_to_alt():
    assert diff(p("￼\n", atts=[link(None, alt="A")]), p("￼\n", atts=[link(None, alt="A")])) == []
    assert diff(p("￼\n", atts=[link(None, alt="A")]), p("￼\n", atts=[link(None, alt="B")]))


def test_tag_alt_change_is_a_content_change():
    tag = lambda alt: AttachmentRef("t", TAG_UTI, None, alt, None)  # noqa: E731
    assert diff(p("￼\n", atts=[tag("#a")]), p("￼\n", atts=[tag("#b")]))


def test_lost_attachment_is_a_content_change():
    assert diff(p("￼\n", atts=[link(UUID)]), p("￼\n"))


def test_table_or_media_content_change_is_a_change():
    blob = lambda uti, c, i="x": AttachmentRef(i, uti, None, None, c)  # noqa: E731
    assert diff(p("￼\n", atts=[blob(TABLE_UTI, "h1")]), p("￼\n", atts=[blob(TABLE_UTI, "h2")]))
    assert diff(p("￼\n", atts=[blob(JPEG, "100")]), p("￼\n", atts=[blob(JPEG, "101")]))
    assert diff(p("￼\n", atts=[blob(JPEG, "100", "x")]), p("￼\n", atts=[blob(JPEG, "100", "y")])) == []


def test_title_body_kind_on_paragraph_zero_is_no_change():
    """A whole paste gives an unstyled first line the title style."""
    old = snap(p("T\n", sig(2), index=0, kind="body"), p("b\n"))
    new = snap(p("T\n", sig(2, **{"2": {"1": 0}}), index=0, kind="title"), p("b\n"))
    assert whole_note_content_changes(old, new) == []


def test_heading_title_to_body_on_paragraph_zero_is_a_change():
    old = snap(p("T\n", index=0, kind="heading"), p("b\n"))
    new = snap(p("T\n", index=0, kind="body"), p("b\n"))
    assert whole_note_content_changes(old, new)


def test_body_to_title_kind_on_later_paragraph_is_a_change():
    assert diff(p("a\n", kind="body"), p("a\n", kind="title"))


def test_content_changes_ignores_the_edited_range():
    title = p("T\n", index=0, kind="title")
    before = snap(title, p("a\n", index=1), p("b\n", index=2), p("c", index=3))
    after = snap(title, p("A\n", index=1), p("AA\n", index=2), p("b\n", index=3), p("c", index=4))
    assert content_changes(before, after, 1, 2) == []


def test_content_changes_reports_prefix_and_suffix():
    title = p("T\n", index=0, kind="title")
    before = snap(title, p("a\n", index=1), p("b\n", index=2), p("c", index=3))
    after = snap(p("X\n", index=0, kind="title"), p("A\n", index=1), p("b\n", index=2), p("Z", index=3))
    assert sorted(c.index for c in content_changes(before, after, 1, 2)) == [0, 3]


def test_content_changes_tolerates_prev_newline_only_when_told():
    title = p("T\n", index=0, kind="title")
    before = snap(title, p("a\n", index=1), p("b", index=2))
    after = snap(title, p("a", sig(1), index=1))
    assert content_changes(before, after, 2, 3, prev_newline_removed=True) == []
    assert content_changes(before, after, 2, 3)


def test_content_changes_note_shorter_than_outside():
    title = p("T\n", index=0, kind="title")
    before = snap(title, p("a\n", index=1), p("b", index=2))
    changes = content_changes(before, snap(title), 1, 2)
    assert changes and changes[0].index is None


def test_whole_note_diff_reports_added_paragraph_not_a_shift():
    title = p("T\n", index=0, kind="title")
    before = snap(title, p("a\n", index=1), p("b", index=2))
    after = snap(title, p("new\n", index=1), p("a\n", index=2), p("b", index=3))
    changes = whole_note_content_changes(before, after)
    assert changes[0].index is None and "count" in changes[0].fields[0]
    assert [c.fields for c in changes[1:]] == [("added",)]


def test_whole_note_diff_reports_removed_paragraph():
    title = p("T\n", index=0, kind="title")
    before = snap(title, p("a\n", index=1), p("b\n", index=2), p("c", index=3))
    after = snap(title, p("a\n", index=1), p("c", index=2))
    assert [c.redacted() for c in whole_note_content_changes(before, after)] == [
        "paragraph count 4 -> 3",
        "paragraph 2: removed",
    ]


def test_change_text_is_truncated_for_callers():
    changes = diff(p("x" * 100 + "\n"), p("y" * 100 + "\n"))
    assert "x" * 41 not in str(changes[0])


def test_identical_fixture_snapshots_have_no_change():
    one = load_snapshot(FakeDB.from_fixture(), 6628)
    two = load_snapshot(FakeDB.from_fixture(), 6628)
    assert whole_note_content_changes(one, two) == []


def test_redacted_change_names_fields_never_text():
    changes = diff(p("secret words\n"), p("other words\n"))
    assert "secret" not in changes[0].redacted()


# --- real snapshots ---------------------------------------------------------


def _table_snapshot(monkeypatch, grid):
    monkeypatch.setattr(sections, "parse_table_crdt", lambda data: grid)
    paras = [para("T", 0), para("￼", atts=[("t", TABLE_UTI)]), para("end")]
    db = FakeDB(encode_note(paras), {}, {"t": b"crdt"})
    return load_snapshot(db, 1)


def test_table_reordered_cells_is_no_change(monkeypatch):
    old = _table_snapshot(monkeypatch, [["a", "b"], ["c", "d"]])
    new = _table_snapshot(monkeypatch, [["c", "a"], ["d", "b"]])
    assert whole_note_content_changes(old, new) == []


def test_table_lost_cell_is_a_change(monkeypatch):
    old = _table_snapshot(monkeypatch, [["a", "b"], ["c", "d"]])
    new = _table_snapshot(monkeypatch, [["a", "b"], ["c", ""]])
    assert whole_note_content_changes(old, new)


def test_table_reshaped_is_a_change(monkeypatch):
    old = _table_snapshot(monkeypatch, [["a", "b", "c", "d"]])
    new = _table_snapshot(monkeypatch, [["a", "b"], ["c", "d"]])
    assert whole_note_content_changes(old, new)


def test_unparseable_table_falls_back_to_raw_bytes(monkeypatch):
    monkeypatch.setattr(sections, "parse_table_crdt", lambda data: None)
    paras = [para("T", 0), para("￼", atts=[("t", TABLE_UTI)])]
    one = load_snapshot(FakeDB(encode_note(paras), {}, {"t": b"one"}), 1)
    two = load_snapshot(FakeDB(encode_note(paras), {}, {"t": b"two"}), 1)
    assert whole_note_content_changes(one, two)


def test_fingerprint_stable_when_table_cells_reorder(monkeypatch):
    """A queued section edit must not conflict because another write re-pasted
    the note and rebuilt its table object (design: fingerprint change)."""
    old = _table_snapshot(monkeypatch, [["a", "b"], ["c", "d"]])
    new = _table_snapshot(monkeypatch, [["c", "a"], ["d", "b"]])
    assert section_fingerprint(old, 1, 2) == section_fingerprint(new, 1, 2)


def test_fingerprint_sees_a_changed_table(monkeypatch):
    old = _table_snapshot(monkeypatch, [["a", "b"]])
    new = _table_snapshot(monkeypatch, [["a", "z"]])
    assert section_fingerprint(old, 1, 2) != section_fingerprint(new, 1, 2)


def test_media_size_comes_from_the_block_attachment_row():
    paras = [para("T", 0), para("￼", atts=[("m", JPEG)])]
    one = load_snapshot(FakeDB(encode_note(paras), block_sizes={"m": 100}), 1)
    two = load_snapshot(FakeDB(encode_note(paras), block_sizes={"m": 101}), 1)
    same = load_snapshot(FakeDB(encode_note(paras), block_sizes={"m": 100}), 1)
    assert whole_note_content_changes(one, two)
    assert whole_note_content_changes(one, same) == []


# --- media size reads (database) ----------------------------------------------


@pytest.fixture
def media_db(scoped_db):
    import sqlite3

    conn = sqlite3.connect(scoped_db)
    for col in ("ZNOTE INTEGER", "ZTYPEUTI TEXT", "ZFILESIZE INTEGER"):
        conn.execute(f"ALTER TABLE ZICCLOUDSYNCINGOBJECT ADD COLUMN {col}")
    rows = [
        ("M1", 10, JPEG, 100, 0),
        ("M2", 10, "com.adobe.pdf", 50, 0),
        ("GONE", 10, JPEG, 999, 1),
        ("SCAN", 10, "com.apple.paper.doc.scan", None, 0),
        ("OTHER", 11, JPEG, 7, 0),
        ("PRIV", 12, JPEG, 5, 0),
    ]
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT"
        " (ZIDENTIFIER, ZNOTE, ZTYPEUTI, ZFILESIZE, ZMARKEDFORDELETION)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return scoped_db


def test_block_attachment_sizes_live_rows_of_this_note(media_db):
    with NotesDatabase(media_db) as db:
        assert db.get_block_attachment_sizes(10) == {"M1": 100, "M2": 50, "SCAN": None}


def test_note_media_bytes_sums_live_rows_only(media_db):
    with NotesDatabase(media_db) as db:
        assert db.note_media_bytes(10) == 150
        assert db.note_media_bytes(999) == 0


def test_media_reads_respect_the_folder_scope(media_db):
    with NotesDatabase(media_db) as db:
        folder_scope.set_folder_allowlist(["Daily"])
        try:
            assert db.note_media_bytes(12) == 0
            assert db.get_block_attachment_sizes(12) == {}
            assert db.note_media_bytes(10) == 150
        finally:
            folder_scope.reset()


class _MediaDB:
    def __init__(self, size):
        self.size = size

    def note_media_bytes(self, pk):
        return self.size


def test_media_at_cap_allowed():
    refuse_heavy_media(_MediaDB(MAX_NOTE_MEDIA_BYTES), 1, "N")
    assert MAX_NOTE_MEDIA_BYTES == 25_000_000


def test_media_over_cap_refused_naming_the_alternative():
    with pytest.raises(MediaCapExceeded, match="update_note_content") as exc:
        refuse_heavy_media(_MediaDB(MAX_NOTE_MEDIA_BYTES + 1), 1, "Blue Prince")
    assert "Blue Prince" in str(exc.value) and "25 MB" in str(exc.value)
    assert isinstance(exc.value, SectionError)


@pytest.mark.parametrize("error", [MediaCapExceeded("too big"), NoteBusyError("retry")])
def test_section_refusals_are_not_system_faults(error):
    from mcp.server.fastmcp.exceptions import ToolError

    from apple_notes import unified_log

    assert not unified_log.should_report(error)
    wrapped = ToolError(str(error))
    wrapped.__cause__ = error
    assert not unified_log.should_report(wrapped)
