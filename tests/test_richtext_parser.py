"""Tests for Apple Notes richtext parser.

Following TDD: these tests are written first, before the parser implementation.
Tests use real fixture files from tests/fixtures/.
"""

from pathlib import Path

import pytest

from apple_notes.richtext import (
    FontStyle,
    NotesRichtext,
    NotesRichtextParser,
    ParagraphType,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_data() -> bytes:
    """Load the 'great sentence' sample fixture."""
    return (FIXTURES / "notes_richtext_sample_with_great_sentence.bin").read_bytes()


@pytest.fixture
def sample_data_alt() -> bytes:
    """Load the 'amazing phrase' sample fixture."""
    return (FIXTURES / "notes_richtext_sample_with_amazing_phrase.bin").read_bytes()


@pytest.fixture
def parser() -> NotesRichtextParser:
    """Create a parser instance."""
    return NotesRichtextParser()


class TestParserBasics:
    """Basic parser functionality tests."""

    def test_parse_returns_notes_richtext(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser returns a NotesRichtext object."""
        result = parser.parse(sample_data)
        assert isinstance(result, NotesRichtext)

    def test_parse_extracts_text(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser extracts the plain text content."""
        result = parser.parse(sample_data)
        assert "Test note format FOR UPDATING" in result.text
        assert "great sentence" in result.text

    def test_parse_alt_fixture_has_different_text(
        self, parser: NotesRichtextParser, sample_data_alt: bytes
    ) -> None:
        """Alternative fixture has 'amazing phrase' instead of 'great sentence'."""
        result = parser.parse(sample_data_alt)
        assert "amazing phrase" in result.text
        assert "great sentence" not in result.text


class TestParserRuns:
    """Tests for attribute run extraction."""

    def test_parse_extracts_runs(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser extracts attribute runs."""
        result = parser.parse(sample_data)
        assert len(result.runs) > 0

    def test_parse_extracts_correct_run_count(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser extracts the expected number of runs (46 from analysis)."""
        result = parser.parse(sample_data)
        assert len(result.runs) == 46

    def test_run_lengths_sum_to_text_length(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Critical invariant: run lengths must sum to text length."""
        result = parser.parse(sample_data)
        total_length = sum(run.length for run in result.runs)
        assert total_length == len(result.text)

    def test_first_run_covers_title(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """First run should cover the title 'Test note format FOR UPDATING\\n'."""
        result = parser.parse(sample_data)
        first_run = result.runs[0]
        assert first_run.length == 30
        assert result.text_at_run(0) == "Test note format FOR UPDATING\n"


class TestParserFontStyles:
    """Tests for font style detection."""

    def test_parse_identifies_bold(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies bold text."""
        result = parser.parse(sample_data)

        # First run (title) should be bold
        assert result.runs[0].font_style == FontStyle.BOLD

        # Find 'bolded' text and verify it's bold
        bold_runs = [r for r in result.runs if r.font_style == FontStyle.BOLD]
        assert len(bold_runs) >= 2  # Title + "bolded"

    def test_parse_identifies_italic(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies italic text."""
        result = parser.parse(sample_data)

        italic_runs = [r for r in result.runs if r.font_style == FontStyle.ITALIC]
        assert len(italic_runs) >= 1

    def test_parse_identifies_underline(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies underlined text."""
        result = parser.parse(sample_data)

        underline_runs = [r for r in result.runs if r.underline]
        assert len(underline_runs) >= 1

    def test_parse_identifies_strikethrough(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies strikethrough text."""
        result = parser.parse(sample_data)

        strikethrough_runs = [r for r in result.runs if r.strikethrough]
        assert len(strikethrough_runs) >= 1


class TestParserParagraphStyles:
    """Tests for paragraph/block style detection."""

    def test_parse_identifies_heading(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies heading paragraphs."""
        result = parser.parse(sample_data)

        heading_runs = [
            r
            for r in result.runs
            if r.paragraph and r.paragraph.type == ParagraphType.HEADING
        ]
        assert len(heading_runs) >= 1

    def test_parse_identifies_code_block(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies code block paragraphs."""
        result = parser.parse(sample_data)

        code_runs = [
            r
            for r in result.runs
            if r.paragraph and r.paragraph.type == ParagraphType.MONOSPACE
        ]
        assert len(code_runs) >= 1

    def test_parse_identifies_checklist(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies checklist items."""
        result = parser.parse(sample_data)

        checklist_runs = [
            r
            for r in result.runs
            if r.paragraph and r.paragraph.type == ParagraphType.CHECKLIST
        ]
        assert len(checklist_runs) >= 4  # "to-do list" items (type 103 with field 5)

    def test_parse_identifies_numbered_list(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies numbered list items."""
        result = parser.parse(sample_data)

        numbered_runs = [
            r
            for r in result.runs
            if r.paragraph and r.paragraph.type == ParagraphType.NUMBERED_LIST
        ]
        assert len(numbered_runs) >= 2

    def test_parse_identifies_unchecked_checklists_correctly(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser correctly identifies unchecked checklists (not all marked checked).

        This tests the fix for a bug where unchecked checklists were incorrectly
        marked as checked because field 5 can be a nested dict like
        {'1': uuid, '2': 0} where '2' contains the actual checked state.
        The old code did bool(dict) which is always True for non-empty dicts.
        """
        result = parser.parse(sample_data)

        # Find checklist items
        checklist_runs = [
            r
            for r in result.runs
            if r.paragraph and r.paragraph.type == ParagraphType.CHECKLIST
        ]

        # Fixture has checklist items
        assert len(checklist_runs) >= 2, "Expected at least 2 checklist items"

        # The fixture's checklists are unchecked - verify parser doesn't
        # incorrectly mark them as checked (the original bug)
        unchecked_count = sum(
            1 for r in checklist_runs if not r.paragraph.checklist_checked
        )
        assert unchecked_count >= 2, "Expected unchecked checklists to be identified"


class TestParserAttachments:
    """Tests for attachment extraction (tables, internal links)."""

    def test_parse_extracts_attachments(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser extracts attachment objects."""
        result = parser.parse(sample_data)
        assert len(result.attachments) >= 1

    def test_parse_identifies_table(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies table attachment."""
        result = parser.parse(sample_data)

        tables = [a for a in result.attachments if "table" in a.type_uti]
        assert len(tables) >= 1

    def test_parse_identifies_internal_link(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser identifies internal note link attachment."""
        result = parser.parse(sample_data)

        links = [a for a in result.attachments if "link" in a.type_uti]
        assert len(links) >= 1


class TestParserPositionMapping:
    """Tests for mapping text positions to formatting."""

    def test_text_at_run_returns_correct_slice(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """text_at_run returns the correct text slice."""
        result = parser.parse(sample_data)

        # First run is title
        assert result.text_at_run(0).startswith("Test note format")

    def test_run_at_position_finds_correct_run(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """run_at_position finds the run containing a position."""
        result = parser.parse(sample_data)

        # Position 0 should be in run 0
        idx, run = result.run_at_position(0)
        assert idx == 0

        # Position 35 should be in a later run (after title + newlines)
        idx2, run2 = result.run_at_position(35)
        assert idx2 > 0


class TestParserArchivePreservation:
    """Tests for preserving original archive structure for serialization."""

    def test_parse_preserves_archive_template(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser preserves original archive structure for reconstruction."""
        result = parser.parse(sample_data)
        assert result._archive_template is not None

    def test_parse_preserves_raw_run_data(
        self, parser: NotesRichtextParser, sample_data: bytes
    ) -> None:
        """Parser preserves raw protobuf data in runs for reconstruction."""
        result = parser.parse(sample_data)

        # At least some runs should have raw data preserved
        runs_with_raw = [r for r in result.runs if r.raw_data]
        assert len(runs_with_raw) > 0


class TestParagraphUuidGuessedAsMessage:
    """A checklist paragraph's uuid (field 9) is 16 random bytes. Without a
    typedef, bbpb sometimes guesses them as a nested message and returns a
    dict (~1% per paragraph): parse must not fail on it (issue #200 — a
    failed copy-back parse would report a landed write as unconfirmed)."""

    def test_dict_uuid_does_not_fail_the_parse(self):
        style = NotesRichtextParser()._parse_paragraph_style(
            {"1": 103, "9": {"1": "x"}}
        )
        assert style.uuid is None

    def test_many_generated_checklists_all_parse(self):
        from apple_notes.richtext import markdown_to_richtext_new

        for _ in range(400):
            NotesRichtextParser().parse(markdown_to_richtext_new("T\n- [ ] a").data)

    def test_checklist_template_converts_reliably(self):
        """With the uuid guessed as a message, the archive typedef recorded
        field 9 (and 5.1) as a message and every new checklist paragraph then
        failed to serialize — a replace on a checklist note failed ~1-2% of
        the time. The parser pins those uuid fields as bytes."""
        from apple_notes.richtext import markdown_to_richtext, markdown_to_richtext_new

        for _ in range(400):
            template = markdown_to_richtext_new("T\n- [ ] a\n- [ ] b").data
            markdown_to_richtext("- [x] a\n- [ ] b\n- [ ] c", template)

    def test_uuid_fields_decode_as_bytes(self):
        from apple_notes.richtext import markdown_to_richtext_new

        for _ in range(200):
            rt = NotesRichtextParser().parse(markdown_to_richtext_new("T\n- [ ] a").data)
            para = rt.runs[1].raw_data["2"]
            assert isinstance(para["9"], bytes)
            assert isinstance(para["5"]["1"], bytes)


# Short texts that blackboxprotobuf would otherwise guess are nested messages
# ("Empty" = 0x45 -> field 8 fixed32 + 4 bytes; issue #184, R12).
SHORT_TEXTS = ["Empty", "A heading", "Head", "Beta", "x", "Empty\n", "Alpha one"]


@pytest.mark.parametrize("text", SHORT_TEXTS)
def test_short_text_round_trips(sample_data, text):
    from apple_notes.richtext import NotesRichtextSerializer
    from apple_notes.richtext.operations import delete_text, insert_text

    parser = NotesRichtextParser()
    base = parser.parse(sample_data)
    emptied = delete_text(base, 0, len(base.text))
    emptied.runs = [r for r in emptied.runs[:1]]
    rt = insert_text(emptied, 0, text)
    data = NotesRichtextSerializer().serialize(rt)
    again = parser.parse(data)
    assert again.text == text
    assert sum(r.length for r in again.runs) == len(text)
    assert parser.parse(NotesRichtextSerializer().serialize(again)).text == text


def test_text_field_typedef_stays_string(sample_data):
    rt = NotesRichtextParser().parse(sample_data)
    assert rt._protobuf_typedef["2"]["type"] == "string"


# 16 bytes that are themselves valid protobuf (field 1 = 1, eight times): an
# unpinned decode guesses a nested message and _parse_paragraph_style crashed.
MESSAGE_LIKE_UUID = b"\x08\x01" * 8


@pytest.mark.parametrize("field", ["9", "5"])
def test_message_like_paragraph_uuids_parse(field):
    from apple_notes.richtext import NotesRichtextSerializer, markdown_to_richtext

    template = (FIXTURES / "notes_richtext_sample_with_great_sentence.bin").read_bytes()
    parser = NotesRichtextParser()
    rt = parser.parse(markdown_to_richtext("- [x] done item", template).data)
    style = rt.runs[0].raw_data["2"]
    if field == "9":
        style["9"] = MESSAGE_LIKE_UUID
    else:
        style["5"]["1"] = MESSAGE_LIKE_UUID
    again = parser.parse(NotesRichtextSerializer().serialize(rt))
    style = again.runs[0].raw_data["2"]
    assert (style["9"] if field == "9" else style["5"]["1"]) == MESSAGE_LIKE_UUID
    assert again.runs[0].paragraph.checklist_checked is True


def test_invalid_utf8_text_parses_with_replacement(sample_data):
    """Invalid UTF-8 in the text field falls back to a bytes decode with
    errors="replace" instead of failing the whole parse (#184 final review)."""
    from apple_notes.richtext import NotesRichtextSerializer
    from apple_notes.richtext.operations import delete_text, insert_text

    parser = NotesRichtextParser()
    base = parser.parse(sample_data)
    emptied = delete_text(base, 0, len(base.text))
    emptied.runs = emptied.runs[:1]
    data = NotesRichtextSerializer().serialize(insert_text(emptied, 0, "zzQQQQzz"))
    assert data.count(b"QQQQ") == 1
    broken = data.replace(b"QQQQ", b"Q\xff\xfeQ")  # same length: plist offsets hold
    rt = parser.parse(broken)
    assert rt.text == "zzQ\ufffd\ufffdQzz"
    assert sum(r.length for r in rt.runs) == len(rt.text)
