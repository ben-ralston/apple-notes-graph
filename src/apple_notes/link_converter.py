"""Find and resolve [[Note Name]] links in note content."""
import re
from typing import Optional, List, Set

from .database import NotesDatabase


class LinkConverter:
    """
    Find and resolve [[Note Name]] links in note content.

    Note: This class caches note names for performance. If notes are created
    or deleted externally between calls, use refresh_cache() to update the
    cache, or create a new instance.
    """

    # Regex to find [[Note Name]] patterns
    LINK_PATTERN = re.compile(r'\[\[([^\]]+)\]\]')

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize link converter.

        Args:
            db_path: Path to the Notes database
        """
        self.db_path = db_path
        self._note_names_cache: Optional[Set[str]] = None

    def refresh_cache(self) -> None:
        """
        Refresh the cached note names from the database.

        Call this method after creating or deleting notes if reusing the same
        LinkConverter instance across multiple operations.
        """
        self._note_names_cache = None

    def _get_note_names(self) -> Set[str]:
        """Get set of all note names from database (cached)."""
        if self._note_names_cache is None:
            with NotesDatabase(self.db_path) as db:
                notes = db.get_all_notes()
                self._note_names_cache = {note.name for note in notes}
        return self._note_names_cache

    def find_links(self, text: str) -> List[str]:
        """
        Find all [[Note Name]] links in text.

        Args:
            text: Text to search

        Returns:
            List of note names found in links (may include duplicates)
        """
        return self.LINK_PATTERN.findall(text)

    def resolve_link(self, note_name: str) -> bool:
        """
        Check if a note name exists in the database.

        Args:
            note_name: Name of the note to find

        Returns:
            True if note exists, False otherwise
        """
        note_names = self._get_note_names()
        return note_name in note_names
