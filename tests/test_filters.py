"""Tests for the folder filtering utilities."""
from dataclasses import dataclass

import pytest

from apple_notes.filters import matches_folder_filter, filter_notes


@dataclass
class MockNote:
    """Mock note for testing."""
    name: str
    folder: str


class TestMatchesFolderFilter:
    """Tests for the matches_folder_filter function."""

    def test_exact_match(self):
        """Should match when folder exactly matches pattern."""
        assert matches_folder_filter("Archive", ["Archive"]) is True

    def test_substring_match(self):
        """Should match when folder contains pattern as substring."""
        assert matches_folder_filter("Other/Archive", ["Archive"]) is True
        assert matches_folder_filter("Work/Archive/Old", ["Archive"]) is True

    def test_case_insensitive(self):
        """Should match case-insensitively."""
        assert matches_folder_filter("Archive", ["archive"]) is True
        assert matches_folder_filter("ARCHIVE", ["Archive"]) is True
        assert matches_folder_filter("archive", ["ARCHIVE"]) is True

    def test_multiple_patterns(self):
        """Should match if any pattern matches."""
        assert matches_folder_filter("Archive", ["Work", "Archive"]) is True
        assert matches_folder_filter("Work", ["Work", "Archive"]) is True
        assert matches_folder_filter("Personal", ["Work", "Archive"]) is False

    def test_no_match(self):
        """Should not match when folder doesn't contain pattern."""
        assert matches_folder_filter("Work", ["Archive"]) is False
        assert matches_folder_filter("Personal/Notes", ["Archive"]) is False

    def test_empty_patterns(self):
        """Should not match when patterns list is empty."""
        assert matches_folder_filter("Archive", []) is False

    def test_folder_with_emoji(self):
        """Should handle folders with emoji characters."""
        assert matches_folder_filter("Other/🗄️ Archive", ["Archive"]) is True
        assert matches_folder_filter("Other/🗄️ Archive", ["🗄️"]) is True


class TestFilterNotes:
    """Tests for the filter_notes function."""

    @pytest.fixture
    def sample_notes(self):
        """Create sample notes for testing."""
        return [
            MockNote(name="Work Note 1", folder="Work"),
            MockNote(name="Work Note 2", folder="Work/Projects"),
            MockNote(name="Personal Note", folder="Personal"),
            MockNote(name="Archived Item", folder="Other/Archive"),
            MockNote(name="Trash Item", folder="Recently Deleted"),
        ]

    def test_no_filters(self, sample_notes):
        """Should return all notes when no filters are applied."""
        result = filter_notes(sample_notes)
        assert len(result) == 5

    def test_exclude_single_folder(self, sample_notes):
        """Should exclude notes matching single folder pattern."""
        result = filter_notes(sample_notes, exclude_folders=["Archive"])
        assert len(result) == 4
        assert not any(n.folder == "Other/Archive" for n in result)

    def test_exclude_multiple_folders(self, sample_notes):
        """Should exclude notes matching any of multiple patterns."""
        result = filter_notes(sample_notes, exclude_folders=["Archive", "Deleted"])
        assert len(result) == 3
        assert not any("Archive" in n.folder for n in result)
        assert not any("Deleted" in n.folder for n in result)

    def test_include_single_folder(self, sample_notes):
        """Should only include notes matching folder pattern."""
        result = filter_notes(sample_notes, include_folders=["Work"])
        assert len(result) == 2
        assert all("Work" in n.folder for n in result)

    def test_include_multiple_folders(self, sample_notes):
        """Should include notes matching any of multiple patterns."""
        result = filter_notes(sample_notes, include_folders=["Work", "Personal"])
        assert len(result) == 3
        assert all("Work" in n.folder or "Personal" in n.folder for n in result)

    def test_include_then_exclude(self, sample_notes):
        """Should apply include first, then exclude from included set."""
        # Include Work notes, then exclude Projects
        notes = [
            MockNote(name="Work Note 1", folder="Work"),
            MockNote(name="Work Project", folder="Work/Projects"),
            MockNote(name="Personal Note", folder="Personal"),
        ]
        result = filter_notes(notes, include_folders=["Work"], exclude_folders=["Projects"])
        assert len(result) == 1
        assert result[0].name == "Work Note 1"

    def test_empty_result(self, sample_notes):
        """Should return empty list when all notes are excluded."""
        result = filter_notes(sample_notes, include_folders=["Nonexistent"])
        assert result == []

    def test_none_filters_same_as_empty(self, sample_notes):
        """None filters should behave same as not filtering."""
        result1 = filter_notes(sample_notes)
        result2 = filter_notes(sample_notes, exclude_folders=None, include_folders=None)
        assert result1 == result2

    def test_preserves_note_type(self, sample_notes):
        """Should preserve the type of notes in the result."""
        result = filter_notes(sample_notes, exclude_folders=["Archive"])
        assert all(isinstance(n, MockNote) for n in result)
