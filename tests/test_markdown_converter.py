"""Tests for markdown to Apple Notes richtext conversion.

These tests verify that markdown text is correctly converted to the Notes
richtext format, including paragraph types, checklist states, and text formatting.
"""

from pathlib import Path

import pytest

from apple_notes.richtext import (
    FontStyle,
    NotesRichtextParser,
    ParagraphType,
    parse_markdown,
)
from apple_notes.richtext.markdown_converter import (
    MarkdownConversionError,
    MarkdownLine,
    PendingKeyboardAction,
    RichtextConversionResult,
    TableBlock,
    TextSpan,
    _ensure_complete_typedef,
    _make_placeholder,
    build_richtext,
    markdown_to_richtext,
    markdown_to_richtext_new,
    parse_inline_formatting,
    parse_markdown,
    parse_markdown_line,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def template_data() -> bytes:
    """Load a template fixture for serialization."""
    return (FIXTURES / "notes_richtext_sample_with_great_sentence.bin").read_bytes()


@pytest.fixture
def parser() -> NotesRichtextParser:
    """Create a parser instance."""
    return NotesRichtextParser()


class TestParseInlineFormatting:
    """Tests for inline text formatting parsing."""

    def test_plain_text(self) -> None:
        """Plain text returns single span with NORMAL style."""
        spans = parse_inline_formatting("Hello world")

        assert len(spans) == 1
        assert spans[0].text == "Hello world"
        assert spans[0].font_style == FontStyle.NORMAL

    def test_bold_text(self) -> None:
        """Bold text is detected and styled correctly."""
        spans = parse_inline_formatting("Hello **world**")

        assert len(spans) == 2
        assert spans[0].text == "Hello "
        assert spans[0].font_style == FontStyle.NORMAL
        assert spans[1].text == "world"
        assert spans[1].font_style == FontStyle.BOLD

    def test_italic_text(self) -> None:
        """Italic text is detected and styled correctly."""
        spans = parse_inline_formatting("Hello *world*")

        assert len(spans) == 2
        assert spans[0].text == "Hello "
        assert spans[0].font_style == FontStyle.NORMAL
        assert spans[1].text == "world"
        assert spans[1].font_style == FontStyle.ITALIC

    def test_bold_italic_text(self) -> None:
        """Bold italic text is detected and styled correctly."""
        spans = parse_inline_formatting("Hello ***world***")

        assert len(spans) == 2
        assert spans[0].text == "Hello "
        assert spans[0].font_style == FontStyle.NORMAL
        assert spans[1].text == "world"
        assert spans[1].font_style == FontStyle.BOLD_ITALIC

    def test_loose_asterisks_in_prose_preserved(self) -> None:
        """Spaced asterisks used as math/prose are NOT italic markers (NTG-007).

        An opening * followed by whitespace (or a closing * preceded by it)
        fails the flanking rule, so the characters must survive verbatim.
        """
        text = "5 * 3 * 2 = 30"
        spans = parse_inline_formatting(text)

        assert "".join(s.text for s in spans) == text
        assert all(s.font_style == FontStyle.NORMAL for s in spans)

    def test_asterisk_pair_with_inner_spaces_preserved(self) -> None:
        """`a * b * c` keeps both asterisks as literal text."""
        text = "a * b * c"
        spans = parse_inline_formatting(text)

        assert "".join(s.text for s in spans) == text
        assert all(s.font_style == FontStyle.NORMAL for s in spans)

    def test_tight_asterisks_still_italic(self) -> None:
        """Flanking-valid single asterisks still parse as italic."""
        spans = parse_inline_formatting("5 *3* 2")

        assert len(spans) == 3
        assert spans[1].text == "3"
        assert spans[1].font_style == FontStyle.ITALIC

    def test_mismatched_flanking_asterisks_preserved(self) -> None:
        """`*open but * spaced close` is not an italic pair."""
        text = "keep *this * literal"
        spans = parse_inline_formatting(text)

        assert "".join(s.text for s in spans) == text
        assert all(s.font_style == FontStyle.NORMAL for s in spans)

    def test_mixed_formatting(self) -> None:
        """Multiple formatting styles in one line."""
        spans = parse_inline_formatting("**bold** and *italic* text")

        # Should have: bold, " and ", italic, " text"
        assert len(spans) == 4
        assert spans[0].text == "bold"
        assert spans[0].font_style == FontStyle.BOLD
        assert spans[1].text == " and "
        assert spans[1].font_style == FontStyle.NORMAL
        assert spans[2].text == "italic"
        assert spans[2].font_style == FontStyle.ITALIC
        assert spans[3].text == " text"
        assert spans[3].font_style == FontStyle.NORMAL

    def test_empty_string(self) -> None:
        """Empty string returns single empty span."""
        spans = parse_inline_formatting("")

        assert len(spans) == 1
        assert spans[0].text == ""

    def test_inline_code_stripped(self) -> None:
        """Inline code has backticks stripped and text is consolidated."""
        spans = parse_inline_formatting("Use `code` here")

        # Backticks are stripped and adjacent NORMAL spans are consolidated
        # into a single span since Notes doesn't have inline code style
        assert len(spans) == 1
        assert spans[0].text == "Use code here"
        assert spans[0].font_style == FontStyle.NORMAL


class TestParseInlineLinks:
    """Tests for inline markdown link parsing."""

    def test_simple_link(self) -> None:
        """[text](url) produces TextSpan with link_url."""
        spans = parse_inline_formatting("Click [here](https://example.com) please")

        assert len(spans) == 3
        assert spans[0].text == "Click "
        assert spans[0].link_url is None
        assert spans[1].text == "here"
        assert spans[1].link_url == "https://example.com"
        assert spans[2].text == " please"
        assert spans[2].link_url is None

    def test_link_only(self) -> None:
        """A line with just a link."""
        spans = parse_inline_formatting("[Google](https://google.com)")

        assert len(spans) == 1
        assert spans[0].text == "Google"
        assert spans[0].link_url == "https://google.com"

    def test_two_links_not_consolidated(self) -> None:
        """Adjacent links with different URLs are not merged."""
        spans = parse_inline_formatting("[a](https://a.com) [b](https://b.com)")

        # Should have: link "a", plain " ", link "b"
        link_spans = [s for s in spans if s.link_url is not None]
        assert len(link_spans) == 2
        assert link_spans[0].text == "a"
        assert link_spans[0].link_url == "https://a.com"
        assert link_spans[1].text == "b"
        assert link_spans[1].link_url == "https://b.com"

    def test_link_inside_bold(self) -> None:
        """Link inside bold markers is parsed as bold text with link."""
        spans = parse_inline_formatting(
            "**Check out [Google](https://google.com) for more**"
        )

        assert len(spans) == 3
        assert spans[0].text == "Check out "
        assert spans[0].font_style == FontStyle.BOLD
        assert spans[0].link_url is None
        assert spans[1].text == "Google"
        assert spans[1].font_style == FontStyle.BOLD
        assert spans[1].link_url == "https://google.com"
        assert spans[2].text == " for more"
        assert spans[2].font_style == FontStyle.BOLD
        assert spans[2].link_url is None

    def test_link_inside_italic(self) -> None:
        """Link inside italic markers is parsed as italic text with link."""
        spans = parse_inline_formatting("*See [here](https://example.com) for details*")

        assert len(spans) == 3
        assert spans[0].text == "See "
        assert spans[0].font_style == FontStyle.ITALIC
        assert spans[0].link_url is None
        assert spans[1].text == "here"
        assert spans[1].font_style == FontStyle.ITALIC
        assert spans[1].link_url == "https://example.com"
        assert spans[2].text == " for details"
        assert spans[2].font_style == FontStyle.ITALIC
        assert spans[2].link_url is None

    def test_link_inside_bold_italic(self) -> None:
        """Link inside bold-italic markers is parsed correctly."""
        spans = parse_inline_formatting("***Visit [site](https://example.com)***")

        assert len(spans) == 2
        assert spans[0].text == "Visit "
        assert spans[0].font_style == FontStyle.BOLD_ITALIC
        assert spans[0].link_url is None
        assert spans[1].text == "site"
        assert spans[1].font_style == FontStyle.BOLD_ITALIC
        assert spans[1].link_url == "https://example.com"

    def test_only_link_inside_bold(self) -> None:
        """Bold wrapping only a link produces single bold+link span."""
        spans = parse_inline_formatting("**[Google](https://google.com)**")

        assert len(spans) == 1
        assert spans[0].text == "Google"
        assert spans[0].font_style == FontStyle.BOLD
        assert spans[0].link_url == "https://google.com"

    def test_bold_and_link_mixed(self) -> None:
        """Bold and link in same line are both parsed."""
        spans = parse_inline_formatting("**bold** and [link](https://url.com)")

        bold_spans = [s for s in spans if s.font_style == FontStyle.BOLD]
        link_spans = [s for s in spans if s.link_url is not None]
        assert len(bold_spans) == 1
        assert bold_spans[0].text == "bold"
        assert len(link_spans) == 1
        assert link_spans[0].text == "link"
        assert link_spans[0].link_url == "https://url.com"

    def test_link_with_path_segments(self) -> None:
        """Link with path segments in URL is parsed correctly."""
        spans = parse_inline_formatting("[wiki](https://en.wikipedia.org/wiki/Test)")

        link_spans = [s for s in spans if s.link_url is not None]
        assert len(link_spans) == 1
        assert link_spans[0].link_url == "https://en.wikipedia.org/wiki/Test"

    def test_link_with_parentheses_in_url(self) -> None:
        """URL containing parentheses like Wikipedia links is parsed correctly."""
        spans = parse_inline_formatting(
            "[Barcelona](https://en.wikipedia.org/wiki/Barcelona_(city))"
        )

        link_spans = [s for s in spans if s.link_url is not None]
        assert len(link_spans) == 1
        assert link_spans[0].text == "Barcelona"
        assert (
            link_spans[0].link_url == "https://en.wikipedia.org/wiki/Barcelona_(city)"
        )

    def test_link_with_parentheses_followed_by_text(self) -> None:
        """URL with parens followed by plain text parses correctly."""
        spans = parse_inline_formatting(
            "See [Barcelona](https://en.wikipedia.org/wiki/Barcelona_(city)) for info"
        )

        assert len(spans) == 3
        assert spans[0].text == "See "
        assert spans[0].link_url is None
        assert spans[1].text == "Barcelona"
        assert spans[1].link_url == "https://en.wikipedia.org/wiki/Barcelona_(city)"
        assert spans[2].text == " for info"
        assert spans[2].link_url is None

    def test_plain_text_link_url_is_none(self) -> None:
        """Plain text spans have link_url=None."""
        spans = parse_inline_formatting("no links here")

        assert len(spans) == 1
        assert spans[0].link_url is None


class TestParseMarkdownLine:
    """Tests for line-level markdown parsing."""

    def test_plain_line(self) -> None:
        """Plain line has DEFAULT paragraph type."""
        line = parse_markdown_line("Just some text")

        assert line.paragraph_type == ParagraphType.DEFAULT
        assert line.text == "Just some text"
        assert not line.checklist_checked

    def test_checklist_unchecked(self) -> None:
        """Unchecked checklist line."""
        line = parse_markdown_line("- [ ] Task to do")

        assert line.paragraph_type == ParagraphType.CHECKLIST
        assert line.checklist_checked is False
        assert line.text == "Task to do"

    def test_checklist_checked(self) -> None:
        """Checked checklist line."""
        line = parse_markdown_line("- [x] Completed task")

        assert line.paragraph_type == ParagraphType.CHECKLIST
        assert line.checklist_checked is True
        assert line.text == "Completed task"

    def test_checklist_checked_uppercase_x(self) -> None:
        """Checked checklist with uppercase X."""
        line = parse_markdown_line("- [X] Also completed")

        assert line.paragraph_type == ParagraphType.CHECKLIST
        assert line.checklist_checked is True
        assert line.text == "Also completed"

    def test_heading_level_2(self) -> None:
        """Level 2 heading (##)."""
        line = parse_markdown_line("## Section Title")

        assert line.paragraph_type == ParagraphType.HEADING
        assert line.text == "Section Title"

    def test_heading_level_3(self) -> None:
        """Level 3 heading (###)."""
        line = parse_markdown_line("### Subsection")

        assert line.paragraph_type == ParagraphType.SUBHEADING
        assert line.text == "Subsection"

    def test_dashed_list(self) -> None:
        """Dashed list item."""
        line = parse_markdown_line("- List item")

        assert line.paragraph_type == ParagraphType.DASHED_LIST
        assert line.text == "List item"

    def test_bullet_list(self) -> None:
        """Bullet list item (asterisk)."""
        line = parse_markdown_line("* Bullet item")

        assert line.paragraph_type == ParagraphType.BULLET_LIST
        assert line.text == "Bullet item"

    def test_numbered_list(self) -> None:
        """Numbered list item."""
        line = parse_markdown_line("1. First item")

        assert line.paragraph_type == ParagraphType.NUMBERED_LIST
        assert line.text == "First item"

    def test_numbered_list_any_number(self) -> None:
        """Numbered list accepts any number."""
        line = parse_markdown_line("42. Item forty-two")

        assert line.paragraph_type == ParagraphType.NUMBERED_LIST
        assert line.text == "Item forty-two"

    def test_block_quote(self) -> None:
        """Block quote line."""
        line = parse_markdown_line("> Quoted text")

        assert line.paragraph_type == ParagraphType.BLOCK_QUOTE
        assert line.text == "Quoted text"

    def test_monospace_line(self) -> None:
        """Monospace line (entire line in backticks)."""
        line = parse_markdown_line("`code block`")

        assert line.paragraph_type == ParagraphType.MONOSPACE
        assert line.text == "code block"

    def test_checklist_with_formatting(self) -> None:
        """Checklist with inline formatting."""
        line = parse_markdown_line("- [ ] **Important** task")

        assert line.paragraph_type == ParagraphType.CHECKLIST
        assert line.checklist_checked is False

        # Should have two spans: "Important" (bold) and " task" (normal)
        assert len(line.spans) == 2
        assert line.spans[0].text == "Important"
        assert line.spans[0].font_style == FontStyle.BOLD
        assert line.spans[1].text == " task"
        assert line.spans[1].font_style == FontStyle.NORMAL


class TestParseMarkdown:
    """Tests for full document parsing."""

    def test_simple_document(self) -> None:
        """Parse a simple multi-line document."""
        md = """## Title
- [ ] Task 1
- [x] Task 2"""

        doc = parse_markdown(md)

        assert len(doc.lines) == 3
        assert doc.lines[0].paragraph_type == ParagraphType.HEADING
        assert doc.lines[1].paragraph_type == ParagraphType.CHECKLIST
        assert doc.lines[1].checklist_checked is False
        assert doc.lines[2].paragraph_type == ParagraphType.CHECKLIST
        assert doc.lines[2].checklist_checked is True

    def test_document_text_property(self) -> None:
        """Document text property returns joined lines."""
        md = """Line 1
Line 2
Line 3"""

        doc = parse_markdown(md)

        assert doc.text == "Line 1\nLine 2\nLine 3"

    def test_empty_lines_preserved(self) -> None:
        """Empty lines are preserved in document."""
        md = """First

Second"""

        doc = parse_markdown(md)

        assert len(doc.lines) == 3
        assert doc.lines[1].text == ""


class TestBuildRichtext:
    """Tests for building NotesRichtext from parsed markdown."""

    def test_simple_text(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Build richtext from simple text."""
        md = "Hello world"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        assert richtext.text == "Hello world"
        assert len(richtext.runs) >= 1
        assert pending == []

        # Total run lengths should equal text length
        total_length = sum(r.length for r in richtext.runs)
        assert total_length == len(richtext.text)

    def test_multiline_text(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Build richtext from multiline text."""
        md = """Line 1
Line 2
Line 3"""
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        assert richtext.text == "Line 1\nLine 2\nLine 3"
        assert pending == []

        # Total run lengths should equal text length
        total_length = sum(r.length for r in richtext.runs)
        assert total_length == len(richtext.text)

    def test_checklist_paragraph_type(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Checklist lines have correct paragraph type."""
        md = "- [ ] Unchecked task"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        # Find run with paragraph style
        checklist_runs = [
            r
            for r in richtext.runs
            if r.paragraph and r.paragraph.type == ParagraphType.CHECKLIST
        ]
        assert len(checklist_runs) >= 1
        assert checklist_runs[0].paragraph.checklist_checked is False

    def test_checked_checklist(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Checked checklist has correct checked state."""
        md = "- [x] Completed task"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        checklist_runs = [
            r
            for r in richtext.runs
            if r.paragraph and r.paragraph.type == ParagraphType.CHECKLIST
        ]
        assert len(checklist_runs) >= 1
        assert checklist_runs[0].paragraph.checklist_checked is True

    def test_bold_formatting(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Bold text has correct font style."""
        md = "Hello **world**"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        bold_runs = [r for r in richtext.runs if r.font_style == FontStyle.BOLD]
        assert len(bold_runs) >= 1
        # The bold run should cover "world" (5 chars)
        assert any(r.length == 5 for r in bold_runs)


class TestBuildRichtextWithLinks:
    """Tests for building NotesRichtext with external hyperlinks."""

    def test_link_produces_attribute_run_with_link_url(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Link span produces AttributeRun with link_url set."""
        md = "Visit [Google](https://google.com) today"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        link_runs = [r for r in richtext.runs if r.link_url is not None]
        assert len(link_runs) == 1
        assert link_runs[0].link_url == "https://google.com"
        assert link_runs[0].length == len("Google")

    def test_link_raw_data_has_field_9(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Link run's raw_data has field 9 with URL bytes."""
        md = "[example](https://example.com)"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        link_runs = [r for r in richtext.runs if r.link_url is not None]
        assert len(link_runs) == 1
        assert "9" in link_runs[0].raw_data
        assert link_runs[0].raw_data["9"] == b"https://example.com"

    def test_non_link_runs_have_no_link_url(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Runs without links have link_url=None."""
        md = "plain text [link](https://url.com) more text"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        non_link_runs = [r for r in richtext.runs if r.link_url is None]
        assert len(non_link_runs) >= 2  # "plain text " and " more text"

    def test_run_lengths_valid_with_links(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Total run lengths still equal text length when links present."""
        md = "Start [link1](https://a.com) middle [link2](https://b.com) end"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        total_length = sum(r.length for r in richtext.runs)
        assert total_length == len(richtext.text)


class TestMarkdownToRichtext:
    """Tests for the main conversion function."""

    def test_full_conversion(self, template_data: bytes) -> None:
        """Full markdown to binary conversion."""
        md = """## Test Heading

- [ ] Task 1
- [x] Task 2

**Bold text** and *italic text*"""

        result = markdown_to_richtext(md, template_data)

        # Should return RichtextConversionResult with bytes
        assert isinstance(result, RichtextConversionResult)
        assert isinstance(result.data, bytes)
        assert len(result.data) > 0
        assert result.pending_actions == []

    def test_roundtrip_text_preserved(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Text content is preserved through conversion."""
        md = "Simple test text"

        result = markdown_to_richtext(md, template_data)
        parsed = parser.parse(result.data)

        assert parsed.text == "Simple test text"

    def test_invalid_template_raises(self) -> None:
        """Invalid template data raises error."""
        with pytest.raises(MarkdownConversionError):
            markdown_to_richtext("test", b"not valid plist data")


class TestLinkRoundtrip:
    """Tests for external hyperlink roundtrip (markdown → binary → parse)."""

    def test_link_survives_roundtrip(self, template_data: bytes) -> None:
        """Link URL is preserved through markdown → binary → parse."""
        md = "Visit [Google](https://google.com) today"

        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "Visit Google today"

        link_runs = [r for r in parsed.runs if r.link_url is not None]
        assert len(link_runs) == 1
        assert link_runs[0].link_url == "https://google.com"

    def test_link_roundtrip_with_new_template(self) -> None:
        """Link URL preserved using bundled template."""
        md = "[example](https://example.com)"

        result = markdown_to_richtext_new(md)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "example"

        link_runs = [r for r in parsed.runs if r.link_url is not None]
        assert len(link_runs) == 1
        assert link_runs[0].link_url == "https://example.com"

    def test_multiple_links_roundtrip(self, template_data: bytes) -> None:
        """Multiple links in same text all survive roundtrip."""
        md = "[a](https://a.com) and [b](https://b.com)"

        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        link_runs = [r for r in parsed.runs if r.link_url is not None]
        assert len(link_runs) == 2
        urls = {r.link_url for r in link_runs}
        assert urls == {"https://a.com", "https://b.com"}


class TestParseInlineNoteLinks:
    """Tests for [[Note Name]] wiki-link parsing."""

    def test_note_link_parsed(self) -> None:
        """[[Note Name]] produces TextSpan with note_link."""
        spans = parse_inline_formatting("See [[My Note]] for details")

        assert len(spans) == 3
        assert spans[0].text == "See "
        assert spans[0].note_link is None
        assert spans[1].text == "My Note"
        assert spans[1].note_link == "My Note"
        assert spans[2].text == " for details"
        assert spans[2].note_link is None

    def test_note_link_only(self) -> None:
        """A line with just a note link."""
        spans = parse_inline_formatting("[[Test Note]]")

        assert len(spans) == 1
        assert spans[0].text == "Test Note"
        assert spans[0].note_link == "Test Note"

    def test_two_note_links(self) -> None:
        """Two note links are both parsed."""
        spans = parse_inline_formatting("[[Note A]] and [[Note B]]")

        link_spans = [s for s in spans if s.note_link is not None]
        assert len(link_spans) == 2
        assert link_spans[0].note_link == "Note A"
        assert link_spans[1].note_link == "Note B"

    def test_note_link_not_confused_with_external_link(self) -> None:
        """[[link]] and [text](url) are parsed as different types."""
        spans = parse_inline_formatting("[[Note]] and [link](https://url.com)")

        note_spans = [s for s in spans if s.note_link is not None]
        link_spans = [s for s in spans if s.link_url is not None]
        assert len(note_spans) == 1
        assert note_spans[0].note_link == "Note"
        assert len(link_spans) == 1
        assert link_spans[0].link_url == "https://url.com"


class TestBuildRichtextWithNoteLinks:
    """Tests for building NotesRichtext with internal note links (placeholder approach)."""

    def test_resolved_note_link_uses_placeholder(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Resolved [[Note Name]] becomes a placeholder in text."""
        md = "See [[My Note]] here"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        resolver = lambda name: "fake-uuid-1234" if name == "My Note" else None
        richtext, pending = build_richtext(doc, template, link_resolver=resolver)

        # Text should have placeholder instead of "My Note" or FFFC
        expected_placeholder = _make_placeholder("LINK", 0)
        assert expected_placeholder in richtext.text
        assert "[[My Note]]" not in richtext.text
        assert "\ufffc" not in richtext.text

    def test_resolved_note_link_creates_pending_action(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Resolved [[Note Name]] creates a pending wiki_link action."""
        md = "See [[My Note]] here"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        resolver = lambda name: "fake-uuid-1234" if name == "My Note" else None
        richtext, pending = build_richtext(doc, template, link_resolver=resolver)

        assert len(pending) == 1
        assert pending[0].action_type == "wiki_link"
        assert pending[0].text_to_type == "My Note"
        assert pending[0].original_markdown == "[[My Note]]"
        assert pending[0].placeholder == _make_placeholder("LINK", 0)

    def test_unresolved_note_link_stays_as_text(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Unresolved [[Note Name]] stays as plain text, no pending action."""
        md = "See [[Unknown Note]] here"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        resolver = lambda name: None  # Can't resolve any name
        richtext, pending = build_richtext(doc, template, link_resolver=resolver)

        assert "[[Unknown Note]]" in richtext.text
        assert "\ufffc" not in richtext.text
        assert pending == []

    def test_no_resolver_keeps_plain_text(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Without resolver, [[Note Name]] stays as plain text."""
        md = "See [[My Note]] here"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        assert "[[My Note]]" in richtext.text
        assert "\ufffc" not in richtext.text
        assert pending == []

    def test_resolved_link_no_attachment(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Resolved link does NOT create protobuf attachments (uses keyboard instead)."""
        md = "See [[My Note]] here"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        resolver = lambda name: "fake-uuid-1234" if name == "My Note" else None
        richtext, pending = build_richtext(doc, template, link_resolver=resolver)

        # No protobuf attachments — the link will be typed via keyboard
        assert len(richtext.attachments) == 0
        assert len(pending) == 1

    def test_resolved_link_run_length_matches_placeholder(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """The placeholder run has length matching the placeholder text."""
        md = "[[My Note]]"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        resolver = lambda name: "fake-uuid-1234" if name == "My Note" else None
        richtext, pending = build_richtext(doc, template, link_resolver=resolver)

        expected_placeholder = _make_placeholder("LINK", 0)
        assert richtext.text == expected_placeholder
        total_length = sum(r.length for r in richtext.runs)
        assert total_length == len(expected_placeholder)

    def test_run_lengths_valid_with_resolved_links(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Total run lengths equal text length with resolved links."""
        md = "Start [[Note A]] middle [[Note B]] end"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        resolver = lambda name: f"uuid-{name.lower().replace(' ', '-')}"
        richtext, pending = build_richtext(doc, template, link_resolver=resolver)

        total_length = sum(r.length for r in richtext.runs)
        assert total_length == len(richtext.text)
        assert len(pending) == 2

    def test_multiple_links_have_unique_placeholders(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Multiple resolved links get unique placeholder strings."""
        md = "[[Note A]] and [[Note B]]"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        resolver = lambda name: f"uuid-{name}"
        richtext, pending = build_richtext(doc, template, link_resolver=resolver)

        assert len(pending) == 2
        assert pending[0].placeholder != pending[1].placeholder
        assert pending[0].placeholder == _make_placeholder("LINK", 0)
        assert pending[1].placeholder == _make_placeholder("LINK", 1)


class TestParseInlineHashtags:
    """Tests for explicit {#tag} parsing (issue #100).

    Only the explicit ``{#tag}`` syntax triggers an Apple Notes tag; plain
    ``#word`` in prose is left as literal text.
    """

    def test_explicit_tag_at_start(self) -> None:
        """{#tag} at start of text is parsed as a tag."""
        spans = parse_inline_formatting("{#todo} some text")

        hashtag_spans = [s for s in spans if s.hashtag is not None]
        assert len(hashtag_spans) == 1
        assert hashtag_spans[0].text == "#todo"
        assert hashtag_spans[0].hashtag == "todo"

    def test_explicit_tag_mid_text(self) -> None:
        """{#tag} in the middle of text is parsed as a tag."""
        spans = parse_inline_formatting("text {#important} here")

        hashtag_spans = [s for s in spans if s.hashtag is not None]
        assert len(hashtag_spans) == 1
        assert hashtag_spans[0].text == "#important"
        assert hashtag_spans[0].hashtag == "important"

    def test_multiple_explicit_tags(self) -> None:
        """Multiple {#tags} are all parsed."""
        spans = parse_inline_formatting("{#tag1} {#tag2} {#tag3}")

        hashtag_spans = [s for s in spans if s.hashtag is not None]
        assert len(hashtag_spans) == 3

    def test_hyphenated_tag(self) -> None:
        """{#tag} with a hyphen keeps the full name (fixes old \\w+ bug)."""
        spans = parse_inline_formatting("{#daily-note}")

        hashtag_spans = [s for s in spans if s.hashtag is not None]
        assert len(hashtag_spans) == 1
        assert hashtag_spans[0].hashtag == "daily-note"

    def test_plain_hashtag_is_literal(self) -> None:
        """Plain #word is NOT a tag — written as literal text."""
        spans = parse_inline_formatting("#todo some text")

        hashtag_spans = [s for s in spans if s.hashtag is not None]
        assert len(hashtag_spans) == 0
        assert "#todo" in "".join(s.text for s in spans)

    def test_incidental_hashtags_are_literal(self) -> None:
        """Social/prose hashtags (#1, #3, #WWDC) do not overfire as tags."""
        spans = parse_inline_formatting("Top #1, ranked #3, #WWDC recap")

        hashtag_spans = [s for s in spans if s.hashtag is not None]
        assert len(hashtag_spans) == 0

    def test_hash_inside_word_not_matched(self) -> None:
        """Hash inside a word (e.g., C#sharp) is NOT matched as a tag."""
        spans = parse_inline_formatting("Use C#sharp for coding")

        hashtag_spans = [s for s in spans if s.hashtag is not None]
        assert len(hashtag_spans) == 0

    def test_mixed_plain_and_explicit(self) -> None:
        """A line with both a plain #word and an explicit {#tag}."""
        spans = parse_inline_formatting("Plain #1 and a {#real} tag")

        hashtag_spans = [s for s in spans if s.hashtag is not None]
        assert len(hashtag_spans) == 1
        assert hashtag_spans[0].hashtag == "real"
        assert "#1" in "".join(s.text for s in spans)

    def test_plain_text_hashtag_is_none(self) -> None:
        """Plain text spans have hashtag=None."""
        spans = parse_inline_formatting("no tags here")

        assert all(s.hashtag is None for s in spans)


class TestBuildRichtextWithHashtags:
    """Tests for building NotesRichtext with {#tag} placeholders."""

    def test_explicit_tag_produces_placeholder(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """{#tag} becomes a placeholder in text."""
        md = "Check {#todo} item"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        expected_placeholder = _make_placeholder("TAG", 0)
        assert expected_placeholder in richtext.text
        assert "{#todo}" not in richtext.text
        assert "\ufffc" not in richtext.text

    def test_explicit_tag_creates_pending_action(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """{#tag} creates a pending hashtag action (no protobuf attachment)."""
        md = "{#important}"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        # No protobuf attachments — hashtag will be typed via keyboard
        assert len(richtext.attachments) == 0
        assert len(pending) == 1
        assert pending[0].action_type == "hashtag"
        assert pending[0].text_to_type == "important"
        assert pending[0].original_markdown == "{#important}"

    def test_hyphenated_tag_pending_action(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """{#daily-note} types the full hyphenated tag name."""
        md = "{#daily-note}"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        assert len(pending) == 1
        assert pending[0].text_to_type == "daily-note"

    def test_plain_hashtag_no_pending_action(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Plain #word produces no tag action and stays literal."""
        md = "Check #todo item"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        assert len(pending) == 0
        assert "#todo" in richtext.text
        assert _make_placeholder("TAG", 0) not in richtext.text

    def test_tag_run_length_matches_placeholder(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """The placeholder run has length matching the placeholder text."""
        md = "{#tag}"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        expected_placeholder = _make_placeholder("TAG", 0)
        assert richtext.text == expected_placeholder
        total_length = sum(r.length for r in richtext.runs)
        assert total_length == len(expected_placeholder)

    def test_run_lengths_valid_with_tags(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Total run lengths equal text length with tags."""
        md = "Start {#tag1} middle {#tag2} end"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        total_length = sum(r.length for r in richtext.runs)
        assert total_length == len(richtext.text)
        assert len(pending) == 2

    def test_mixed_plain_and_explicit_tags(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Mixed content: only the {#tag} fires; plain #word stays literal."""
        md = "Plain #1 and {#real} here"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, pending = build_richtext(doc, template)

        assert len(pending) == 1
        assert pending[0].text_to_type == "real"
        assert "#1" in richtext.text


class TestMarkdownToRichtextWithResolver:
    """Tests for top-level conversion functions with link_resolver."""

    def test_markdown_to_richtext_new_with_resolver(self) -> None:
        """markdown_to_richtext_new returns RichtextConversionResult with pending actions."""
        resolver = lambda name: "uuid-123" if name == "Target" else None
        result = markdown_to_richtext_new("See [[Target]]", link_resolver=resolver)

        assert isinstance(result, RichtextConversionResult)
        assert len(result.pending_actions) == 1
        assert result.pending_actions[0].action_type == "wiki_link"
        assert result.pending_actions[0].text_to_type == "Target"

        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        placeholder = _make_placeholder("LINK", 0)
        assert placeholder in parsed.text
        assert "[[Target]]" not in parsed.text

    def test_markdown_to_richtext_with_resolver(self, template_data: bytes) -> None:
        """markdown_to_richtext returns RichtextConversionResult with pending actions."""
        resolver = lambda name: "uuid-123" if name == "Target" else None
        result = markdown_to_richtext(
            "See [[Target]]", template_data, link_resolver=resolver
        )

        assert isinstance(result, RichtextConversionResult)
        assert len(result.pending_actions) == 1

        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        placeholder = _make_placeholder("LINK", 0)
        assert placeholder in parsed.text


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_wiki_links_preserved_as_text(self) -> None:
        """Wiki-style links are preserved as plain text (no resolver)."""
        line = parse_markdown_line("See [[My Note]] for details")

        # Without resolver, should be preserved as plain text
        assert line.paragraph_type == ParagraphType.DEFAULT
        assert "My Note" in line.text

    def test_heading_level_1_treated_as_default(self) -> None:
        """Level 1 heading is treated as DEFAULT (title)."""
        # In Notes, the first line is auto-titled, so # doesn't create special type
        line = parse_markdown_line("# Title")

        # Using DEFAULT since Notes handles title differently
        assert line.paragraph_type == ParagraphType.DEFAULT
        assert line.text == "Title"

    def test_dashed_list_vs_checklist(self) -> None:
        """Distinguish between dashed list and checklist."""
        dashed = parse_markdown_line("- Plain item")
        checklist = parse_markdown_line("- [ ] Task item")

        assert dashed.paragraph_type == ParagraphType.DASHED_LIST
        assert checklist.paragraph_type == ParagraphType.CHECKLIST

    def test_empty_checklist_item(self) -> None:
        """Empty checklist item is still recognized."""
        line = parse_markdown_line("- [ ] ")

        assert line.paragraph_type == ParagraphType.CHECKLIST
        assert line.text == ""


class TestMarkdownToRichtextNew:
    """Tests for markdown_to_richtext_new (bundled template)."""

    def test_converts_without_explicit_template(self) -> None:
        """Converts markdown using the bundled default template."""
        md = """## Heading
- [ ] Task
- [x] Done"""

        result = markdown_to_richtext_new(md)

        assert isinstance(result, RichtextConversionResult)
        assert isinstance(result.data, bytes)
        assert len(result.data) > 0
        assert result.pending_actions == []

    def test_roundtrip_text_preserved(self) -> None:
        """Text content is preserved through conversion with bundled template."""
        md = "Simple test text"

        result = markdown_to_richtext_new(md)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "Simple test text"


class TestParseInlineStrikethrough:
    """Tests for ~~strikethrough~~ parsing."""

    def test_basic_strikethrough(self) -> None:
        """~~text~~ produces TextSpan with strikethrough=True."""
        spans = parse_inline_formatting("Hello ~~world~~")

        assert len(spans) == 2
        assert spans[0].text == "Hello "
        assert spans[0].strikethrough is False
        assert spans[1].text == "world"
        assert spans[1].strikethrough is True
        assert spans[1].font_style == FontStyle.NORMAL

    def test_strikethrough_only(self) -> None:
        """Line with just strikethrough."""
        spans = parse_inline_formatting("~~deleted~~")

        assert len(spans) == 1
        assert spans[0].text == "deleted"
        assert spans[0].strikethrough is True

    def test_empty_strikethrough_is_plain_text(self) -> None:
        """~~~~ (empty markers) is treated as plain text."""
        spans = parse_inline_formatting("~~~~")

        assert len(spans) == 1
        assert spans[0].strikethrough is False


class TestParseInlineUnderline:
    """Tests for <u>underline</u> parsing."""

    def test_basic_underline(self) -> None:
        """<u>text</u> produces TextSpan with underline=True."""
        spans = parse_inline_formatting("Hello <u>world</u>")

        assert len(spans) == 2
        assert spans[0].text == "Hello "
        assert spans[0].underline is False
        assert spans[1].text == "world"
        assert spans[1].underline is True
        assert spans[1].font_style == FontStyle.NORMAL

    def test_underline_only(self) -> None:
        """Line with just underline."""
        spans = parse_inline_formatting("<u>underlined</u>")

        assert len(spans) == 1
        assert spans[0].text == "underlined"
        assert spans[0].underline is True

    def test_empty_underline_is_plain_text(self) -> None:
        """<u></u> (empty markers) is treated as plain text."""
        spans = parse_inline_formatting("<u></u>")

        assert len(spans) == 1
        assert spans[0].underline is False


class TestNestedFormatting:
    """Tests for nested/overlapping formatting combinations."""

    def test_bold_strikethrough(self) -> None:
        """**~~text~~** → bold + strikethrough."""
        spans = parse_inline_formatting("**~~text~~**")

        assert len(spans) == 1
        assert spans[0].text == "text"
        assert spans[0].font_style == FontStyle.BOLD
        assert spans[0].strikethrough is True

    def test_strikethrough_bold(self) -> None:
        """~~**text**~~ → same result (bold + strikethrough)."""
        spans = parse_inline_formatting("~~**text**~~")

        assert len(spans) == 1
        assert spans[0].text == "text"
        assert spans[0].font_style == FontStyle.BOLD
        assert spans[0].strikethrough is True

    def test_italic_underline(self) -> None:
        """*<u>text</u>* → italic + underline."""
        spans = parse_inline_formatting("*<u>text</u>*")

        assert len(spans) == 1
        assert spans[0].text == "text"
        assert spans[0].font_style == FontStyle.ITALIC
        assert spans[0].underline is True

    def test_bold_italic_underline_strikethrough(self) -> None:
        """***<u>~~text~~</u>*** → bold_italic + underline + strikethrough."""
        spans = parse_inline_formatting("***<u>~~text~~</u>***")

        assert len(spans) == 1
        assert spans[0].text == "text"
        assert spans[0].font_style == FontStyle.BOLD_ITALIC
        assert spans[0].underline is True
        assert spans[0].strikethrough is True

    def test_link_inside_bold_strikethrough(self) -> None:
        """**~~[link](url)~~** → bold + strikethrough + link."""
        spans = parse_inline_formatting("**~~[link](https://example.com)~~**")

        assert len(spans) == 1
        assert spans[0].text == "link"
        assert spans[0].font_style == FontStyle.BOLD
        assert spans[0].strikethrough is True
        assert spans[0].link_url == "https://example.com"

    def test_mixed_line_with_strikethrough(self) -> None:
        """Mixed line: hello **~~world~~** bye → 3 spans."""
        spans = parse_inline_formatting("hello **~~world~~** bye")

        assert len(spans) == 3
        assert spans[0].text == "hello "
        assert spans[0].font_style == FontStyle.NORMAL
        assert spans[0].strikethrough is False
        assert spans[1].text == "world"
        assert spans[1].font_style == FontStyle.BOLD
        assert spans[1].strikethrough is True
        assert spans[2].text == " bye"
        assert spans[2].font_style == FontStyle.NORMAL
        assert spans[2].strikethrough is False

    def test_underline_with_link(self) -> None:
        """<u>[link](url)</u> → underline + link."""
        spans = parse_inline_formatting("<u>[link](https://example.com)</u>")

        assert len(spans) == 1
        assert spans[0].text == "link"
        assert spans[0].underline is True
        assert spans[0].link_url == "https://example.com"


class TestBuildRichtextWithStrikethroughUnderline:
    """Tests for building NotesRichtext with strikethrough/underline."""

    def test_strikethrough_run_has_field_7(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Strikethrough run's raw_data has field 7 set."""
        md = "~~deleted~~"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        strike_runs = [r for r in richtext.runs if r.strikethrough]
        assert len(strike_runs) == 1
        assert "7" in strike_runs[0].raw_data
        assert strike_runs[0].raw_data["7"] == 1

    def test_underline_run_has_field_6(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Underline run's raw_data has field 6 set."""
        md = "<u>underlined</u>"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        underline_runs = [r for r in richtext.runs if r.underline]
        assert len(underline_runs) == 1
        assert "6" in underline_runs[0].raw_data
        assert underline_runs[0].raw_data["6"] == 1

    def test_bold_strikethrough_run(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Bold + strikethrough run has both font_style and field 7."""
        md = "**~~text~~**"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        combined_runs = [
            r
            for r in richtext.runs
            if r.font_style == FontStyle.BOLD and r.strikethrough
        ]
        assert len(combined_runs) == 1
        assert "5" in combined_runs[0].raw_data  # font_style field
        assert "7" in combined_runs[0].raw_data  # strikethrough field

    def test_run_lengths_valid_with_strikethrough(
        self, parser: NotesRichtextParser, template_data: bytes
    ) -> None:
        """Total run lengths equal text length with strikethrough."""
        md = "Start ~~middle~~ end"
        doc = parse_markdown(md)

        template = parser.parse(template_data)
        richtext, _ = build_richtext(doc, template)

        total_length = sum(r.length for r in richtext.runs)
        assert total_length == len(richtext.text)


class TestStrikethroughUnderlineRoundtrip:
    """Tests for strikethrough/underline roundtrip (markdown → binary → parse)."""

    def test_strikethrough_survives_roundtrip(self, template_data: bytes) -> None:
        """Strikethrough is preserved through markdown → binary → parse."""
        md = "Hello ~~deleted~~ world"

        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "Hello deleted world"

        strike_runs = [r for r in parsed.runs if r.strikethrough]
        assert len(strike_runs) == 1

    def test_underline_survives_roundtrip(self, template_data: bytes) -> None:
        """Underline is preserved through markdown → binary → parse."""
        md = "Hello <u>underlined</u> world"

        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "Hello underlined world"

        underline_runs = [r for r in parsed.runs if r.underline]
        assert len(underline_runs) == 1

    def test_combined_formatting_roundtrip(self, template_data: bytes) -> None:
        """Bold + strikethrough survives roundtrip."""
        md = "**~~bold strike~~**"

        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "bold strike"

        combined_runs = [
            r for r in parsed.runs if r.font_style == FontStyle.BOLD and r.strikethrough
        ]
        assert len(combined_runs) == 1


class TestColorParsing:
    """Tests for {color:name}...{/color} syntax parsing."""

    def test_simple_color(self) -> None:
        """Parse a single color tag."""
        spans = parse_inline_formatting("{color:red}hello{/color}")
        assert len(spans) == 1
        assert spans[0].text == "hello"
        assert spans[0].color == "red"

    def test_color_preserves_surrounding_text(self) -> None:
        """Color tag with surrounding plain text."""
        spans = parse_inline_formatting("before {color:blue}colored{/color} after")
        assert len(spans) == 3
        assert spans[0].text == "before "
        assert spans[0].color is None
        assert spans[1].text == "colored"
        assert spans[1].color == "blue"
        assert spans[2].text == " after"
        assert spans[2].color is None

    def test_color_with_bold(self) -> None:
        """Bold inside color tag."""
        spans = parse_inline_formatting("{color:red}**bold red**{/color}")
        assert len(spans) == 1
        assert spans[0].text == "bold red"
        assert spans[0].color == "red"
        assert spans[0].font_style == FontStyle.BOLD

    def test_bold_with_color(self) -> None:
        """Color inside bold tag."""
        spans = parse_inline_formatting("**{color:red}bold red{/color}**")
        assert len(spans) == 1
        assert spans[0].text == "bold red"
        assert spans[0].color == "red"
        assert spans[0].font_style == FontStyle.BOLD

    def test_color_no_formatting(self) -> None:
        """Plain text span has no color."""
        spans = parse_inline_formatting("plain text")
        assert len(spans) == 1
        assert spans[0].color is None

    def test_consolidation_same_color(self) -> None:
        """Adjacent spans with same color are consolidated."""
        spans = parse_inline_formatting("{color:red}hello{/color}")
        assert len(spans) == 1
        assert spans[0].color == "red"

    def test_nested_color_tags_recurse(self) -> None:
        """Nested {color:} spans color the right ranges (NTG-018).

        The outer tag must match its balanced closer, not the first {/color},
        so no tag text leaks into the note as literal characters.
        """
        spans = parse_inline_formatting(
            "{color:purple}outer {color:blue}inner{/color} tail{/color}"
        )

        assert "".join(s.text for s in spans) == "outer inner tail"
        assert [(s.text, s.color) for s in spans] == [
            ("outer ", "purple"),
            ("inner", "blue"),
            (" tail", "purple"),
        ]

    def test_sibling_color_tags_stay_separate(self) -> None:
        """Two sequential color spans don't merge into one balanced match."""
        spans = parse_inline_formatting(
            "{color:purple}a{/color} x {color:blue}b{/color}"
        )

        assert [(s.text, s.color) for s in spans] == [
            ("a", "purple"),
            (" x ", None),
            ("b", "blue"),
        ]

    def test_unclosed_inner_color_degrades_to_literal(self) -> None:
        """An unbalanced inner opener stays literal inside the outer color."""
        spans = parse_inline_formatting("{color:purple}a {color:blue}b{/color}")

        assert "".join(s.text for s in spans) == "a {color:blue}b"
        assert all(s.color == "purple" for s in spans)

    def test_hex_color(self) -> None:
        """Parse a hex color tag."""
        spans = parse_inline_formatting("{color:#FF0000}hello{/color}")
        assert len(spans) == 1
        assert spans[0].text == "hello"
        assert spans[0].color == "#FF0000"

    def test_hex_color_lowercase(self) -> None:
        """Parse a lowercase hex color tag."""
        spans = parse_inline_formatting("{color:#ff8000}orange{/color}")
        assert len(spans) == 1
        assert spans[0].text == "orange"
        assert spans[0].color == "#ff8000"


class TestColorWritePath:
    """Tests for color in the write path (markdown → richtext)."""

    def test_named_color_produces_field_14(self, template_data: bytes) -> None:
        """Named color tag produces field 14 (highlight palette index)."""
        md = "{color:blue}hello{/color}"
        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "hello"
        color_runs = [r for r in parsed.runs if r.color is not None]
        assert len(color_runs) == 1
        # Check raw_data has field 14, not field 10
        assert "14" in color_runs[0].raw_data
        assert "10" not in color_runs[0].raw_data
        assert color_runs[0].raw_data["14"] == 5  # blue = index 5

    def test_color_roundtrip(self, template_data: bytes) -> None:
        """Color survives markdown → binary → parse."""
        md = "{color:blue}blue text{/color}"
        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "blue text"
        color_runs = [r for r in parsed.runs if r.color is not None]
        assert len(color_runs) == 1

    def test_color_with_bold_roundtrip(self, template_data: bytes) -> None:
        """Color + bold survives roundtrip."""
        md = "**{color:orange}bold orange{/color}**"
        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "bold orange"
        bold_color_runs = [
            r
            for r in parsed.runs
            if r.font_style == FontStyle.BOLD and r.color is not None
        ]
        assert len(bold_color_runs) == 1

    def test_unsupported_color_raises_error(self, template_data: bytes) -> None:
        """Unsupported color name raises MarkdownConversionError."""
        import pytest

        md = "{color:red}hello{/color}"
        with pytest.raises(
            MarkdownConversionError, match="Unsupported highlight color 'red'"
        ):
            markdown_to_richtext(md, template_data)

    def test_all_valid_colors(self, template_data: bytes) -> None:
        """All 5 valid highlight colors produce field 14."""
        for color, index in [
            ("purple", 1),
            ("pink", 2),
            ("orange", 3),
            ("green", 4),
            ("blue", 5),
        ]:
            md = f"{{color:{color}}}text{{/color}}"
            result = markdown_to_richtext(md, template_data)
            parser = NotesRichtextParser()
            parsed = parser.parse(result.data)
            color_runs = [r for r in parsed.runs if r.color is not None]
            assert len(color_runs) == 1, f"{color} should produce a color run"
            assert color_runs[0].raw_data["14"] == index, (
                f"{color} should map to index {index}"
            )

    def test_hex_color_near_named_uses_field_14(self, template_data: bytes) -> None:
        """Hex color close to a named highlight color resolves to field 14."""
        # #FF9500 is close to orange (1.0, 0.5843, 0.0)
        md = "{color:#FF9500}hello{/color}"
        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "hello"
        color_runs = [r for r in parsed.runs if r.color is not None]
        assert len(color_runs) == 1
        assert "14" in color_runs[0].raw_data

    def test_hex_color_no_highlight_match_writes_field_10(
        self, template_data: bytes
    ) -> None:
        """Hex with no highlight match writes a field 10 foreground color
        instead of being silently dropped (NTG-019)."""
        from apple_notes.colors import rgb_floats_to_ints

        md = "{color:#FFFFFF}hello{/color}"
        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert parsed.text == "hello"
        color_runs = [r for r in parsed.runs if r.color is not None]
        assert len(color_runs) == 1
        raw = color_runs[0].raw_data
        assert "14" not in raw
        expected_r, expected_g, expected_b = rgb_floats_to_ints(1.0, 1.0, 1.0)
        assert raw["10"]["1"] == expected_r
        assert raw["10"]["2"] == expected_g
        assert raw["10"]["3"] == expected_b

    def test_hex_color_roundtrips_through_read_path(self, template_data: bytes) -> None:
        """A read→edit→write round-trip preserves a non-highlight hex color.

        The read path renders field 10 foreground colors as {color:#RRGGBB},
        so writing that hex back must land in field 10 with the same value
        (the error message's own example #FF3B30 used to be silently dropped).
        """
        from apple_notes.protobuf_parser import NoteProtobufParser

        md = "{color:#FF3B30}hello{/color}"
        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        color_runs = [r for r in parsed.runs if r.color is not None]
        assert len(color_runs) == 1

        # Feed the written run through the note read path
        read_parser = NoteProtobufParser()
        formats = read_parser.extract_text_formatting(
            [{"1": 5, "10": color_runs[0].raw_data["10"]}]
        )
        assert len(formats) == 1
        assert formats[0].color == "#FF3B30"

    def test_invalid_hex_color_raises(self, template_data: bytes) -> None:
        """An unparseable hex color raises instead of silently dropping."""
        from apple_notes.richtext.markdown_converter import _build_run_raw_data

        with pytest.raises(MarkdownConversionError, match="Invalid hex color"):
            _build_run_raw_data(5, color="#12345Z")


class TestTableParsing:
    """Tests for markdown table parsing."""

    def test_simple_table(self) -> None:
        """Parse a simple 2-column table."""
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        doc = parse_markdown(md)

        # Should have one line with a table
        table_lines = [line for line in doc.lines if line.table is not None]
        assert len(table_lines) == 1
        table = table_lines[0].table
        assert table.col_count == 2
        assert table.cells == [["A", "B"], ["1", "2"]]

    def test_table_with_header_and_data(self) -> None:
        """Parse a table with header and multiple data rows."""
        md = "| Name | Age |\n| --- | --- |\n| Alice | 30 |\n| Bob | 25 |"
        doc = parse_markdown(md)

        table_lines = [line for line in doc.lines if line.table is not None]
        assert len(table_lines) == 1
        table = table_lines[0].table
        assert table.col_count == 2
        assert len(table.cells) == 3  # header + 2 data rows

    def test_table_uses_fffc(self) -> None:
        """Table lines use U+FFFC character."""
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        doc = parse_markdown(md)

        table_lines = [line for line in doc.lines if line.table is not None]
        assert len(table_lines) == 1
        assert table_lines[0].spans[0].text == "\ufffc"

    def test_table_surrounded_by_text(self) -> None:
        """Table with text before and after."""
        md = "Before\n| A | B |\n| --- | --- |\n| 1 | 2 |\nAfter"
        doc = parse_markdown(md)

        assert len(doc.lines) == 3
        assert doc.lines[0].text == "Before"
        assert doc.lines[1].table is not None
        assert doc.lines[2].text == "After"

    def test_pipe_without_separator_not_table(self) -> None:
        """Lines with pipes but no separator are not tables."""
        md = "| not | a | table |"
        doc = parse_markdown(md)

        table_lines = [line for line in doc.lines if line.table is not None]
        assert len(table_lines) == 0

    def test_three_column_table(self) -> None:
        """Parse a 3-column table."""
        md = "| A | B | C |\n| --- | --- | --- |\n| 1 | 2 | 3 |"
        doc = parse_markdown(md)

        table_lines = [line for line in doc.lines if line.table is not None]
        assert len(table_lines) == 1
        table = table_lines[0].table
        assert table.col_count == 3


class TestTableWritePath:
    """Tests for table in the write path (markdown → richtext).

    Tables should produce PendingKeyboardAction (for keyboard automation)
    instead of Attachment objects (which render as empty boxes in Notes.app).
    """

    def test_table_produces_pending_action(self, template_data: bytes) -> None:
        """Markdown table creates a PendingKeyboardAction, not an Attachment."""
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"

        result = markdown_to_richtext(md, template_data)

        # Should have a pending action for the table
        table_actions = [a for a in result.pending_actions if a.action_type == "table"]
        assert len(table_actions) == 1
        assert table_actions[0].table_cells == [["A", "B"], ["1", "2"]]
        assert table_actions[0].table_col_count == 2

    def test_table_placeholder_in_text(self, template_data: bytes) -> None:
        """Table should produce a placeholder in the richtext text."""
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"

        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert "{__NG_TABLE_0__}" in parsed.text

    def test_table_no_attachment(self, template_data: bytes) -> None:
        """Table should NOT create an Attachment object."""
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"

        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        table_attachments = [
            a for a in parsed.attachments if a.type_uti == "com.apple.notes.table"
        ]
        assert len(table_attachments) == 0

    def test_multiple_tables_unique_placeholders(self, template_data: bytes) -> None:
        """Multiple tables get unique placeholders."""
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |\nMiddle\n| C | D |\n| --- | --- |\n| 3 | 4 |"

        result = markdown_to_richtext(md, template_data)

        table_actions = [a for a in result.pending_actions if a.action_type == "table"]
        assert len(table_actions) == 2
        assert table_actions[0].placeholder == "{__NG_TABLE_0__}"
        assert table_actions[1].placeholder == "{__NG_TABLE_1__}"

    def test_table_cells_preserved(self, template_data: bytes) -> None:
        """Table cell content is preserved in PendingKeyboardAction."""
        md = "| Name | Age |\n| --- | --- |\n| Alice | 30 |\n| Bob | 25 |"

        result = markdown_to_richtext(md, template_data)

        table_actions = [a for a in result.pending_actions if a.action_type == "table"]
        assert len(table_actions) == 1
        assert table_actions[0].table_cells == [
            ["Name", "Age"],
            ["Alice", "30"],
            ["Bob", "25"],
        ]
        assert table_actions[0].table_col_count == 2

    def test_table_surrounded_by_text(self, template_data: bytes) -> None:
        """Table with text before and after produces correct output."""
        md = "Before text\n| A | B |\n| --- | --- |\n| 1 | 2 |\nAfter text"

        result = markdown_to_richtext(md, template_data)
        parser = NotesRichtextParser()
        parsed = parser.parse(result.data)

        assert "Before text" in parsed.text
        assert "{__NG_TABLE_0__}" in parsed.text
        assert "After text" in parsed.text

        table_actions = [a for a in result.pending_actions if a.action_type == "table"]
        assert len(table_actions) == 1


class TestEnsureCompleteTypedef:
    """Tests for _ensure_complete_typedef field 6 merge bug fix."""

    def test_field_6_missing_entirely_gets_created(self) -> None:
        """When field 6 is entirely missing, it should be created with all subfields."""
        typedef = {"5": {"type": "message", "message_typedef": {}, "name": ""}}
        _ensure_complete_typedef(typedef)
        assert "6" in typedef
        msg_td = typedef["6"]["message_typedef"]
        assert "2" in msg_td
        assert "6" in msg_td
        assert "8" in msg_td
        assert "25" in msg_td

    def test_field_6_exists_but_missing_subfield_25(self) -> None:
        """When field 6 exists but lacks subfield 25, it should be patched in."""
        typedef = {
            "5": {"type": "message", "message_typedef": {}, "name": ""},
            "6": {
                "type": "message",
                "message_typedef": {
                    "2": {"type": "string", "name": ""},
                    "6": {"type": "string", "name": ""},
                    "8": {"type": "string", "name": ""},
                    # Note: subfield 25 is MISSING
                },
                "name": "",
            },
        }
        _ensure_complete_typedef(typedef)
        msg_td = typedef["6"]["message_typedef"]
        assert "25" in msg_td
        assert msg_td["25"]["type"] == "int"

    def test_field_10_exists_but_missing_alpha_subfield(self) -> None:
        """Field 10 present with only RGB subfields gets alpha (4) merged in.

        Templates parsed from notes with a foreground color may carry a
        field 10 typedef with subfields 1-3 only; the hex write path always
        emits subfield 4 (alpha), which must exist in the typedef or
        serialization fails (Codex review finding on NTG-019).
        """
        typedef = {
            "5": {
                "type": "message",
                "message_typedef": {
                    "10": {
                        "type": "message",
                        "message_typedef": {
                            "1": {"type": "fixed32", "name": ""},
                            "2": {"type": "fixed32", "name": ""},
                            "3": {"type": "fixed32", "name": ""},
                            # Note: subfield 4 (alpha) is MISSING
                        },
                        "name": "",
                    },
                },
                "name": "",
            }
        }
        _ensure_complete_typedef(typedef)
        color_td = typedef["5"]["message_typedef"]["10"]["message_typedef"]
        assert color_td["4"]["type"] == "fixed32"

    def test_field_6_exists_with_all_subfields_unchanged(self) -> None:
        """When field 6 already has all subfields, it should remain unchanged."""
        typedef = {
            "5": {"type": "message", "message_typedef": {}, "name": ""},
            "6": {
                "type": "message",
                "message_typedef": {
                    "2": {"type": "string", "name": ""},
                    "6": {"type": "string", "name": ""},
                    "8": {"type": "string", "name": ""},
                    "25": {"type": "int", "name": ""},
                },
                "name": "",
            },
        }
        _ensure_complete_typedef(typedef)
        msg_td = typedef["6"]["message_typedef"]
        assert msg_td["25"]["type"] == "int"
        assert len(msg_td) == 4


class TestMonospaceLineNotGreedy:
    """Whole-line monospace must not swallow separate inline-code spans (NTG-008)."""

    def test_line_with_two_inline_code_spans_not_monospace(self) -> None:
        """A line starting and ending with distinct code spans stays a DEFAULT line."""
        line = parse_markdown_line("`first` and `second`")

        assert line.paragraph_type == ParagraphType.DEFAULT

    def test_single_backtick_wrapped_line_still_monospace(self) -> None:
        line = parse_markdown_line("`code block`")

        assert line.paragraph_type == ParagraphType.MONOSPACE
        assert line.text == "code block"


# --- partial typedefs from the parser's decode hint (issue #184 live run) ----
#
# The pasteboard parser pins 5 -> 2 -> 9 and 5 -> 2 -> 5 -> 1, so a template
# without checklists comes back with a PARTIAL 5 -> 2 -> 5 typedef. The
# typedef completion must merge missing subfields into it, not skip it.

_CONSTRUCTS = [
    "## Heading two",
    "### Heading three",
    "- [ ] open task",
    "- [x] done task",
    "- dashed item",
    "* bullet item",
    "1. numbered item",
    "> quoted line",
    "`mono line`",
    "**bold** *italic* ***both*** ~~strike~~ <u>under</u> `code`",
    "{color:pink}pink text{/color} {color:#12ab34}hex text{/color}",
    "see [a site](https://example.com/x)",
    "link to [[Some Note]]",
    "tag {#todo}",
    "| a | b |\n| - | - |\n| 1 | 2 |",
]


def _plain_template():
    from apple_notes.richtext.markdown_converter import markdown_to_richtext_new

    return markdown_to_richtext_new("plain paragraph only").data


def test_checklist_on_plain_template_regression():
    from apple_notes.richtext.markdown_converter import markdown_to_richtext

    out = markdown_to_richtext("intro\n- [ ] new task", _plain_template())
    rt = NotesRichtextParser().parse(out.data)
    assert rt.text == "intro\nnew task"


@pytest.mark.parametrize("construct", _CONSTRUCTS)
def test_every_construct_converts_against_plain_template(construct):
    from apple_notes.richtext.markdown_converter import markdown_to_richtext

    resolver = lambda name: "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"  # noqa: E731
    out = markdown_to_richtext(
        f"intro\n{construct}\noutro", _plain_template(), link_resolver=resolver
    )
    rt = NotesRichtextParser().parse(out.data)
    assert rt.text.startswith("intro\n") and rt.text.endswith("\noutro")


def test_checklist_on_template_with_paragraph_uuid_but_no_checklist():
    from apple_notes.richtext.markdown_converter import (
        markdown_to_richtext,
        markdown_to_richtext_new,
    )

    template = markdown_to_richtext_new("## A heading\nbody line").data
    style = NotesRichtextParser().parse(template).runs[0].raw_data["2"]
    assert "9" in style and "5" not in style  # uuid present, no checklist
    out = markdown_to_richtext("- [x] done\n- [ ] open", template)
    rt = NotesRichtextParser().parse(out.data)
    assert [r.paragraph.checklist_checked for r in rt.runs if r.paragraph] [:2] == [True, False]
