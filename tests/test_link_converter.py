"""Tests for the link converter module."""
from unittest.mock import patch, MagicMock

import pytest

from apple_notes.link_converter import LinkConverter
from apple_notes.models import Note


class TestFindLinks:
    """Tests for the find_links method."""

    def test_find_single_link(self):
        """Should find a single [[Note Name]] link."""
        converter = LinkConverter()
        links = converter.find_links("Check out [[My Note]] for details")

        assert links == ["My Note"]

    def test_find_multiple_links(self):
        """Should find multiple [[Note Name]] links."""
        converter = LinkConverter()
        links = converter.find_links("See [[Note A]] and [[Note B]] for info")

        assert links == ["Note A", "Note B"]

    def test_find_no_links(self):
        """Should return empty list when no links present."""
        converter = LinkConverter()
        links = converter.find_links("This text has no links")

        assert links == []

    def test_find_duplicate_links(self):
        """Should return duplicates when same link appears multiple times."""
        converter = LinkConverter()
        links = converter.find_links("[[Note A]] and then [[Note A]] again")

        assert links == ["Note A", "Note A"]

    def test_find_links_with_special_chars(self):
        """Should find links with special characters in name."""
        converter = LinkConverter()
        links = converter.find_links("Check [[My Note: Part 1]] here")

        assert links == ["My Note: Part 1"]

    def test_find_links_empty_brackets(self):
        """Should not match empty brackets."""
        converter = LinkConverter()
        links = converter.find_links("Empty [[]] brackets")

        assert links == []

    def test_find_links_nested_brackets(self):
        """Should handle nested brackets correctly."""
        converter = LinkConverter()
        # [[Note [with] brackets]] should only match up to first ]]
        links = converter.find_links("[[Note [with]]")

        assert links == ["Note [with"]


class TestResolveLink:
    """Tests for the resolve_link method."""

    @patch("apple_notes.link_converter.NotesDatabase")
    def test_resolve_link_exists(self, mock_db_class):
        """Should return True when note exists."""
        mock_db = MagicMock()
        mock_db.get_all_notes.return_value = [
            Note(name="Existing Note", uuid="uuid-1", folder="Notes", pk=1)
        ]
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=False)

        converter = LinkConverter()
        result = converter.resolve_link("Existing Note")

        assert result is True

    @patch("apple_notes.link_converter.NotesDatabase")
    def test_resolve_link_not_exists(self, mock_db_class):
        """Should return False when note doesn't exist."""
        mock_db = MagicMock()
        mock_db.get_all_notes.return_value = [
            Note(name="Other Note", uuid="uuid-1", folder="Notes", pk=1)
        ]
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=False)

        converter = LinkConverter()
        result = converter.resolve_link("Nonexistent Note")

        assert result is False

    @patch("apple_notes.link_converter.NotesDatabase")
    def test_resolve_link_caches_notes(self, mock_db_class):
        """Should cache note names and not query database again."""
        mock_db = MagicMock()
        mock_db.get_all_notes.return_value = [
            Note(name="Note A", uuid="uuid-1", folder="Notes", pk=1),
            Note(name="Note B", uuid="uuid-2", folder="Notes", pk=2),
        ]
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=False)

        converter = LinkConverter()
        converter.resolve_link("Note A")
        converter.resolve_link("Note B")
        converter.resolve_link("Note A")

        # Database should only be queried once
        assert mock_db.get_all_notes.call_count == 1


class TestRefreshCache:
    """Tests for the refresh_cache method."""

    @patch("apple_notes.link_converter.NotesDatabase")
    def test_refresh_cache_clears_cache(self, mock_db_class):
        """Should clear the cache so next query hits database."""
        mock_db = MagicMock()
        mock_db.get_all_notes.return_value = [
            Note(name="Note A", uuid="uuid-1", folder="Notes", pk=1),
        ]
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=False)

        converter = LinkConverter()

        # First query populates cache
        converter.resolve_link("Note A")
        assert mock_db.get_all_notes.call_count == 1

        # Second query uses cache
        converter.resolve_link("Note A")
        assert mock_db.get_all_notes.call_count == 1

        # Refresh cache
        converter.refresh_cache()

        # Third query should hit database again
        converter.resolve_link("Note A")
        assert mock_db.get_all_notes.call_count == 2

    def test_refresh_cache_sets_cache_to_none(self):
        """Should set internal cache to None."""
        converter = LinkConverter()
        converter._note_names_cache = {"Note A", "Note B"}

        converter.refresh_cache()

        assert converter._note_names_cache is None
