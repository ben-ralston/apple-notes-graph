"""A bullet is paragraph style_type 100, not paragraph field 3 (issue #235).

Field 3 = 1 sits on most paragraphs Notes writes (dashed, numbered and checklist
items, a table's attachment line, plain text after it), so reading it as a
bullet put stray ``* `` markers in front of tables and the text after them.
"""

from pathlib import Path

from apple_notes.note_content import NoteContentReader
from apple_notes.protobuf_parser import NoteProtobufParser, ParagraphStyleInfo
from apple_notes.richtext.parser import NotesRichtextParser

FIXTURES = Path(__file__).parent / "fixtures"
BULLET = NoteProtobufParser.STYLE_BULLET_LIST
_READER = NoteContentReader.__new__(NoteContentReader)


def _styles(*paragraph_styles):
    runs = [{"1": 5, "2": style} for style in paragraph_styles]
    return NoteProtobufParser().extract_paragraph_styles(runs)


def test_style_type_100_is_a_bullet():
    (info,) = _styles({"1": 100, "3": 1})
    assert info.style_type == BULLET


def test_style_type_100_without_field_3_is_a_bullet():
    (info,) = _styles({"1": 100})
    assert info.style_type == BULLET


def test_field_3_alone_is_not_a_bullet():
    assert _styles({"3": 1}) == []


def test_field_3_on_a_checklist_is_not_a_bullet():
    assert _styles({"1": 103, "3": 1, "5": {"2": 0}}) == []


def test_block_quote_with_field_3_stays_a_block_quote():
    (info,) = _styles({"8": 1, "3": 1})
    assert info.style_type == NoteProtobufParser.STYLE_BLOCK_QUOTE


def _sample_lines_by_style():
    """Real runs from the committed clipboard sample: same paragraph-style
    message as NoteStore's ZDATA runs."""
    parser = NotesRichtextParser()
    data = (FIXTURES / "notes_richtext_sample_with_great_sentence.bin").read_bytes()
    message, _ = parser._decode_pinned(
        parser._extract_ns_data(parser._decode_archive(data))
    )
    text = parser._extract_text(message)
    infos = NoteProtobufParser().extract_paragraph_styles(message["5"])
    lines = _READER._build_paragraph_style_line_map(text, infos)
    rows = text.split("\n")
    return {rows[n]: style for n, style in lines.items()}


def test_real_sample_bullets_are_exactly_the_bulleted_lines():
    styles = _sample_lines_by_style()
    bullets = sorted(line for line, style in styles.items() if style == BULLET)
    assert bullets == ["Bulleted 2", "Bulleted list"]


def test_real_sample_table_and_following_text_are_not_bullets():
    styles = _sample_lines_by_style()
    assert "￼" not in styles
    assert "Normal text again." not in styles
    assert styles["Dashed list 2"] == NoteProtobufParser.STYLE_DASHED_LIST
    assert styles["Number item 2"] == NoteProtobufParser.STYLE_NUMBERED_LIST


def test_bullet_after_a_blank_line_keeps_its_style():
    """The old reader dropped a bullet that followed a blank line after body
    text, to hide the field-3 misreads. A real list right under an intro line
    must keep its marker."""
    text = "Intro\n\nitem\n"
    infos = [ParagraphStyleInfo(position=7, length=5, style_type=BULLET)]
    lines = _READER._build_paragraph_style_line_map(text, infos)
    assert lines == {2: BULLET}


def test_bullet_inside_a_block_quote_stays_a_block_quote():
    """The clipboard parser and sections._style_of check the quote first; the
    NoteStore reader must agree, or a rendered line and its paragraph kind
    disagree for section matching (#235 review)."""
    (info,) = _styles({"1": 100, "8": 1})
    assert info.style_type == NoteProtobufParser.STYLE_BLOCK_QUOTE
