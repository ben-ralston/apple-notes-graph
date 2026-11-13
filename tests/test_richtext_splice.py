"""Slicing a section out of copied richtext and splicing it back (issue #240).

Headless and CI-visible: real Notes richtext bytes from the fixture, real
parser and serializer. The whole-note section edit stands on these.
"""

import copy
from pathlib import Path

from apple_notes.richtext import NotesRichtextParser, NotesRichtextSerializer
from apple_notes.richtext.models import AttributeRun, NotesRichtext
from apple_notes.richtext.operations import (
    paragraph_spans,
    replace_range,
    slice_richtext,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "notes_richtext_sample_with_great_sentence.bin"
)
P, S = NotesRichtextParser(), NotesRichtextSerializer()


def _fixture():
    return P.parse(FIXTURE.read_bytes())


def _round_trip(rt):
    return P.parse(S.serialize(rt))


def _note(text, runs):
    """A small richtext on the fixture's archive; runs are (length, raw fields)."""
    base = _fixture()
    return NotesRichtext(
        text=text,
        runs=[AttributeRun(length=n, raw_data={"1": n, **raw}) for n, raw in runs],
        attachments=[],
        _archive_template=base._archive_template,
        _protobuf_typedef=copy.deepcopy(base._protobuf_typedef),
    )


def _middle(rt):
    spans = paragraph_spans(rt.text)
    return spans[len(spans) // 2]


def test_paragraph_spans_ignores_cr_and_line_separator():
    assert paragraph_spans("a\nb\rc\nd e") == [(0, 1), (2, 5), (6, 9)]


def test_paragraph_spans_keeps_empty_paragraphs():
    assert paragraph_spans("a\n\nb\n") == [(0, 1), (2, 2), (3, 4), (5, 5)]


def test_identity_splice_serializes_and_keeps_text():
    rt = _fixture()
    s, e = _middle(rt)
    frag = _round_trip(slice_richtext(rt, s, e))
    out = _round_trip(replace_range(rt, s, e, frag))
    assert out.text == rt.text
    assert sum(r.length for r in out.runs) == len(rt.text)


def test_splice_keeps_attachment_fields_outside_slice():
    """A slice's typedef lacks fields only other attachments use (the fixture's
    field 6 subfield 17); serializing the spliced note must not lose them."""
    rt = _fixture()
    assert any("17" in a.raw_data for a in rt.attachments)
    s, e = _middle(rt)
    frag = _round_trip(slice_richtext(rt, s, e))
    assert not frag.attachments
    out = _round_trip(replace_range(rt, s, e, frag))
    assert [a.raw_data for a in out.attachments] == [a.raw_data for a in rt.attachments]


def test_slice_keeps_only_referenced_attachments():
    rt = _fixture()
    pos, ident = 0, None
    for run in rt.runs:
        info = run.raw_data.get("12") if run.raw_data else None
        if isinstance(info, dict):
            ident, at = info["1"], pos
            break
        pos += run.length
    assert ident is not None
    inside = slice_richtext(rt, at, at + 1)
    assert [a.raw_data["2"] for a in inside.attachments] == [ident]
    assert len(rt.attachments) > 1


def test_replace_range_drops_attachments_of_replaced_text():
    rt = _fixture()
    pos = 0
    for run in rt.runs:
        info = run.raw_data.get("12") if run.raw_data else None
        if isinstance(info, dict):
            ident, at = info["1"], pos
            break
        pos += run.length
    empty = slice_richtext(rt, 0, 0)
    out = replace_range(rt, at, at + 1, empty)
    assert ident not in [a.raw_data["2"] for a in out.attachments]
    assert len(out.attachments) == len(rt.attachments) - 1


def test_run_across_boundary_keeps_style_on_both_pieces():
    style = {"2": {"1": 1}}
    rt = _note("title\none\ntwo\nthree", [(19, style)])
    s, e = paragraph_spans(rt.text)[2]
    inner = slice_richtext(rt, s, e)
    assert inner.runs[0].raw_data["2"] == {"1": 1}
    empty = slice_richtext(rt, 0, 0)
    out = replace_range(rt, s, e, empty)
    assert out.text == "title\none\n\nthree"
    assert [r.raw_data["2"] for r in out.runs] == [{"1": 1}, {"1": 1}]
    assert sum(r.length for r in out.runs) == len(out.text)


def test_non_bmp_before_section_splices_at_right_place():
    text = "😀 title\nkeep\nedit\nkeep"
    rt = _round_trip(_note(text, [(len(text), {})]))
    assert rt.text == text
    s, e = paragraph_spans(rt.text)[2]
    assert rt.text[s:e] == "edit"
    new = _note("EDITED", [(6, {})])
    out = _round_trip(replace_range(rt, s, e, new))
    assert out.text == "😀 title\nkeep\nEDITED\nkeep"


def test_empty_fragment_removes_range():
    rt = _note("title\none\ntwo", [(13, {})])
    s, e = paragraph_spans(rt.text)[1]
    out = replace_range(rt, s, e + 1, slice_richtext(rt, 0, 0))
    assert out.text == "title\ntwo"
    assert sum(r.length for r in out.runs) == len(out.text)


