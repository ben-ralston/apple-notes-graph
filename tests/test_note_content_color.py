"""Tests for color and table rendering in note content read path."""

from unittest.mock import MagicMock, patch

from apple_notes.colors import rgb_floats_to_ints
from apple_notes.note_content import NoteContentReader
from apple_notes.protobuf_parser import (
    NoteProtobufParser,
    ParagraphStyleInfo,
    TextFormatInfo,
)


class TestProtobufColorExtraction:
    """Tests for color extraction from protobuf attribute runs."""

    def setup_method(self):
        self.parser = NoteProtobufParser()

    def test_field_14_palette_index_purple(self):
        """Field 14 = 1 produces color 'purple'."""
        attribute_runs = [{"1": 5, "14": 1}]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color == "purple"

    def test_field_14_palette_index_pink(self):
        """Field 14 = 2 produces color 'pink'."""
        attribute_runs = [{"1": 5, "14": 2}]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color == "pink"

    def test_field_14_palette_index_orange(self):
        """Field 14 = 3 produces color 'orange'."""
        attribute_runs = [{"1": 5, "14": 3}]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color == "orange"

    def test_field_14_palette_index_green(self):
        """Field 14 = 4 produces color 'green'."""
        attribute_runs = [{"1": 5, "14": 4}]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color == "green"

    def test_field_14_palette_index_blue(self):
        """Field 14 = 5 produces color 'blue'."""
        attribute_runs = [{"1": 5, "14": 5}]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color == "blue"

    def test_field_14_all_known_indices(self):
        """All known palette indices (1-5) produce valid color names."""
        expected = {
            1: "purple",
            2: "pink",
            3: "orange",
            4: "green",
            5: "blue",
        }
        for index, name in expected.items():
            attribute_runs = [{"1": 5, "14": index}]
            infos = self.parser.extract_text_formatting(attribute_runs)
            assert len(infos) == 1, f"No format info for index {index}"
            assert infos[0].color == name, (
                f"Index {index}: expected {name}, got {infos[0].color}"
            )

    def test_field_14_unknown_index_no_color(self):
        """Unknown palette index (not in map) produces no color."""
        attribute_runs = [{"1": 5, "14": 99}]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 0  # No formatting at all

    def test_field_14_invalid_indices_6_through_9_no_color(self):
        """Indices 6-9 are invalid (crash Notes) and should produce no color."""
        for index in [6, 7, 8, 9]:
            attribute_runs = [{"1": 5, "14": index}]
            infos = self.parser.extract_text_formatting(attribute_runs)
            assert len(infos) == 0, f"Index {index} should not produce color"

    def test_field_10_fallback_rgb_non_highlight_uses_hex(self):
        """Field 10 with non-highlight RGB (red) produces hex, not name."""
        r_int, g_int, b_int = rgb_floats_to_ints(1.0, 0.2314, 0.1882)  # "red"
        attribute_runs = [
            {"1": 5, "10": {"1": r_int, "2": g_int, "3": b_int, "4": 1065353216}},
        ]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color is not None
        assert infos[0].color.startswith("#")  # red is not a highlight color

    def test_field_10_fallback_rgb_highlight_uses_name(self):
        """Field 10 with highlight RGB (blue) produces color name."""
        r_int, g_int, b_int = rgb_floats_to_ints(0.0, 0.4784, 1.0)  # "blue"
        attribute_runs = [
            {"1": 5, "10": {"1": r_int, "2": g_int, "3": b_int, "4": 1065353216}},
        ]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color == "blue"

    def test_field_10_unknown_rgb_uses_hex(self):
        """Field 10 with unknown RGB values produces hex string."""
        r_int, g_int, b_int = rgb_floats_to_ints(1.0, 1.0, 1.0)  # white
        attribute_runs = [
            {"1": 5, "10": {"1": r_int, "2": g_int, "3": b_int}},
        ]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color is not None
        assert infos[0].color.startswith("#")

    def test_field_14_takes_priority_over_field_10(self):
        """When both field 14 and field 10 are present, field 14 wins."""
        r_int, g_int, b_int = rgb_floats_to_ints(1.0, 0.0, 0.0)
        attribute_runs = [
            {"1": 5, "14": 3, "10": {"1": r_int, "2": g_int, "3": b_int}},  # 3 = orange
        ]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].color == "orange"

    def test_no_color_no_format(self):
        """Run without formatting or color is not included."""
        attribute_runs = [{"1": 5}]
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 0

    def test_color_only_no_other_format(self):
        """Run with only color (no bold/italic/etc) is still included."""
        attribute_runs = [{"1": 5, "14": 5}]  # blue
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].is_bold is False
        assert infos[0].color == "blue"

    def test_bold_with_color(self):
        """Run with both bold and color."""
        attribute_runs = [{"1": 5, "5": 1, "14": 2}]  # bold + pink
        infos = self.parser.extract_text_formatting(attribute_runs)
        assert len(infos) == 1
        assert infos[0].is_bold is True
        assert infos[0].color == "pink"


class TestNoteContentColorRendering:
    """Tests for color rendering in _build_format_replacements."""

    def test_color_produces_color_tag(self):
        """TextFormatInfo with color produces {color:name} tags."""
        reader = NoteContentReader.__new__(NoteContentReader)
        note_text = "hello"
        format_infos = [TextFormatInfo(position=0, length=5, color="red")]
        replacements = reader._build_format_replacements(note_text, format_infos, set())
        assert len(replacements) == 1
        assert "{color:red}" in replacements[0]["text"]
        assert "{/color}" in replacements[0]["text"]

    def test_bold_and_color(self):
        """Bold + color produces nested formatting."""
        reader = NoteContentReader.__new__(NoteContentReader)
        note_text = "hello"
        format_infos = [
            TextFormatInfo(position=0, length=5, is_bold=True, color="blue")
        ]
        replacements = reader._build_format_replacements(note_text, format_infos, set())
        assert len(replacements) == 1
        text = replacements[0]["text"]
        # Color should be inside bold: **{color:blue}hello{/color}**
        assert text.startswith("**")
        assert text.endswith("**")
        assert "{color:blue}" in text

    def test_hex_color_produces_hex_tag(self):
        """Hex color string produces {color:#XXXXXX} tag."""
        reader = NoteContentReader.__new__(NoteContentReader)
        note_text = "hello"
        format_infos = [TextFormatInfo(position=0, length=5, color="#FFFFFF")]
        replacements = reader._build_format_replacements(note_text, format_infos, set())
        assert len(replacements) == 1
        text = replacements[0]["text"]
        assert "{color:#FFFFFF}" in text
        assert "{/color}" in text


class TestFormatMarkersHugText:
    """Markers must not wrap edge whitespace (review finding on NTG-007).

    The write path's italic flanking rule rejects '* word *', so a run whose
    text has leading/trailing spaces must read back as ' *word* ' — whitespace
    outside the markers — or a read→edit→write round-trip drops the italics
    and writes literal asterisks.
    """

    def setup_method(self):
        self.reader = NoteContentReader.__new__(NoteContentReader)

    def test_italic_run_with_edge_spaces_hoists_whitespace(self):
        note_text = "before word after"
        # Run covers " word " (positions 6-11, spaces included)
        format_infos = [TextFormatInfo(position=6, length=6, is_italic=True)]

        replacements = self.reader._build_format_replacements(
            note_text, format_infos, set()
        )

        assert replacements == [{"position": 6, "length": 6, "text": " *word* "}]

    def test_hoisted_italic_reparses_as_italic(self):
        from apple_notes.richtext import FontStyle
        from apple_notes.richtext.markdown_converter import parse_inline_formatting

        note_text = "before word after"
        format_infos = [TextFormatInfo(position=6, length=6, is_italic=True)]
        replacements = self.reader._build_format_replacements(
            note_text, format_infos, set()
        )

        spans = parse_inline_formatting(replacements[0]["text"])
        italic = [s for s in spans if s.font_style == FontStyle.ITALIC]
        assert [s.text for s in italic] == ["word"]

    def test_multiline_run_hoists_whitespace_per_segment(self):
        note_text = "one \ntwo"
        format_infos = [TextFormatInfo(position=0, length=8, is_italic=True)]

        replacements = self.reader._build_format_replacements(
            note_text, format_infos, set()
        )

        assert replacements == [
            {"position": 0, "length": 4, "text": "*one* "},
            {"position": 5, "length": 3, "text": "*two*"},
        ]


class TestAdjustLineMaps:
    """Tests for _adjust_line_maps after table insertion shifts lines."""

    def test_table_before_heading_shifts_line(self):
        """A table replacement expanding 1 char to multi-line shifts heading line."""
        reader = NoteContentReader.__new__(NoteContentReader)
        # Original: line 0 = table (U+FFFC), line 1 = heading text
        note_text = "\ufffc\nMy Heading\n"
        paragraph_style_lines = {1: 1}  # line 1 = heading style
        checklist_lines = {}

        # Table replacement: 1 char → 3-line table (2 extra newlines)
        replacements = [
            {"position": 0, "length": 1, "text": "| A | B |\n| --- | --- |\n| 1 | 2 |"}
        ]

        adj_checklist, adj_para = reader._adjust_line_maps(
            note_text, replacements, checklist_lines, paragraph_style_lines
        )
        # The heading was on original line 1, table added 2 newlines,
        # so heading should now be on line 3
        assert 3 in adj_para
        assert adj_para[3] == 1

    def test_table_before_checklist_shifts_line(self):
        """A table replacement shifts checklist line numbers."""
        reader = NoteContentReader.__new__(NoteContentReader)
        note_text = "\ufffc\nTask item\n"
        checklist_lines = {1: False}  # line 1 = unchecked checklist item
        paragraph_style_lines = {}

        replacements = [
            {
                "position": 0,
                "length": 1,
                "text": "| H1 | H2 |\n| --- | --- |\n| a | b |",
            }
        ]

        adj_checklist, adj_para = reader._adjust_line_maps(
            note_text, replacements, checklist_lines, paragraph_style_lines
        )
        assert 3 in adj_checklist
        assert adj_checklist[3] is False

    def test_no_tables_preserves_existing_behavior(self):
        """Without tables, line maps are unchanged (regression test)."""
        reader = NoteContentReader.__new__(NoteContentReader)
        note_text = "Hello\nWorld\n"
        checklist_lines = {1: True}
        paragraph_style_lines = {0: 1}

        # A link replacement that doesn't change line count
        replacements = [{"position": 0, "length": 5, "text": "[[Hello]]"}]

        adj_checklist, adj_para = reader._adjust_line_maps(
            note_text, replacements, checklist_lines, paragraph_style_lines
        )
        assert adj_checklist == {1: True}
        assert adj_para == {0: 1}
