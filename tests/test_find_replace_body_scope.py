"""find_replace must never rewrite a note's title line (issue #120).

Deliberately NOT marked macos_gui: everything here is byte/string manipulation
with a mocked pasteboard, so CI runs it. A revert of the title guard has to fail
a check that CI actually executes.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apple_notes.richtext import (
    NotesRichtextParser,
    NotesRichtextSerializer,
    find_replace,
)
from apple_notes.title_scope import (
    body_start_offset,
    body_text,
    rtf_body_start_offset,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PATH = FIXTURE_DIR / "notes_richtext_sample_with_great_sentence.bin"
TITLE = "Test note format FOR UPDATING"

# A Cocoa/Apple Notes RTF prologue: \pard, \paperw and friends all start with
# the letters "par"/"pa", so a naive substring search for "\par" matches them.
COCOA_RTF = (
    b"{\\rtf1\\ansi\\ansicpg1252\\cocoartf2761\\paperw11900\\margl1440"
    b"\\pard\\tx560\\pardirnatural\\partightenfactor0 \\f0\\fs36 \\cf0 "
    b"Status\\par Status: pending\\par}"
)


class TestTitleScopeHelpers:
    def test_body_start_offset_after_title_line(self):
        assert body_start_offset("Title\nbody\n") == 6

    def test_body_start_offset_title_only_note(self):
        text = "Title only"
        assert body_start_offset(text) == len(text)

    def test_body_text_strips_title(self):
        assert body_text("Title\nbody\nmore\n") == "body\nmore\n"

    def test_rtf_body_start_offset_ignores_pard_prologue(self):
        rtf = COCOA_RTF.decode("utf-8")
        offset = rtf_body_start_offset(rtf)
        # Everything before the offset must contain the title word exactly once
        assert rtf[:offset].count("Status") == 1
        assert rtf[offset:].count("Status") == 1

    def test_rtf_body_start_offset_without_paragraph_break(self):
        rtf = "{\\rtf1\\ansi\\pard Status here}"
        assert rtf_body_start_offset(rtf) == len(rtf)


@pytest.fixture
def manager():
    with patch("apple_notes.clipboard.NSPasteboard") as mock_pb_class:
        mock_pb_class.generalPasteboard.return_value = MagicMock()
        from apple_notes.clipboard import ClipboardManager

        yield ClipboardManager()


@pytest.fixture
def sample_richtext() -> bytes:
    return SAMPLE_PATH.read_bytes()


def _seed(data: bytes, search: str, replace: str) -> bytes:
    """Rewrite body text in real richtext bytes to set up a test scenario."""
    parser, serializer = NotesRichtextParser(), NotesRichtextSerializer()
    richtext = parser.parse(data)
    seeded = find_replace(
        richtext,
        search,
        replace,
        replace_all=True,
        search_start=body_start_offset(richtext.text),
    )
    return serializer.serialize(seeded)


class TestNotesRichtextVariableBodyOnly:
    """NTG-003: the richtext engine must skip the title line."""

    def test_title_only_match_makes_no_replacement(self, manager, sample_richtext):
        result, count, pending = manager.find_replace_in_notes_richtext_variable(
            sample_richtext, "UPDATING", "REVISED"
        )

        assert count == 0
        assert result == sample_richtext
        assert pending == []

    def test_body_match_still_replaced_and_title_preserved(
        self, manager, sample_richtext
    ):
        result, count, _ = manager.find_replace_in_notes_richtext_variable(
            sample_richtext, "great sentence", "magnificent"
        )

        assert count == 1
        text = NotesRichtextParser().parse(result).text
        assert text.split("\n")[0] == TITLE
        assert "magnificent" in text

    def test_body_only_can_be_disabled(self, manager, sample_richtext):
        result, count, _ = manager.find_replace_in_notes_richtext_variable(
            sample_richtext, "UPDATING", "REVISED", body_only=False
        )

        assert count == 1
        assert result != sample_richtext

    def test_title_term_repeated_in_body_replaces_body_only(
        self, manager, sample_richtext
    ):
        """The title word also appears in the body — only the body changes."""
        data = _seed(sample_richtext, "great sentence", "UPDATING")

        result, count, _ = manager.find_replace_in_notes_richtext_variable(
            data, "UPDATING", "DONE"
        )

        assert count == 1
        text = NotesRichtextParser().parse(result).text
        assert text.split("\n")[0] == TITLE
        assert "DONE" in body_text(text)

    def test_formatted_replacement_skips_title(self, manager, sample_richtext):
        """NTG-004: the markdown-formatted path is body-scoped too."""
        data = _seed(sample_richtext, "great sentence", "UPDATING")

        result, count, _ = manager.find_replace_in_notes_richtext_variable(
            data, "UPDATING", "**DONE**"
        )

        assert count == 1
        text = NotesRichtextParser().parse(result).text
        assert text.split("\n")[0] == TITLE
        assert "DONE" in body_text(text)

    def test_pending_action_replacement_stays_single_occurrence(
        self, manager, sample_richtext
    ):
        """A replacement carrying native elements can only be applied once.

        build_richtext emits one PendingKeyboardAction per placeholder, so
        splicing the placeholder into several occurrences would leave literal
        {__NG_…__} text behind in the note.
        """
        data = _seed(sample_richtext, "great sentence", "TWICE and TWICE")

        result, count, pending = manager.find_replace_in_notes_richtext_variable(
            data, "TWICE", "linked {#tagx}"
        )

        assert pending, "expected a pending keyboard action for the tag"
        assert count == 1
        text = NotesRichtextParser().parse(result).text
        assert body_text(text).count("TWICE") == 1


class TestRawRtfBodyOnly:
    """The RTF pasteboard flavor written alongside the richtext, same rules."""

    def test_body_only_skips_title_paragraph_in_cocoa_rtf(self, manager):
        result, count = manager.find_replace_in_raw_rtf(
            COCOA_RTF, "Status", "Done", body_only=True
        )

        assert count == 1
        assert b"\\cf0 Status\\par Done: pending" in result

    def test_body_only_default_is_whole_document(self, manager):
        result, count = manager.find_replace_in_raw_rtf(COCOA_RTF, "Status", "Done")

        assert count == 2
        assert b"\\cf0 Done\\par Done: pending" in result

    def test_body_only_without_paragraph_break_replaces_nothing(self, manager):
        """No paragraph break means the whole document is the title line."""
        rtf = b"{\\rtf1\\ansi\\pard Status here}"

        result, count = manager.find_replace_in_raw_rtf(
            rtf, "Status", "Done", body_only=True
        )

        assert count == 0
        assert result == rtf
