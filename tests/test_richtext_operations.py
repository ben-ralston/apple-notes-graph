"""Tests for Apple Notes richtext edit operations.

Following TDD: these tests define the expected behavior for edit operations
that modify text while preserving formatting.
"""

from pathlib import Path

import pytest

from apple_notes.richtext import (
    AttributeRun,
    FontStyle,
    NotesRichtext,
    NotesRichtextParser,
    ParagraphType,
)
from apple_notes.richtext.operations import (
    adjust_run_lengths_for_replacement,
    checklist_item_checked,
    find_replace,
    toggle_checkbox,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_data() -> bytes:
    """Load the 'great sentence' sample fixture."""
    return (FIXTURES / "notes_richtext_sample_with_great_sentence.bin").read_bytes()


@pytest.fixture
def parser() -> NotesRichtextParser:
    """Create a parser instance."""
    return NotesRichtextParser()


@pytest.fixture
def parsed_richtext(parser: NotesRichtextParser, sample_data: bytes) -> NotesRichtext:
    """Parse the sample fixture."""
    return parser.parse(sample_data)


class TestAdjustRunLengths:
    """Tests for run length adjustment after text changes."""

    def test_same_length_replacement_no_change(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Same-length replacement doesn't change run lengths."""
        original_lengths = [r.length for r in parsed_richtext.runs]

        # Replace "great" with "super" (5 chars each)
        adjust_run_lengths_for_replacement(
            parsed_richtext.runs,
            parsed_richtext.text,
            "great",
            "super",
        )

        new_lengths = [r.length for r in parsed_richtext.runs]
        assert new_lengths == original_lengths

    def test_shorter_replacement_adjusts_length(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Shorter replacement reduces affected run length."""
        original_total = sum(r.length for r in parsed_richtext.runs)

        # "great sentence" (14 chars) -> "good" (4 chars), diff = -10
        adjust_run_lengths_for_replacement(
            parsed_richtext.runs,
            parsed_richtext.text,
            "great sentence",
            "good",
        )

        new_total = sum(r.length for r in parsed_richtext.runs)
        assert new_total == original_total - 10

    def test_longer_replacement_adjusts_length(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Longer replacement increases affected run length."""
        original_total = sum(r.length for r in parsed_richtext.runs)

        # "great" (5 chars) -> "fantastic" (9 chars), diff = +4
        adjust_run_lengths_for_replacement(
            parsed_richtext.runs,
            parsed_richtext.text,
            "great",
            "fantastic",
        )

        new_total = sum(r.length for r in parsed_richtext.runs)
        assert new_total == original_total + 4

    def test_run_lengths_remain_positive(self, parsed_richtext: NotesRichtext) -> None:
        """Run lengths never become negative after adjustment."""
        adjust_run_lengths_for_replacement(
            parsed_richtext.runs,
            parsed_richtext.text,
            "Test note format FOR UPDATING",  # Replace first 29 chars
            "Hi",  # With just 2 chars
        )

        for run in parsed_richtext.runs:
            assert run.length >= 0


class TestFindReplace:
    """Tests for find/replace operation."""

    def test_find_replace_updates_text(self, parsed_richtext: NotesRichtext) -> None:
        """Find/replace updates the text content."""
        result = find_replace(parsed_richtext, "great sentence", "wonderful phrase")

        assert "wonderful phrase" in result.text
        assert "great sentence" not in result.text

    def test_find_replace_updates_run_lengths(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Find/replace updates run lengths to match new text."""
        result = find_replace(parsed_richtext, "great", "magnificent")

        # Run lengths should sum to new text length
        total_run_length = sum(r.length for r in result.runs)
        assert total_run_length == len(result.text)

    def test_find_replace_preserves_formatting(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Find/replace preserves formatting structure."""
        original_run_count = len(parsed_richtext.runs)

        result = find_replace(parsed_richtext, "great", "super")

        # Should have same number of runs (formatting preserved)
        assert len(result.runs) == original_run_count

    def test_find_replace_not_found_returns_unchanged(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Find/replace with non-existent text returns unchanged."""
        original_text = parsed_richtext.text

        result = find_replace(parsed_richtext, "nonexistent text xyz", "replacement")

        assert result.text == original_text

    def test_find_replace_multiple_occurrences(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Find/replace handles multiple occurrences."""
        # "list" appears multiple times in the sample
        result = find_replace(parsed_richtext, "list", "collection", replace_all=True)

        # All occurrences should be replaced
        assert "list" not in result.text.lower() or "checklist" in result.text.lower()

    def test_replace_all_no_infinite_loop_when_replace_contains_search(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """replace_all terminates when replacement contains the search string."""
        result = find_replace(parsed_richtext, "great", "greatest", replace_all=True)

        assert "greatest" in result.text
        assert "great sentence" not in result.text
        result.validate()

    def test_replace_all_no_infinite_loop_noop(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """replace_all terminates when search == replace (no-op)."""
        result = find_replace(parsed_richtext, "great", "great", replace_all=True)

        assert result.text == parsed_richtext.text
        result.validate()

    def test_find_replace_preserves_checklist_checked_state(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Find/replace preserves checked state of checklists."""
        # Count checked items before
        checked_before = sum(
            1
            for r in parsed_richtext.runs
            if r.paragraph
            and r.paragraph.type == ParagraphType.CHECKLIST
            and r.paragraph.checklist_checked
        )
        unchecked_before = sum(
            1
            for r in parsed_richtext.runs
            if r.paragraph
            and r.paragraph.type == ParagraphType.CHECKLIST
            and not r.paragraph.checklist_checked
        )

        # Do a replacement that doesn't touch checklist text
        result = find_replace(parsed_richtext, "great", "super")

        # Count checked items after
        checked_after = sum(
            1
            for r in result.runs
            if r.paragraph
            and r.paragraph.type == ParagraphType.CHECKLIST
            and r.paragraph.checklist_checked
        )
        unchecked_after = sum(
            1
            for r in result.runs
            if r.paragraph
            and r.paragraph.type == ParagraphType.CHECKLIST
            and not r.paragraph.checklist_checked
        )

        assert checked_after == checked_before
        assert unchecked_after == unchecked_before


class TestToggleCheckbox:
    """Tests for checkbox toggling operation."""

    def test_toggle_checkbox_finds_item(self, parsed_richtext: NotesRichtext) -> None:
        """Toggle checkbox finds the checklist item by text."""
        # The fixture has "This needs to happen" as an unchecked CHECKLIST item
        result = toggle_checkbox(parsed_richtext, "This needs to happen")
        assert result is not None

    def test_toggle_checkbox_changes_state(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Toggle checkbox changes the checked state."""
        item_text = "This needs to happen"
        original_checked = None

        for i, run in enumerate(parsed_richtext.runs):
            text_slice = parsed_richtext.text_at_run(i)
            if item_text in text_slice and run.paragraph:
                if run.paragraph.type == ParagraphType.CHECKLIST:
                    original_checked = run.paragraph.checklist_checked
                    break

        # Toggle it
        result = toggle_checkbox(parsed_richtext, item_text)

        # Find new state
        new_checked = None
        for i, run in enumerate(result.runs):
            text_slice = result.text_at_run(i)
            if item_text in text_slice and run.paragraph:
                if run.paragraph.type == ParagraphType.CHECKLIST:
                    new_checked = run.paragraph.checklist_checked
                    break

        assert new_checked is not None
        assert new_checked != original_checked

    def test_toggle_checkbox_not_found_raises(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Toggle checkbox raises if item not found."""
        with pytest.raises(ValueError, match="not found"):
            toggle_checkbox(parsed_richtext, "nonexistent checklist item xyz")

    def test_toggle_checkbox_preserves_other_formatting(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Toggle checkbox preserves other formatting."""
        original_run_count = len(parsed_richtext.runs)
        original_text = parsed_richtext.text

        result = toggle_checkbox(parsed_richtext, "This needs to happen")

        # Text should be unchanged
        assert result.text == original_text
        # Run count should be unchanged
        assert len(result.runs) == original_run_count


class TestChecklistItemChecked:
    """Read side of toggle_checkbox, used to verify a toggle landed (#177)."""

    def test_reads_unchecked_item(self, parsed_richtext: NotesRichtext) -> None:
        assert checklist_item_checked(parsed_richtext, "This needs to happen") is False

    def test_reads_toggled_item(self, parsed_richtext: NotesRichtext) -> None:
        toggled = toggle_checkbox(parsed_richtext, "This needs to happen")
        assert checklist_item_checked(toggled, "This needs to happen") is True

    def test_missing_item_is_none(self, parsed_richtext: NotesRichtext) -> None:
        assert checklist_item_checked(parsed_richtext, "nonexistent xyz") is None

    def test_non_checklist_text_is_none(self, parsed_richtext: NotesRichtext) -> None:
        """Text that exists only outside a checklist paragraph isn't an item —
        same match rule as toggle_checkbox, which would raise on it."""
        assert "great" in parsed_richtext.text
        with pytest.raises(ValueError):
            toggle_checkbox(parsed_richtext, "great")
        assert checklist_item_checked(parsed_richtext, "great") is None


class TestOperationsValidation:
    """Tests for validation after operations."""

    def test_find_replace_result_validates(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Find/replace result passes validation."""
        result = find_replace(parsed_richtext, "great", "magnificent")

        # Should not raise
        result.validate()

    def test_toggle_checkbox_result_validates(
        self, parsed_richtext: NotesRichtext
    ) -> None:
        """Toggle checkbox result passes validation."""
        result = toggle_checkbox(parsed_richtext, "This needs to happen")

        # Should not raise
        result.validate()


class TestFindReplaceSearchStart:
    """Title protection: find_replace must be able to skip the title line.

    Regression tests for NTG-003/NTG-004 (issue #120) — the note's first line
    is the Apple Notes title, and find_replace must never rewrite it.
    """

    @staticmethod
    def _plain(text: str) -> NotesRichtext:
        return NotesRichtext(text=text, runs=[AttributeRun(length=len(text))])

    def test_search_start_skips_earlier_occurrence(self) -> None:
        """replace_all starting after the title leaves the title untouched."""
        richtext = self._plain("Project X Plan\nProject X notes\n")
        body_start = richtext.text.index("\n") + 1

        result = find_replace(
            richtext,
            "Project X",
            "Project Y",
            replace_all=True,
            search_start=body_start,
        )

        assert result.text == "Project X Plan\nProject Y notes\n"
        result.validate()

    def test_search_start_returns_unchanged_when_only_in_prefix(self) -> None:
        """A match that lives only in the title is not a match at all."""
        richtext = self._plain("Status\nbody text\n")
        body_start = richtext.text.index("\n") + 1

        result = find_replace(
            richtext, "Status", "Done", replace_all=True, search_start=body_start
        )

        assert result.text == "Status\nbody text\n"

    def test_search_start_honored_for_first_occurrence_only(self) -> None:
        """replace_all=False still starts searching at search_start."""
        richtext = self._plain("Status\nStatus: pending\nStatus: later\n")
        body_start = richtext.text.index("\n") + 1

        result = find_replace(
            richtext, "Status", "Done", replace_all=False, search_start=body_start
        )

        assert result.text == "Status\nDone: pending\nStatus: later\n"
        result.validate()

    def test_formatted_replacement_skips_prefix(self) -> None:
        """Formatted (markdown) replacements honor search_start too."""
        richtext = self._plain("Status\nStatus: pending\n")
        body_start = richtext.text.index("\n") + 1
        replacement_runs = [AttributeRun(length=4, font_style=FontStyle.BOLD)]

        result = find_replace(
            richtext,
            "Status",
            "Done",
            replace_all=True,
            replacement_runs=replacement_runs,
            search_start=body_start,
        )

        assert result.text == "Status\nDone: pending\n"
        result.validate()

    def test_formatted_replacement_replaces_all_occurrences(self) -> None:
        """Formatted replacements honor replace_all instead of only the first."""
        richtext = self._plain("a Status and Status here\n")
        replacement_runs = [AttributeRun(length=4, font_style=FontStyle.BOLD)]

        result = find_replace(
            richtext,
            "Status",
            "Done",
            replace_all=True,
            replacement_runs=replacement_runs,
        )

        assert result.text == "a Done and Done here\n"
        result.validate()

    def test_formatted_replacement_first_only_when_replace_all_false(self) -> None:
        """replace_all=False keeps the single-occurrence formatted behavior."""
        richtext = self._plain("a Status and Status here\n")
        replacement_runs = [AttributeRun(length=4, font_style=FontStyle.BOLD)]

        result = find_replace(
            richtext,
            "Status",
            "Done",
            replace_all=False,
            replacement_runs=replacement_runs,
        )

        assert result.text == "a Done and Status here\n"
        result.validate()
