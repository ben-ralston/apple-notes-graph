"""Paragraph snapshots, outside diff and section fingerprint (issue #184)."""

import pytest

from apple_notes.models import Note
from apple_notes.note_content import NoteContentReader
from apple_notes.sections import (
    AttachmentRef,
    SECTION_KEY_ALLOWLIST,
    SectionError,
    Section,
    link_uuid,
    plan_edit,
    resolve_section,
    load_snapshot,
    section_fingerprint,
)
from section_fakes import LINK_UTI, FakeDB, encode_note, para


@pytest.fixture
def snap():
    return load_snapshot(FakeDB.from_fixture(), 6628)


def _paras(extra=None):
    """A small note; ``extra`` maps paragraph index -> replacement para()."""
    base = [
        para("Title", 0),
        para("Alpha", 1),
        para("body one"),
        para("see ￼", atts=[("L1", LINK_UTI)]),
        para("Beta", 1),
        para("body two"),
        para("Gamma", 1),
    ]
    for i, p in (extra or {}).items():
        base[i] = p
    return base


def _snap(paragraphs, attachments=None, pk=1):
    atts = {
        "L1": {
            "type_uti": LINK_UTI,
            "target_url": "applenotes:note/aa-bb",
            "alt_text": "A",
        }
    }
    atts.update(attachments or {})
    return load_snapshot(FakeDB(encode_note(paragraphs), atts), pk)


def test_snapshot_paragraph_count_and_kinds(snap):
    p = snap.paragraphs
    assert len(p) == 19
    kinds = [x.kind for x in p]
    assert kinds[:4] == ["title", "heading", "body", "subheading"]
    assert kinds[4:6] == ["dashed"] * 2
    assert kinds[6:8] == ["numbered"] * 2
    assert kinds[8:11] == ["checklist"] * 3
    assert [x.checked for x in p[8:11]] == [False, True, False]
    assert kinds[11:14] == ["body"] * 3
    assert kinds[14] == "heading" and kinds[15:17] == ["body"] * 2
    assert kinds[17] == "heading" and kinds[18] == "body"
    assert p[18].text == ""

    link, tag = p[11].atts
    target_uuid = FakeDB.link_target()["uuid"].lower()
    assert target_uuid in link.target.lower()
    assert link.alt == "NG Spike 184 Target"
    assert tag.alt == "#spiketag"
    (table,) = p[12].atts
    assert table.uti == "com.apple.notes.table"
    assert table.content and len(table.content) == 12


def test_snapshot_offsets_cover_text(snap):
    starts = [p.start for p in snap.paragraphs]
    assert starts == sorted(set(starts))
    text = "".join(p.text for p in snap.paragraphs)
    assert text.startswith("NG Spike 184\nAlpha heading\n")
    assert text.endswith("Gamma heading\n")
    assert all(text[p.start : p.start + len(p.text)] == p.text for p in snap.paragraphs)


def test_snapshot_uti_falls_back_to_run_field(snap):
    db = FakeDB(encode_note([para("t", 0), para("￼", atts=[("T1", "com.apple.notes.table")])]),
                {}, {"T1": b"cells"})  # fmt: skip
    (att,) = load_snapshot(db, 1).paragraphs[1].atts
    assert att.uti == "com.apple.notes.table" and att.content


def test_run_sig_ignores_paragraph_uuid():
    a = _snap(_paras({2: para("body one", uuid=b"aaa")}))
    b = _snap(_paras({2: para("body one", uuid=b"bbb")}))
    c = _snap(_paras({2: para("body one", ts=2)}))
    assert a.paragraphs[2].run_sigs == b.paragraphs[2].run_sigs
    assert a.paragraphs[2].run_sigs != c.paragraphs[2].run_sigs


def test_run_sig_treats_empty_paragraph_style_as_absent():
    from apple_notes.sections import _run_sig

    absent = _run_sig({"1": 5, "13": 1793414442}, 5)
    assert _run_sig({"1": 5, "13": 1793414442, "2": {}}, 5) == absent
    assert _run_sig({"1": 5, "13": 1793414442, "2": {"9": b"u"}}, 5) == absent
    assert _run_sig({"1": 5, "13": 1793414442, "2": {"1": 1}}, 5) != absent
    # Through a real snapshot: a run with no field 2 vs one with only a uuid.
    a = _snap(_paras({2: para("body one", no_style=True)}))
    b = _snap(_paras({2: para("body one")}))
    assert a.paragraphs[2].run_sigs == b.paragraphs[2].run_sigs
    from apple_notes.sections import whole_note_content_changes

    assert whole_note_content_changes(a, b) == []


# --- field paths of a run change (#224, #225) ---------------------------------


def _sigs(*runs):
    from apple_notes.sections import _run_sig

    return tuple(_run_sig(r, int(r["1"])) for r in runs)


def test_run_field_changes_names_restamped_timestamp():
    from apple_notes.sections import run_field_changes

    old = _sigs({"1": 5, "13": 1, "2": {"1": 0}})
    new = _sigs({"1": 5, "13": 9, "2": {"1": 0}})
    assert run_field_changes(old, new) == ["13"]


def test_run_field_changes_names_style_subfield():
    from apple_notes.sections import run_field_changes

    old = _sigs({"1": 1, "13": 1, "2": {"1": 0}})
    new = _sigs({"1": 1, "13": 1, "2": {"1": 0, "3": 1}})
    assert run_field_changes(old, new) == ["2.3"]


def test_run_field_changes_names_added_top_level_field():
    from apple_notes.sections import run_field_changes

    old = _sigs({"1": 1, "13": 1})
    new = _sigs({"1": 1, "13": 1, "2": {"1": 0}})
    assert run_field_changes(old, new) == ["2.1"]


def test_run_field_changes_reports_run_split():
    from apple_notes.sections import run_field_changes

    old = _sigs({"1": 8, "13": 1})
    new = _sigs({"1": 3, "13": 1, "2": {"1": 0}}, {"1": 5, "13": 2})
    # Runs no longer correspond one-to-one: comparing them by position would
    # name fields that never changed ("2.1" here), so only the count is named.
    assert run_field_changes(old, new) == ["run_count 1->2"]


def test_run_field_changes_reports_moved_run_boundary():
    from apple_notes.sections import run_field_changes

    old = _sigs({"1": 3, "13": 1}, {"1": 5, "13": 2})
    new = _sigs({"1": 4, "13": 1}, {"1": 4, "13": 2})
    assert run_field_changes(old, new) == ["lengths"]


def test_run_field_changes_never_leaks_values():
    from apple_notes.sections import run_field_changes

    old = _sigs({"1": 4, "13": 1793414442, "12": {"1": b"SECRET-ID", "2": "x.uti"}})
    new = _sigs({"1": 4, "13": 1793419999, "12": {"1": b"OTHER-ID!", "2": "x.uti"}})
    changes = run_field_changes(old, new)
    assert changes == ["12.1", "13"]
    joined = " ".join(changes)
    for value in ("1793414442", "1793419999", "SECRET", "OTHER", b"SECRET-ID".hex()):
        assert value not in joined


def test_section_fingerprint_ignores_ids_timestamps_and_2_9():
    before = _snap(_paras())
    after = _snap(
        _paras({
            2: para("body one", ts=9, uuid=b"zzz"),
            3: para("see ￼", ts=9, atts=[("L2", LINK_UTI)]),
        }),
        {"L2": {"type_uti": LINK_UTI, "target_url": "applenotes:note/aa-bb", "alt_text": "A"}},
    )  # fmt: skip
    assert section_fingerprint(before, 1, 4) == section_fingerprint(after, 1, 4)


def test_section_fingerprint_changes_with_checked_state_and_text():
    base = _snap([para("t", 0), para("item", 103, checked=False)])
    checked = _snap([para("t", 0), para("item", 103, checked=True)])
    edited = _snap([para("t", 0), para("item!", 103, checked=False)])
    fp = section_fingerprint(base, 1, 2)
    assert fp != section_fingerprint(checked, 1, 2)
    assert fp != section_fingerprint(edited, 1, 2)


def test_section_fingerprint_prefers_rendered_lines():
    snap_ = _snap(_paras())
    rendered = tuple(f"r{i}" for i in range(len(snap_.paragraphs)))
    from dataclasses import replace

    other = replace(snap_, rendered=rendered)
    assert section_fingerprint(snap_, 1, 3) != section_fingerprint(other, 1, 3)


# --- Task 2: rendered paragraph lines and section resolution ---------------


def _reader(db):
    """A NoteContentReader over a fake db (the seam: db is injected)."""
    target = FakeDB.link_target()
    reader = NoteContentReader()
    reader.db = db
    reader._uuid_lookup = {
        target["uuid"]: Note(target["title"], target["uuid"], "Notes", 1)
    }
    return reader


@pytest.fixture
def rsnap():
    db = FakeDB.from_fixture()
    reader = _reader(db)
    note = Note("NG Spike 184", "u", "Notes", 6628)
    return load_snapshot(db, 6628, render=lambda pk: reader._paragraph_lines(note))


def _rsnap(paragraphs):
    db = FakeDB(encode_note(paragraphs), {"L1": {
        "type_uti": LINK_UTI, "target_url": "applenotes:note/aa-bb", "alt_text": "A"}})  # fmt: skip
    reader = _reader(db)
    reader._uuid_lookup = {}
    note = Note("T", "u", "Notes", 1)
    return load_snapshot(db, 1, render=lambda pk: reader._paragraph_lines(note))


def test_paragraph_lines_fixture(rsnap):
    lines = rsnap.rendered
    assert len(lines) == 19
    assert lines[1] == "## Alpha heading"
    assert lines[3] == "### Alpha sub"
    assert lines[8] == "- [ ] todo open"
    assert lines[9] == "- [x] todo done"
    assert "[[NG Spike 184 Target]]" in lines[11] and "{#spiketag}" in lines[11]
    assert lines[12] is None
    assert lines[14] == "## Beta heading"


def test_paragraph_lines_public_wrapper_uses_db_context():
    entered = []

    class CtxDB(FakeDB):
        def __enter__(self):
            entered.append(1)
            return self

        def __exit__(self, *a):
            entered.append(2)

    db = CtxDB.from_fixture()
    db.zdata = FakeDB.from_fixture().zdata
    reader = _reader(db)
    lines = reader.paragraph_lines(Note("x", "u", "Notes", 6628))
    assert entered == [1, 2] and len(lines) == 19


def test_heading_section_bounds(rsnap):
    got = {
        h: (s.a, s.b)
        for h in ("Alpha heading", "Alpha sub", "Beta heading", "Gamma heading")
        for s in [resolve_section(rsnap, "n", heading=h, section_text=None)]
    }
    assert got == {
        "Alpha heading": (2, 14),
        "Alpha sub": (4, 14),
        "Beta heading": (15, 17),
        "Gamma heading": (18, 19),
    }


def test_heading_ambiguous_refused():
    snap = _rsnap(
        [para("T", 0), para("Beta", 1), para("x"), para("Beta", 1), para("y")]
    )
    with pytest.raises(SectionError) as e:
        resolve_section(snap, "N", heading="Beta", section_text=None)
    assert (
        str(e.value)
        == "Heading 'Beta' appears 2 times in note 'N'; use section_text instead."
    )


def test_heading_not_found_refused(rsnap):
    with pytest.raises(SectionError) as e:
        resolve_section(rsnap, "N", heading="Nope", section_text=None)
    assert str(e.value) == "Heading 'Nope' not found in note 'N'."


def test_heading_never_matches_title_or_body():
    snap = _rsnap([para("Same", 0), para("Same"), para("H", 1), para("x")])
    with pytest.raises(SectionError, match="not found"):
        resolve_section(snap, "N", heading="Same", section_text=None)


def test_empty_heading_section_allowed():
    snap = _rsnap([para("T", 0), para("A", 1), para("B", 1), para("x")])
    s = resolve_section(snap, "N", heading="A", section_text=None)
    assert (s.a, s.b, s.heading_index) == (2, 2, 1)


def test_text_single_and_multi_paragraph(rsnap):
    s = resolve_section(rsnap, "N", heading=None, section_text="- [x] todo done")
    assert (s.a, s.b, s.heading_index) == (9, 10, None)
    s = resolve_section(
        rsnap, "N", heading=None, section_text="- [ ] todo open\n- [x] todo done\n\n"
    )
    assert (s.a, s.b) == (8, 10)


def test_text_partial_paragraph_refused(rsnap):
    with pytest.raises(SectionError) as e:
        resolve_section(rsnap, "N", heading=None, section_text="todo do")
    assert str(e.value) == (
        "section_text not found as whole paragraphs in note 'N'. "
        "Copy whole lines exactly as read_note_content shows them."
    )


def test_text_ambiguous_refused():
    snap = _rsnap([para("T", 0), para("same"), para("x"), para("same")])
    with pytest.raises(SectionError) as e:
        resolve_section(snap, "N", heading=None, section_text="same")
    assert str(e.value) == (
        "section_text matches 2 places in note 'N'; include more lines to make it unique."
    )


def test_text_never_matches_title():
    snap = _rsnap([para("Title", 0), para("x")])
    with pytest.raises(SectionError, match="not found as whole paragraphs"):
        resolve_section(snap, "N", heading=None, section_text="Title")


def test_text_table_never_matches(rsnap):
    with pytest.raises(SectionError, match="not found as whole paragraphs"):
        resolve_section(rsnap, "N", heading=None, section_text="| A | B |")


def test_exactly_one_selector_required(rsnap):
    for kwargs in ({}, {"heading": "Beta heading", "section_text": "x"}):
        with pytest.raises(SectionError) as e:
            resolve_section(
                rsnap, "N", **{"heading": None, "section_text": None, **kwargs}
            )
        assert str(e.value) == "Pass exactly one of heading or section_text."


def test_section_fingerprint_set_on_resolution(rsnap):
    s = resolve_section(rsnap, "N", heading="Beta heading", section_text=None)
    assert s.fingerprint == section_fingerprint(rsnap, s.a, s.b)


# --- edit ranges ---------------------------------------------------------------


def _plan(rsnap, heading, op):
    sec = resolve_section(rsnap, "n", heading=heading, section_text=None)
    return plan_edit(rsnap, sec, op, "n")


def _selected(snap_, plan):
    """The characters of the note the plan replaces."""
    return "".join(p.text for p in snap_.paragraphs)[plan.start : plan.end]


def _text(snap_, a, b):
    return "".join(p.text for p in snap_.paragraphs[a:b])


def test_replace_plan_from_top(rsnap):
    sec = Section(8, 11, 7, "fp")
    plan = plan_edit(rsnap, sec, "replace", "n")
    assert (plan.edit_a, plan.edit_b) == (8, 11)
    assert _selected(rsnap, plan) == _text(rsnap, 8, 11).rstrip("\n")
    assert plan.prev_newline_removed is False


def test_append_plan_targets_last_paragraph(rsnap):
    plan = _plan(rsnap, "Beta heading", "append")
    assert (plan.edit_a, plan.edit_b) == (16, 17)
    assert _selected(rsnap, plan) == rsnap.paragraphs[16].text.rstrip("\n")


def test_append_to_empty_heading_section():
    s = _rsnap([para("T", 0), para("H", 1), para("H2", 1), para("x")])
    sec = resolve_section(s, "n", heading="H", section_text=None)
    assert sec.a == sec.b
    plan = plan_edit(s, sec, "append", "n")
    assert (plan.edit_a, plan.edit_b) == (1, 2)
    assert _selected(s, plan) == "H"


def test_delete_plan_adds_trailing_newline(rsnap):
    plan = _plan(rsnap, "Beta heading", "delete")
    assert (plan.edit_a, plan.edit_b) == (15, 17)
    assert _selected(rsnap, plan) == _text(rsnap, 15, 17)
    assert _selected(rsnap, plan).endswith("\n")
    assert plan.prev_newline_removed is False


def test_delete_plan_at_note_end_selects_from_prev_paragraph(rsnap):
    plan = _plan(rsnap, "Gamma heading", "delete")
    assert plan.prev_newline_removed is True
    assert (plan.edit_a, plan.edit_b) == (18, 19)
    assert _selected(rsnap, plan) == "\n" + ""


def test_delete_plan_at_note_end_with_text():
    s = _rsnap([para("T", 0), para("H", 1), para("one"), para("two")])
    plan = plan_edit(
        s, resolve_section(s, "n", heading="H", section_text=None), "delete", "n"
    )
    assert plan.prev_newline_removed is True
    assert _selected(s, plan) == "\none\ntwo"


def test_replace_empty_section_refused():
    s = _rsnap([para("T", 0), para("H", 1), para("H2", 1), para("x")])
    sec = resolve_section(s, "n", heading="H", section_text=None)
    for op in ("replace", "find_replace", "delete"):
        with pytest.raises(SectionError, match="Section under 'H' is empty"):
            plan_edit(s, sec, op, "n")


def _empties(texts, a, b, op="replace"):
    """Plan over an encode_note note built from ``texts`` (title first)."""
    s = _rsnap([para(t, 0 if i == 0 else None) for i, t in enumerate(texts)])
    plan = plan_edit(s, Section(a, b, None, "fp"), op, "n")
    return s, plan


def test_empty_last_paragraph_bottom_path():
    s, plan = _empties(["T", "a", "b", "c", "d", ""], 4, 5)
    assert _selected(s, plan) == "d"


def test_section_starting_with_empty_paragraph_top_path():
    s, plan = _empties(["T", "x", "", "b", "c", "d", "e", "f"], 2, 4)
    assert _selected(s, plan) == "\nb"


def test_section_ending_with_empty_paragraph_bottom_path():
    s, plan = _empties(["T", "a", "b", "c", "d", "e", "f", "g", ""], 6, 8)
    assert _selected(s, plan) == "f\ng"
    s, plan = _empties(["T", "a", "b", "c", "d", "e", "f", "", "h", "i"], 6, 8)
    assert _selected(s, plan) == "f\n"


def test_empty_paragraph_just_before_section():
    for texts, a, b, op in [
        (["T", "x", "", "b", "c", "d", "e"], 3, 5, "replace"),
        (["T", "x", "", "b", "c", "d", "e"], 3, 5, "delete"),
        (["T", "x", "", "b", "c"], 3, 5, "delete"),
    ]:
        _empties(texts, a, b, op)


def test_one_paragraph_section_that_is_itself_empty():
    s, plan = _empties(["T", "H", "", "x", "y", "z"], 2, 3)
    assert _selected(s, plan) == "H\n" and plan.keep_first
    s, plan = _empties(["T", "H", "", "x", "y", "z"], 2, 3, "delete")
    assert _selected(s, plan) == "\n"
    _empties(["T", "H", "x", ""], 3, 4, "delete")


def test_replace_blank_section_widens_to_previous_paragraph(rsnap):
    plan = _plan(rsnap, "Gamma heading", "replace")
    assert plan.edit_a == 17 and plan.edit_b == 19
    assert plan.keep_first is True
    assert _selected(rsnap, plan) == "Gamma heading\n"


def test_nonblank_plans_do_not_keep_first(rsnap):
    assert _plan(rsnap, "Beta heading", "replace").keep_first is False
    assert _plan(rsnap, "Beta heading", "append").keep_first is False
    s = _rsnap([para("T", 0), para("H", 1), para("H2", 1), para("x")])
    plan = plan_edit(
        s, resolve_section(s, "n", heading="H", section_text=None), "append", "n"
    )
    assert plan.keep_first is False and plan.edit_a == 1


def test_blank_section_under_title_refused():
    s = _rsnap([para("T", 0), para(""), para("x")])
    with pytest.raises(SectionError, match="blank right under the title"):
        plan_edit(s, Section(1, 2, None, "fp"), "replace", "N")


def test_delete_never_widened(rsnap):
    plan = _plan(rsnap, "Gamma heading", "delete")
    assert plan.edit_a == 18 and plan.keep_first is False
    s = _rsnap([para("T", 0), para("H", 1), para(""), para("x")])
    plan = plan_edit(s, Section(2, 3, 1, "fp"), "delete", "n")
    assert plan.edit_a == 2 and plan.keep_first is False


# --- link token forms (issue #240) --------------------------------------------


def _link_att(target):
    return AttachmentRef("id", LINK_UTI, target, "T", None)


def test_link_uuid_reads_hand_made_token():
    att = _link_att("applenotes://showNote?identifier=ABC-1")
    assert link_uuid(att) == "abc-1"


def test_link_uuid_reads_synthesized_token():
    att = _link_att("applenotes:note/abc-1?ownerIdentifier=_o")
    assert link_uuid(att) == "abc-1"


def test_link_uuid_of_unrelated_target_is_none():
    assert link_uuid(_link_att("https://example.com/note/abc")) is None


def test_allowlist_is_select_all_copy_paste_undo_and_left():
    assert SECTION_KEY_ALLOWLIST == {
        (0, "command down"),
        (8, "command down"),
        (9, "command down"),
        (6, "command down"),
        (123, None),
    }


def test_every_op_every_range_plans_the_expected_characters():
    """Exhaustive sweep over small notes with empty paragraphs sprinkled in."""
    import itertools

    for pattern in itertools.product(["x", ""], repeat=5):
        texts = ["T", *pattern]
        s = _rsnap([para(t, 0 if i == 0 else None) for i, t in enumerate(texts)])
        full = "".join(p.text for p in s.paragraphs)
        n = len(texts)
        for a in range(1, n):
            for b in range(a, n + 1):
                for op in ("replace", "append", "delete"):
                    if op != "append" and a == b:
                        continue
                    if op == "append" and b < 2:
                        continue
                    try:
                        plan = plan_edit(s, Section(a, b, None, "fp"), op, "n")
                    except SectionError as err:
                        assert "blank right under the title" in str(err)
                        continue
                    assert 0 <= plan.start < plan.end <= len(full) or op == "delete"
                    if op != "delete":
                        assert _selected(s, plan) != ""
                        assert s.paragraphs[plan.edit_a].start == plan.start


def test_a_section_deep_in_a_long_note_plans_without_a_depth_limit():
    s = _rsnap([para("T", 0)] + [para(f"p{i}") for i in range(999)])
    plan = plan_edit(s, Section(500, 502, None, "fp"), "replace", "N")
    assert _selected(s, plan) == "p499\np500"
