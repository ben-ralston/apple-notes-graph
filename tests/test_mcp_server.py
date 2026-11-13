"""Tests for the Apple Notes MCP server."""

import json

from unittest.mock import ANY, patch, MagicMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from apple_notes import (
    Note,
    NoteContent,
    NoteLink,
    ExternalLink,
    Attachment,
    GraphLink,
    get_backlinks as fetch_backlinks,
    NoteNotFoundError,
    DatabaseNotFoundError,
    EncryptedNoteError,
    ProtobufParseError,
    AppleScriptError,
    UIScriptingError,
    AccessibilityPermissionError,
    SessionLockedError,
    NotesNotFrontmostError,
    WriteVerificationError,
    ClipboardError,
    FolderNotFoundError,
    BackupError,
    InvalidGraphParameterError,
    UIHelperNotAvailableError,
)
from apple_notes.exceptions import EditLockTimeoutError, UIHelperTimeoutError
from apple_notes.richtext import PendingKeyboardAction, RichtextConversionResult
from apple_notes.richtext.parser import NotesRichtextParseError
from apple_notes.richtext.serializer import NotesRichtextSerializeError
from apple_notes_mcp.server import (
    list_notes,
    list_folders,
    read_note_content,
    search_notes,
    get_note_graph,
    get_backlinks,
    add_note,
    update_note_content,
    delete_note,
    toggle_checklist_item,
    export_note,
    export_folder,
    export_graph,
    find_orphans,
    find_broken_links,
    note_health_check,
    suggest_links,
)


# Sample test data
SAMPLE_NOTES = [
    Note(
        name="Meeting Notes",
        uuid="uuid-1",
        folder="Work",
        pk=1,
        created="2024-01-15T10:30:00Z",
        modified="2025-01-05T14:22:00Z",
        last_opened="2025-01-05T16:45:00Z",
    ),
    Note(
        name="Shopping List",
        uuid="uuid-2",
        folder="Personal",
        pk=2,
        created="2024-02-20T08:00:00Z",
        modified="2025-01-04T09:15:00Z",
        last_opened=None,
    ),
    Note(
        name="Project Ideas",
        uuid="uuid-3",
        folder="Work/Projects",
        pk=3,
        created="2024-03-10T11:45:00Z",
        modified="2025-01-03T17:30:00Z",
        last_opened="2025-01-03T18:00:00Z",
    ),
]

SAMPLE_NOTES_WITH_ARCHIVE = [
    Note(
        name="Work Note",
        uuid="uuid-1",
        folder="Work",
        pk=1,
        created="2024-01-01T00:00:00Z",
        modified="2025-01-01T00:00:00Z",
        last_opened=None,
    ),
    Note(
        name="Personal Note",
        uuid="uuid-2",
        folder="Personal",
        pk=2,
        created="2024-01-01T00:00:00Z",
        modified="2025-01-01T00:00:00Z",
        last_opened=None,
    ),
    Note(
        name="Archived Item",
        uuid="uuid-3",
        folder="Other/Archive",
        pk=3,
        created="2024-01-01T00:00:00Z",
        modified="2025-01-01T00:00:00Z",
        last_opened=None,
    ),
    Note(
        name="Trash Item",
        uuid="uuid-4",
        folder="Recently Deleted",
        pk=4,
        created="2024-01-01T00:00:00Z",
        modified="2025-01-01T00:00:00Z",
        last_opened=None,
    ),
]

SAMPLE_NOTE_CONTENT = NoteContent(
    name="Meeting Notes",
    uuid="uuid-1",
    folder="Work",
    raw_text="Meeting Notes\nDiscuss [[Project Ideas]] and review tasks",
    resolved_text="Meeting Notes\nDiscuss [[Project Ideas]] and review tasks",
    note_links=[
        NoteLink(target_note_name="Project Ideas", target_uuid="uuid-3", position=25)
    ],
    external_links=[
        ExternalLink(url="https://example.com", text="example", position=50, length=7)
    ],
    attachments=[Attachment(identifier="att-1", type_uti="public.jpeg", position=100)],
    created="2024-01-15T10:30:00Z",
    modified="2025-01-05T14:22:00Z",
    last_opened="2025-01-05T16:45:00Z",
)


SAMPLE_FOLDERS = [
    {"name": "Notes", "note_count": 10, "parent": None, "full_path": "Notes"},
    {"name": "Work", "note_count": 25, "parent": None, "full_path": "Work"},
    {
        "name": "Archive",
        "note_count": 5,
        "parent": "Other",
        "full_path": "Other/Archive",
    },
    {"name": "Personal", "note_count": 15, "parent": None, "full_path": "Personal"},
]


pytestmark = pytest.mark.usefixtures("stub_note_resolution")



class TestListFolders:
    """Tests for the list_folders tool."""

    @patch("apple_notes_mcp.server.NotesDatabase")
    def test_list_all_folders(self, mock_db_class):
        """Should list all folders with note counts."""
        mock_db = MagicMock()
        mock_db.get_all_folders.return_value = SAMPLE_FOLDERS
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)

        result = list_folders()

        assert len(result) == 4
        assert result[0]["name"] == "Notes"
        assert result[0]["note_count"] == 10
        assert result[0]["parent"] is None
        assert result[0]["full_path"] == "Notes"

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.NotesDatabase")
    def test_list_folders_respects_config_exclusions(
        self, mock_db_class, mock_get_filters
    ):
        """Should exclude folders based on config exclusions by default."""
        mock_db = MagicMock()
        mock_db.get_all_folders.return_value = SAMPLE_FOLDERS
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)
        mock_get_filters.return_value = (["Archive"], None)

        result = list_folders()

        assert len(result) == 3
        assert not any("Archive" in f["full_path"] for f in result)

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.NotesDatabase")
    def test_list_folders_include_all(self, mock_db_class, mock_get_filters):
        """Should return all folders when include_all is True."""
        mock_db = MagicMock()
        mock_db.get_all_folders.return_value = SAMPLE_FOLDERS
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)
        mock_get_filters.return_value = (None, None)

        result = list_folders(include_all=True)

        assert len(result) == 4

    @patch("apple_notes_mcp.server.NotesDatabase")
    def test_list_folders_database_not_found(self, mock_db_class):
        """Should raise ToolError when database is not found."""
        mock_db_class.return_value.__enter__ = MagicMock(
            side_effect=DatabaseNotFoundError("Database not found")
        )

        with pytest.raises(ToolError) as exc_info:
            list_folders()

        assert "database not found" in str(exc_info.value).lower()


class TestListNotes:
    """Tests for the list_notes tool."""

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_all_notes(self, mock_reader_class):
        """Should list all notes sorted by modified date (most recent first)."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = list_notes()

        assert len(result) == 3
        # Default fields include name, folder, modified
        assert result[0]["name"] == "Meeting Notes"
        assert result[0]["folder"] == "Work"
        assert result[0]["modified"] == "2025-01-05T14:22:00Z"
        # uuid is hidden by default (issue #26)
        assert "uuid" not in result[0]
        # Verify sorted by modified (most recent first)
        assert result[0]["modified"] > result[1]["modified"] > result[2]["modified"]

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_with_include_folders_filter(
        self, mock_reader_class, mock_get_filters
    ):
        """Should filter notes by folder when include_folders is provided."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader
        # Only include Work folder
        mock_get_filters.return_value = (None, ["Work"])

        result = list_notes(include_folders=["Work"])

        assert len(result) == 2
        assert all("Work" in n["folder"] for n in result)

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_database_not_found(self, mock_reader_class):
        """Should raise ToolError when database is not found."""
        mock_reader_class.side_effect = DatabaseNotFoundError("Database not found")

        with pytest.raises(ToolError) as exc_info:
            list_notes()

        assert "database not found" in str(exc_info.value).lower()


class TestListNotesPagination:
    """Tests for list_notes pagination features."""

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_with_limit(self, mock_reader_class):
        """Should return limited number of notes with pagination metadata."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = list_notes(limit=2)

        assert len(result["notes"]) == 2
        assert result["total"] == 3
        assert result["limit"] == 2
        assert result["offset"] == 0

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_with_offset(self, mock_reader_class):
        """Should skip notes with offset."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = list_notes(offset=1)

        assert result["offset"] == 1
        assert result["total"] == 3
        assert result["limit"] is None
        assert len(result["notes"]) == 2  # 3 notes - 1 offset = 2 returned

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_with_limit_and_offset(self, mock_reader_class):
        """Should return notes with both limit and offset."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = list_notes(limit=1, offset=1)

        assert len(result["notes"]) == 1
        assert result["total"] == 3
        assert result["limit"] == 1
        assert result["offset"] == 1
        # Verify the returned note is the second one (after offset)
        assert result["notes"][0]["name"] == "Shopping List"

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_count_only(self, mock_reader_class):
        """Should return only count when count_only is True."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = list_notes(count_only=True)

        assert result == {"total": 3}

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_with_fields(self, mock_reader_class):
        """Should return only requested fields."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = list_notes(fields=["name", "uuid"])

        assert len(result) == 3
        assert "name" in result[0]
        assert "uuid" in result[0]
        assert "folder" not in result[0]

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_with_date_fields(self, mock_reader_class):
        """Should return date fields when requested."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = list_notes(fields=["name", "created", "modified", "last_opened"])

        assert len(result) == 3
        assert result[0]["name"] == "Meeting Notes"
        assert result[0]["created"] == "2024-01-15T10:30:00Z"
        assert result[0]["modified"] == "2025-01-05T14:22:00Z"
        assert result[0]["last_opened"] == "2025-01-05T16:45:00Z"
        # Second note has null last_opened
        assert result[1]["last_opened"] is None

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_with_fields_and_pagination(self, mock_reader_class):
        """Should return fields with pagination metadata."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = list_notes(limit=2, fields=["name"])

        assert len(result["notes"]) == 2
        assert "name" in result["notes"][0]
        assert "folder" not in result["notes"][0]
        assert result["total"] == 3

    def test_list_notes_invalid_fields(self):
        """Should raise ToolError for invalid fields."""
        with pytest.raises(ToolError) as exc_info:
            list_notes(fields=["invalid_field"])

        assert "invalid_field" in str(exc_info.value)

    def test_list_notes_invalid_limit(self):
        """Should raise ToolError for invalid limit."""
        with pytest.raises(ToolError) as exc_info:
            list_notes(limit=-1)

        assert "positive" in str(exc_info.value)

    def test_list_notes_invalid_offset(self):
        """Should raise ToolError for invalid offset."""
        with pytest.raises(ToolError) as exc_info:
            list_notes(offset=-5)

        assert "non-negative" in str(exc_info.value)

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_list_notes_with_include_folders_and_pagination(
        self, mock_reader_class, mock_get_filters
    ):
        """Should paginate filtered results when include_folders is specified."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader
        mock_get_filters.return_value = (None, ["Work"])

        result = list_notes(include_folders=["Work"], limit=1)

        # 2 Work notes, limit 1, so we get 1 note with total=2
        assert len(result["notes"]) == 1
        assert result["total"] == 2
        assert result["limit"] == 1


class TestReadNoteContent:
    """Tests for the read_note_content tool."""

    @patch("apple_notes_mcp.server.read_note")
    def test_read_note_success(self, mock_read_note):
        """Should return note content with resolved links and dates."""
        mock_read_note.return_value = SAMPLE_NOTE_CONTENT

        result = read_note_content("Meeting Notes")

        assert result["name"] == "Meeting Notes"
        assert result["folder"] == "Work"
        # uuid is hidden by default (issue #26)
        assert "uuid" not in result
        # Dates should be included
        assert result["created"] == "2024-01-15T10:30:00Z"
        assert result["modified"] == "2025-01-05T14:22:00Z"
        assert result["last_opened"] == "2025-01-05T16:45:00Z"
        assert "[[Project Ideas]]" in result["content"]
        assert len(result["note_links"]) == 1
        assert result["note_links"][0]["target"] == "Project Ideas"
        # note_links still include uuid for reference
        assert result["note_links"][0]["uuid"] == "uuid-3"
        assert len(result["external_links"]) == 1
        assert result["external_links"][0]["url"] == "https://example.com"
        assert len(result["attachments"]) == 1

    @patch("apple_notes_mcp.server.read_note")
    def test_read_note_not_found(self, mock_read_note):
        """Should raise ToolError when note is not found."""
        mock_read_note.side_effect = NoteNotFoundError("Note not found")

        with pytest.raises(ToolError) as exc_info:
            read_note_content("Nonexistent Note")

        assert "Nonexistent Note" in str(exc_info.value)

    @patch("apple_notes_mcp.server.read_note")
    def test_read_encrypted_note(self, mock_read_note):
        """Should raise ToolError when note is encrypted."""
        mock_read_note.side_effect = EncryptedNoteError("Note is encrypted")

        with pytest.raises(ToolError) as exc_info:
            read_note_content("Secret Note")

        assert "encrypted" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.read_note")
    def test_numbered_lines_true(self, mock_read_note):
        """Should format content with cat -n style line numbers when numbered_lines=True."""
        mock_content = MagicMock()
        mock_content.name = "Test Note"
        mock_content.folder = "Notes"
        mock_content.created = "2024-01-01T00:00:00Z"
        mock_content.modified = "2024-01-01T00:00:00Z"
        mock_content.last_opened = None
        mock_content.resolved_text = "Test Note\nFirst line\nSecond line"
        mock_content.note_links = []
        mock_content.external_links = []
        mock_content.attachments = []
        mock_read_note.return_value = mock_content

        result = read_note_content("Test Note", numbered_lines=True)

        assert (
            result["content"]
            == "     1\tTest Note\n     2\tFirst line\n     3\tSecond line"
        )

    @patch("apple_notes_mcp.server.read_note")
    def test_numbered_lines_false_default(self, mock_read_note):
        """Should return content unchanged when numbered_lines=False (default)."""
        mock_read_note.return_value = SAMPLE_NOTE_CONTENT

        result = read_note_content("Meeting Notes")

        # Content should be the raw resolved_text without line numbers
        assert result["content"] == SAMPLE_NOTE_CONTENT.resolved_text
        assert not result["content"].startswith("     1")

    @patch("apple_notes_mcp.server.read_note")
    def test_numbered_lines_alignment(self, mock_read_note):
        """Should right-align line numbers in a 6-char field like cat -n."""
        mock_content = MagicMock()
        mock_content.name = "Long Note"
        mock_content.folder = "Notes"
        mock_content.created = "2024-01-01T00:00:00Z"
        mock_content.modified = "2024-01-01T00:00:00Z"
        mock_content.last_opened = None
        # Create 10+ lines to test alignment
        lines = ["Long Note"] + [f"Line {i}" for i in range(2, 12)]
        mock_content.resolved_text = "\n".join(lines)
        mock_content.note_links = []
        mock_content.external_links = []
        mock_content.attachments = []
        mock_read_note.return_value = mock_content

        result = read_note_content("Long Note", numbered_lines=True)

        content_lines = result["content"].split("\n")
        # Line 1 should be right-aligned
        assert content_lines[0].startswith("     1\t")
        # Line 10 should also be right-aligned
        assert content_lines[9].startswith("    10\t")

    @patch("apple_notes_mcp.server.fetch_backlinks")
    @patch("apple_notes_mcp.server.read_note")
    def test_include_backlinks_false_omits_backlinks(self, mock_read_note, mock_fetch):
        """Should not include backlinks field when include_backlinks=False (default)."""
        mock_read_note.return_value = SAMPLE_NOTE_CONTENT

        result = read_note_content("Meeting Notes")

        assert "backlinks" not in result
        mock_fetch.assert_not_called()

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.fetch_backlinks")
    @patch("apple_notes_mcp.server.read_note")
    def test_include_backlinks_true_adds_backlinks(
        self, mock_read_note, mock_fetch, mock_get_filters
    ):
        """Should include backlinks array when include_backlinks=True."""
        mock_read_note.return_value = SAMPLE_NOTE_CONTENT
        mock_get_filters.return_value = (["Archive"], None)
        mock_fetch.return_value = ["Weekly Review", "Project Plan"]

        result = read_note_content("Meeting Notes", include_backlinks=True)

        assert result["backlinks"] == ["Weekly Review", "Project Plan"]
        mock_fetch.assert_called_once_with(
            "Meeting Notes", exclude_folders=["Archive"], include_folders=None, note_pk=1
        )

    @patch("apple_notes_mcp.server.fetch_backlinks")
    @patch("apple_notes_mcp.server.read_note")
    def test_include_backlinks_true_empty_list(self, mock_read_note, mock_fetch):
        """Should include empty backlinks array when note has no backlinks."""
        mock_read_note.return_value = SAMPLE_NOTE_CONTENT
        mock_fetch.return_value = []

        result = read_note_content("Meeting Notes", include_backlinks=True)

        assert result["backlinks"] == []


class TestSearchNotes:
    """Tests for the search_notes tool."""

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_search_by_title(self, mock_reader_class):
        """Should find notes by title match."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = search_notes("Meeting")

        assert len(result) == 1
        assert result[0]["name"] == "Meeting Notes"
        assert result[0]["match_type"] == "title"

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_search_case_insensitive(self, mock_reader_class):
        """Should search case-insensitively."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = search_notes("meeting")

        assert len(result) == 1
        assert result[0]["name"] == "Meeting Notes"

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_search_content(self, mock_reader_class):
        """Should search in content when search_content is True."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES

        # Return different content for each note
        def get_content_side_effect(name):
            if name == "Meeting Notes":
                return SAMPLE_NOTE_CONTENT
            # Return content that doesn't match for other notes
            return NoteContent(
                name=name,
                uuid="uuid-x",
                folder="Other",
                raw_text="Nothing here",
                resolved_text="Nothing here",
                created="2024-01-01T00:00:00Z",
                modified="2024-01-01T00:00:00Z",
                last_opened=None,
            )

        mock_reader.get_note_content.side_effect = get_content_side_effect
        mock_reader_class.return_value = mock_reader

        result = search_notes("review tasks", search_content=True)

        # Should find it in content
        assert len(result) == 1
        assert result[0]["match_type"] == "content"
        assert "snippet" in result[0]

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_search_no_results(self, mock_reader_class):
        """Should return empty list when no matches found."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES
        mock_reader_class.return_value = mock_reader

        result = search_notes("nonexistent")

        assert result == []


class TestGetNoteGraph:
    """Tests for the get_note_graph tool."""

    @patch("apple_notes_mcp.server.get_graph_data")
    def test_get_graph_success(self, mock_get_graph):
        """Should return graph data with notes and links."""
        mock_get_graph.return_value = {
            "notes": [
                {"name": "Note A", "folder": "Work"},
                {"name": "Note B", "folder": "Personal"},
            ],
            "links": [
                {"source": "Note A", "target": "Note B"},
            ],
            "stats": {
                "total_notes": 2,
                "total_links": 1,
            },
        }

        result = get_note_graph()

        assert len(result["notes"]) == 2
        assert len(result["links"]) == 1
        assert result["stats"]["total_notes"] == 2
        # Graph nodes only have name and folder (no dates)
        assert result["notes"][0]["name"] == "Note A"
        assert result["notes"][0]["folder"] == "Work"
        assert "created" not in result["notes"][0]
        assert "modified" not in result["notes"][0]

    @patch("apple_notes_mcp.server.get_graph_data")
    def test_get_graph_database_not_found(self, mock_get_graph):
        """Should raise ToolError when database is not found."""
        mock_get_graph.side_effect = DatabaseNotFoundError("Database not found")

        with pytest.raises(ToolError) as exc_info:
            get_note_graph()

        assert "database not found" in str(exc_info.value).lower()


class TestListNotesFiltering:
    """Tests for list_notes filtering behavior."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_exclude_folders_parameter(self, mock_reader_class, mock_get_filters):
        """Should exclude notes from specified folders."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES_WITH_ARCHIVE
        mock_reader_class.return_value = mock_reader
        mock_get_filters.return_value = (["Archive"], None)

        result = list_notes(exclude_folders=["Archive"])

        assert len(result) == 3
        assert not any("Archive" in n["folder"] for n in result)

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_include_all_bypasses_config(self, mock_reader_class, mock_get_filters):
        """Should return all notes when include_all is True."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES_WITH_ARCHIVE
        mock_reader_class.return_value = mock_reader
        mock_get_filters.return_value = (None, None)  # No filtering

        result = list_notes(include_all=True)

        assert len(result) == 4


class TestSearchNotesFiltering:
    """Tests for search_notes filtering behavior."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_search_excludes_filtered_folders(
        self, mock_reader_class, mock_get_filters
    ):
        """Should not search in excluded folders."""
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = SAMPLE_NOTES_WITH_ARCHIVE
        mock_reader_class.return_value = mock_reader
        mock_get_filters.return_value = (["Archive", "Deleted"], None)

        result = search_notes("Note")

        # Should find Work Note and Personal Note, but not Archived or Trash items
        assert len(result) == 2
        names = [r["name"] for r in result]
        assert "Work Note" in names
        assert "Personal Note" in names
        assert "Archived Item" not in names
        assert "Trash Item" not in names


class TestGetNoteGraphFiltering:
    """Tests for get_note_graph filtering behavior."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_passes_filters_to_get_graph_data(self, mock_get_graph, mock_get_filters):
        """Should pass effective filters to get_graph_data."""
        mock_get_filters.return_value = (["Archive"], ["Work"])
        mock_get_graph.return_value = {
            "notes": [{"name": "Test", "folder": "Work"}],
            "links": [],
            "stats": {"total_notes": 1, "total_links": 0},
        }

        get_note_graph(exclude_folders=["Archive"], include_folders=["Work"])

        mock_get_graph.assert_called_once_with(
            exclude_folders=["Archive"],
            include_folders=["Work"],
            start_note=None,
            depth=None,
            format="full",
        )

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_include_all_passes_none(self, mock_get_graph, mock_get_filters):
        """Should pass None filters when include_all is True."""
        mock_get_filters.return_value = (None, None)
        mock_get_graph.return_value = {
            "notes": [],
            "links": [],
            "stats": {"total_notes": 0, "total_links": 0},
        }

        get_note_graph(include_all=True)

        mock_get_graph.assert_called_once_with(
            exclude_folders=None,
            include_folders=None,
            start_note=None,
            depth=None,
            format="full",
        )


class TestGetNoteGraphBFS:
    """Tests for get_note_graph BFS traversal feature."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_start_note_parameter(self, mock_get_graph, mock_get_filters):
        """Should pass start_note to core library."""
        mock_get_filters.return_value = (None, None)
        mock_get_graph.return_value = {
            "notes": [{"name": "Home", "folder": "Notes"}],
            "links": [],
            "stats": {"total_notes": 1, "total_links": 0},
        }

        get_note_graph(start_note="Home")

        mock_get_graph.assert_called_once_with(
            exclude_folders=None,
            include_folders=None,
            start_note="Home",
            depth=None,
            format="full",
        )

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_start_note_with_depth(self, mock_get_graph, mock_get_filters):
        """Should pass both start_note and depth."""
        mock_get_filters.return_value = (None, None)
        mock_get_graph.return_value = {
            "notes": [{"name": "Home", "folder": "Notes"}],
            "links": [],
            "stats": {"total_notes": 1, "total_links": 0},
        }

        get_note_graph(start_note="Home", depth=2)

        mock_get_graph.assert_called_once_with(
            exclude_folders=None,
            include_folders=None,
            start_note="Home",
            depth=2,
            format="full",
        )

    def test_depth_without_start_note_error(self):
        """Should raise ToolError when depth given without start_note."""
        with pytest.raises(ToolError) as exc_info:
            get_note_graph(depth=2)

        assert "start_note" in str(exc_info.value)

    def test_invalid_depth_zero_error(self):
        """Should raise ToolError for depth of 0."""
        with pytest.raises(ToolError) as exc_info:
            get_note_graph(start_note="Home", depth=0)

        assert "positive" in str(exc_info.value).lower()

    def test_invalid_depth_negative_error(self):
        """Should raise ToolError for negative depth."""
        with pytest.raises(ToolError) as exc_info:
            get_note_graph(start_note="Home", depth=-1)

        assert "positive" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_start_note_not_found(self, mock_get_graph, mock_get_filters):
        """Should raise ToolError when start note doesn't exist."""
        mock_get_filters.return_value = (None, None)
        mock_get_graph.side_effect = NoteNotFoundError(
            "Start note 'Nonexistent' not found"
        )

        with pytest.raises(ToolError) as exc_info:
            get_note_graph(start_note="Nonexistent")

        assert "not found" in str(exc_info.value).lower()


class TestGetNoteGraphFormat:
    """Tests for get_note_graph format parameter."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_default_format_is_full(self, mock_get_graph, mock_get_filters):
        """Should default to full format."""
        mock_get_filters.return_value = (None, None)
        mock_get_graph.return_value = {
            "notes": [],
            "links": [],
            "stats": {"total_notes": 0, "total_links": 0},
        }

        get_note_graph()

        mock_get_graph.assert_called_once_with(
            exclude_folders=None,
            include_folders=None,
            start_note=None,
            depth=None,
            format="full",
        )

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_links_only_format(self, mock_get_graph, mock_get_filters):
        """Should pass links_only format to core library."""
        mock_get_filters.return_value = (None, None)
        mock_get_graph.return_value = {
            "links": [],
            "stats": {"total_links": 0},
        }

        get_note_graph(format="links_only")

        mock_get_graph.assert_called_once_with(
            exclude_folders=None,
            include_folders=None,
            start_note=None,
            depth=None,
            format="links_only",
        )

    def test_invalid_format_error(self):
        """Should raise ToolError for invalid format."""
        with pytest.raises(ToolError) as exc_info:
            get_note_graph(format="invalid")

        assert "invalid" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_bfs_with_links_only_format(self, mock_get_graph, mock_get_filters):
        """Should combine BFS traversal with links_only format."""
        mock_get_filters.return_value = (None, None)
        mock_get_graph.return_value = {
            "links": [],
            "stats": {"total_links": 0},
        }

        get_note_graph(start_note="Home", depth=1, format="links_only")

        mock_get_graph.assert_called_once_with(
            exclude_folders=None,
            include_folders=None,
            start_note="Home",
            depth=1,
            format="links_only",
        )


class TestTimingInstrumentation:
    """Issue #98: write tools carry elapsed_ms + timing; a log line lands."""

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_response_has_timing_and_logs(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        from apple_notes import profiling

        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"rt", pending_actions=[]
        )

        result = add_note(name="MCP Test", body="hi", folder="Notes")

        # Response fields.
        assert isinstance(result["elapsed_ms"], int)
        assert isinstance(result["timing"], dict)
        # Server-side phases recorded as spans.
        assert "backup" in result["timing"]
        assert "convert" in result["timing"]
        assert "execute" in result["timing"]
        assert "sync" in result["timing"]
        # A greppable START + END pair landed in the (redirected) log.
        lines = profiling.TIMING_LOG_PATH.read_text().splitlines()
        assert any(l.endswith("START") and "add_note" in l for l in lines)
        assert any('name="MCP Test"' in l and "total=" in l for l in lines)

    @patch("apple_notes_mcp.server.read_note")
    def test_read_tool_logged_only_no_response_fields(self, mock_read_note):
        content = MagicMock()
        content.name = "X"
        content.folder = "F"
        content.created = content.modified = content.last_opened = None
        content.resolved_text = "body"
        content.note_links = []
        content.external_links = []
        content.attachments = []
        mock_read_note.return_value = content

        result = read_note_content(name="X")
        assert "elapsed_ms" not in result
        assert "timing" not in result


class TestAddNote:
    """Tests for the add_note tool."""

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_success(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        """Should create note via markdown conversion and richtext paste."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )
        mock_resolver = MagicMock()
        mock_create_resolver.return_value = mock_resolver

        result = add_note(
            name="New Note", body="Link to [[Existing Note]]", folder="Notes"
        )

        assert result["success"] is True
        assert result["note_name"] == "New Note"
        assert result["folder"] == "Notes"
        assert result["backup_path"] == "/path/to/backup.sqlite"
        assert "format" not in result
        # Verify markdown conversion with link resolver
        mock_md_convert.assert_called_once_with(
            "Link to [[Existing Note]]", mock_resolver
        )
        mock_execute_create.assert_called_once_with(
            "New Note", b"richtext-data", "Notes", []
        )

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_with_pending_actions(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        """Should pass pending actions to execute_create_note."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        pending = [
            PendingKeyboardAction(
                placeholder="{__NG_TAG_0__}",
                action_type="hashtag",
                text_to_type="todo",
                original_markdown="#todo",
            ),
            PendingKeyboardAction(
                placeholder="{__NG_LINK_0__}",
                action_type="wiki_link",
                text_to_type="My Note",
                original_markdown="[[My Note]]",
            ),
        ]
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=pending
        )
        mock_create_resolver.return_value = MagicMock()

        result = add_note(name="New Note", body="#todo [[My Note]]", folder="Notes")

        assert result["success"] is True
        mock_execute_create.assert_called_once_with(
            "New Note", b"richtext-data", "Notes", pending
        )

    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_backup_failure(self, mock_backup_class):
        """Should raise ToolError when backup fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.side_effect = BackupError("Backup failed")
        mock_backup_class.return_value = mock_backup

        with pytest.raises(ToolError) as exc_info:
            add_note(name="New Note", body="Body")

        assert "backup" in str(exc_info.value).lower()

    def test_add_note_rejects_custom_folder(self):
        """Should raise ToolError when folder is not 'Notes'."""
        with pytest.raises(ToolError) as exc_info:
            add_note(name="New Note", body="Body", folder="NonexistentFolder")

        assert "Only 'Notes' folder is supported" in str(exc_info.value)
        assert "NonexistentFolder" in str(exc_info.value)

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_accessibility_error(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        """Should raise ToolError when accessibility permissions denied."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )
        mock_create_resolver.return_value = MagicMock()

        mock_execute_create.side_effect = AccessibilityPermissionError("Access denied")

        with pytest.raises(ToolError) as exc_info:
            add_note(name="New Note", body="Body")

        assert "accessibility" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_ui_scripting_error(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        """Should raise ToolError when UI scripting fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )
        mock_create_resolver.return_value = MagicMock()

        mock_execute_create.side_effect = UIScriptingError("UI scripting failed")

        with pytest.raises(ToolError) as exc_info:
            add_note(name="New Note", body="Body")

        assert "ui scripting" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_clipboard_error(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        """Should raise ToolError when clipboard operations fail."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )
        mock_create_resolver.return_value = MagicMock()

        mock_execute_create.side_effect = ClipboardError("Clipboard failed")

        with pytest.raises(ToolError) as exc_info:
            add_note(name="New Note", body="Body")

        assert "clipboard" in str(exc_info.value).lower()

    def test_add_note_empty_name(self):
        """Should raise ToolError for empty note name."""
        with pytest.raises(ToolError) as exc_info:
            add_note(name="", body="Body")

        assert "empty" in str(exc_info.value).lower()

    def test_add_note_whitespace_only_name(self):
        """Should raise ToolError for whitespace-only note name."""
        with pytest.raises(ToolError) as exc_info:
            add_note(name="   ", body="Body")

        assert "empty" in str(exc_info.value).lower()

    def test_add_note_name_too_long(self):
        """Should raise ToolError for note name exceeding max length."""
        long_name = "x" * 256

        with pytest.raises(ToolError) as exc_info:
            add_note(name=long_name, body="Body")

        assert "too long" in str(exc_info.value).lower()

    def test_add_note_name_with_newline(self):
        """Should raise ToolError for note name containing newlines."""
        with pytest.raises(ToolError) as exc_info:
            add_note(name="Note\nWith\nNewlines", body="Body")

        assert "newline" in str(exc_info.value).lower()

    def test_add_note_name_with_carriage_return(self):
        """Should raise ToolError for note name containing carriage returns."""
        with pytest.raises(ToolError) as exc_info:
            add_note(name="Note\rWith\rReturns", body="Body")

        assert "newline" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.nudge_icloud_sync", return_value=True)
    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_with_sync_nudge(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
        mock_nudge,
    ):
        """Should call nudge and include sync_nudged in response."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )

        result = add_note(name="New Note", body="Body", folder="Notes")

        assert result["sync_nudged"] is True
        mock_nudge.assert_called_once()


class TestUpdateNoteContent:
    """Tests for the update_note_content tool using UI scripting."""

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_append_note_success_default(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_uuid,
        mock_execute_append,
        mock_is_gui,
    ):
        """Should append to note by default using richtext conversion."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )

        result = update_note_content(
            name="My Note", body="New content", operation="append"
        )

        assert result["success"] is True
        assert result["note_name"] == "My Note"
        assert result["operation"] == "append"
        assert result["backup_path"] == "/path/to/backup.sqlite"
        mock_execute_append.assert_called_once_with("My Note", b"richtext-data", [])

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_append_with_pending_actions(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_uuid,
        mock_execute_append,
        mock_is_gui,
    ):
        """Should pass pending actions to execute_append_to_note."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        pending = [
            PendingKeyboardAction(
                placeholder="{__NG_TAG_0__}",
                action_type="hashtag",
                text_to_type="todo",
                original_markdown="#todo",
            ),
        ]
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=pending
        )

        result = update_note_content(
            name="My Note", body="#todo content", operation="append"
        )

        assert result["success"] is True
        mock_execute_append.assert_called_once_with(
            "My Note", b"richtext-data", pending
        )

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_replace_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_replace_note_success(
        self,
        mock_backup_class,
        mock_build_uuid,
        mock_execute_replace,
        mock_is_gui,
    ):
        """Should replace note body via execute_replace_note."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_build_uuid.return_value = {"my note": "uuid-1"}

        result = update_note_content(
            name="My Note", body="New content", operation="replace"
        )

        assert result["success"] is True
        assert result["note_name"] == "My Note"
        assert result["operation"] == "replace"
        assert result["backup_path"] == "/path/to/backup.sqlite"
        mock_execute_replace.assert_called_once_with("My Note", "New content", {})

    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_backup_failure(self, mock_backup_class):
        """Should raise ToolError when backup fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.side_effect = BackupError("Backup failed")
        mock_backup_class.return_value = mock_backup

        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="My Note", body="Content", operation="append")

        assert "backup" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_note_not_found(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_uuid,
        mock_execute_append,
        mock_is_gui,
    ):
        """Should raise ToolError when note doesn't exist."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )

        mock_execute_append.side_effect = NoteNotFoundError("Not found")

        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="Nonexistent Note", body="Content", operation="append"
            )

        assert "not found" in str(exc_info.value).lower()

    def test_missing_operation_raises_error(self):
        """Should raise TypeError when operation is not provided."""
        with pytest.raises(TypeError):
            update_note_content(name="My Note", body="Content")

    def test_invalid_operation_raises_error(self):
        """Should raise ToolError for invalid operation."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="My Note", body="Content", operation="invalid")

        assert "invalid operation" in str(exc_info.value).lower()

    # --- find_replace success cases ---

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_find_replace_multiline")
    @patch("apple_notes_mcp.server._build_name_to_uuid")
    @patch("apple_notes_mcp.server.read_note")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_find_replace_single_match(
        self,
        mock_backup_class,
        mock_read_note,
        mock_build_uuid,
        mock_execute_multiline,
        mock_is_gui,
    ):
        """Should call execute_find_replace_multiline with old_text and replacement."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_build_uuid.return_value = {"my note": "uuid-1"}

        mock_content = MagicMock()
        mock_content.resolved_text = (
            "# My Note\nFirst line\nSecond line\nThird line\nFourth line"
        )
        mock_content.raw_text = mock_content.resolved_text
        mock_read_note.return_value = mock_content

        result = update_note_content(
            name="My Note",
            body="Replaced line",
            operation="find_replace",
            old_text="Second line",
        )

        assert result["success"] is True
        assert result["operation"] == "find_replace"

        mock_execute_multiline.assert_called_once_with(
            "My Note",
            "Second line",
            "Replaced line",
            links={},
        )

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_find_replace_multiline")
    @patch("apple_notes_mcp.server._build_name_to_uuid")
    @patch("apple_notes_mcp.server.read_note")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_find_replace_multiline_old_text(
        self,
        mock_backup_class,
        mock_read_note,
        mock_build_uuid,
        mock_execute_multiline,
        mock_is_gui,
    ):
        """Should handle multiline old_text via execute_find_replace_multiline."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_build_uuid.return_value = {}

        mock_content = MagicMock()
        mock_content.resolved_text = (
            "# My Note\nFirst line\nSecond line\nThird line\nFourth line"
        )
        mock_content.raw_text = mock_content.resolved_text
        mock_read_note.return_value = mock_content

        result = update_note_content(
            name="My Note",
            body="New A\nNew B\nNew C",
            operation="find_replace",
            old_text="Second line\nThird line",
        )

        assert result["success"] is True
        mock_execute_multiline.assert_called_once_with(
            "My Note",
            "Second line\nThird line",
            "New A\nNew B\nNew C",
            links={},
        )

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_find_replace_multiline")
    @patch("apple_notes_mcp.server._build_name_to_uuid")
    @patch("apple_notes_mcp.server.read_note")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_find_replace_delete(
        self,
        mock_backup_class,
        mock_read_note,
        mock_build_uuid,
        mock_execute_multiline,
        mock_is_gui,
    ):
        """Should pass empty body for deletion."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_build_uuid.return_value = {}

        mock_content = MagicMock()
        mock_content.resolved_text = "# My Note\nFirst line\nSecond line\nThird line"
        mock_content.raw_text = mock_content.resolved_text
        mock_read_note.return_value = mock_content

        result = update_note_content(
            name="My Note",
            body="",
            operation="find_replace",
            old_text="Second line",
        )

        assert result["success"] is True
        mock_execute_multiline.assert_called_once_with(
            "My Note",
            "Second line",
            "",
            links={},
        )

    # --- find_replace validation errors ---

    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes_mcp.server.read_note")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_find_replace_not_found(
        self,
        mock_backup_class,
        mock_read_note,
        mock_build_uuid,
    ):
        """Should raise ToolError when old_text is not found in note body."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_content = MagicMock()
        mock_content.resolved_text = "# My Note\nFirst line\nSecond line"
        mock_read_note.return_value = mock_content

        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="My Note",
                body="replacement",
                operation="find_replace",
                old_text="nonexistent text",
            )

        assert "not found" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes_mcp.server.read_note")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_find_replace_table_cell_text_rejected(
        self,
        mock_backup_class,
        mock_read_note,
        mock_build_uuid,
    ):
        """Should raise ToolError when old_text is in rendered text but not raw data."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_content = MagicMock()
        # resolved_text has rendered table content
        mock_content.resolved_text = (
            "# My Note\nSome text\n| Cell A | Cell B |\n| --- | --- |\nMore text"
        )
        # raw_text has U+FFFC placeholder instead of table content
        mock_content.raw_text = "# My Note\nSome text\n\ufffc\nMore text"
        mock_read_note.return_value = mock_content

        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="My Note",
                body="New Cell A",
                operation="find_replace",
                old_text="Cell A",
            )

        assert "underlying note data" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes_mcp.server.read_note")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_find_replace_ambiguous(
        self,
        mock_backup_class,
        mock_read_note,
        mock_build_uuid,
    ):
        """Should raise ToolError when old_text matches multiple times."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_content = MagicMock()
        mock_content.resolved_text = (
            "# My Note\ntodo item\nAnother todo item\nMore todo item"
        )
        mock_read_note.return_value = mock_content

        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="My Note",
                body="replacement",
                operation="find_replace",
                old_text="todo item",
            )

        assert "3 times" in str(exc_info.value)
        assert "context" in str(exc_info.value).lower()

    def test_find_replace_missing_old_text(self):
        """Should raise ToolError when old_text is not provided for find_replace."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="My Note",
                body="replacement",
                operation="find_replace",
            )

        assert "old_text" in str(exc_info.value)
        assert "required" in str(exc_info.value).lower()

    def test_find_replace_empty_old_text(self):
        """Should raise ToolError when old_text is empty string."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="My Note",
                body="replacement",
                operation="find_replace",
                old_text="",
            )

        assert "old_text" in str(exc_info.value)
        assert "empty" in str(exc_info.value).lower()

    def test_old_text_rejected_for_append(self):
        """Should raise ToolError when old_text is used with append."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="My Note",
                body="Content",
                operation="append",
                old_text="some text",
            )

        assert "old_text" in str(exc_info.value)
        assert "find_replace" in str(exc_info.value)

    def test_old_text_rejected_for_replace(self):
        """Should raise ToolError when old_text is used with replace."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="My Note",
                body="Content",
                operation="replace",
                old_text="some text",
            )

        assert "old_text" in str(exc_info.value)
        assert "find_replace" in str(exc_info.value)

    def test_markdown_is_invalid_operation(self):
        """Should reject markdown as a separate operation (body is always markdown)."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="My Note", body="## Heading", operation="markdown")

        assert "invalid operation" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_accessibility_permission_error(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_uuid,
        mock_execute_append,
        mock_is_gui,
    ):
        """Should raise ToolError with helpful message when accessibility denied."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )

        mock_execute_append.side_effect = AccessibilityPermissionError(
            "Permissions required"
        )

        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="My Note", body="Content", operation="append")

        assert "accessibility" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_clipboard_error(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_uuid,
        mock_execute_append,
        mock_is_gui,
    ):
        """Should raise ToolError when clipboard operations fail."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )

        mock_execute_append.side_effect = ClipboardError("Clipboard failed")

        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="My Note", body="Content", operation="append")

        assert "clipboard" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_ui_scripting_error(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_uuid,
        mock_execute_append,
        mock_is_gui,
    ):
        """Should raise ToolError when UI scripting fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )

        mock_execute_append.side_effect = UIScriptingError("UI scripting failed")

        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="My Note", body="Content", operation="append")

        assert "ui scripting" in str(exc_info.value).lower()

    def test_update_empty_name_raises_error(self):
        """Should raise ToolError for empty note name."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="", body="Content", operation="append")

        assert "empty" in str(exc_info.value).lower()

    def test_update_whitespace_name_raises_error(self):
        """Should raise ToolError for whitespace-only note name."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="   ", body="Content", operation="append")

        assert "empty" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_error_propagates_from_execute(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_uuid,
        mock_execute_append,
        mock_is_gui,
    ):
        """Should propagate errors from execute functions as ToolError."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )

        mock_execute_append.side_effect = UIScriptingError("Failed")

        with pytest.raises(ToolError):
            update_note_content(name="My Note", body="Content", operation="append")

    # --- edit_lines removed ---

    def test_edit_lines_is_invalid_operation(self):
        """edit_lines has been removed; should be rejected as invalid operation."""
        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="My Note",
                body="Content",
                operation="edit_lines",
            )

        assert "invalid operation" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.nudge_icloud_sync", return_value=True)
    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_note_content_with_sync_nudge(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_uuid,
        mock_execute_append,
        mock_is_gui,
        mock_nudge,
    ):
        """Should call nudge and include sync_nudged in response."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"richtext-data", pending_actions=[]
        )

        result = update_note_content(
            name="My Note", body="New content", operation="append"
        )

        assert result["sync_nudged"] is True
        mock_nudge.assert_called_once()


class TestDeleteNote:
    """Tests for the delete_note tool."""

    @pytest.fixture(autouse=True)
    def _stub_delete_guard(self):
        """The #229 guard reads NoteStore; these tests cover the routing."""
        from apple_notes.models import TitleMatch

        match = TitleMatch(1, "U-1", "Old Note", "Notes", 2, None, False, False)
        with (
            patch("apple_notes.write_service.NotesDatabase"),
            patch("apple_notes.write_service.resolve_unique_note", return_value=match),
            patch("apple_notes.write_service.note_preview", return_value="preview"),
        ):
            yield

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_delete_note")
    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_success(
        self,
        mock_backup_class,
        mock_execute_delete,
        mock_is_gui,
    ):
        """Should delete note via direct execution in GUI session."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        result = delete_note(name="Old Note")

        assert result["success"] is True
        assert result["note_name"] == "Old Note"
        assert result["backup_path"] == "/path/to/backup.sqlite"
        mock_execute_delete.assert_called_once_with("Old Note")

    @patch("apple_notes.write_service.is_gui_session", return_value=False)
    @patch("apple_notes.write_service.UIHelperClient")
    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_remote_session(
        self,
        mock_backup_class,
        mock_client_class,
        mock_is_gui,
    ):
        """Should route through UI helper daemon in remote/SSH session."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        result = delete_note(name="Old Note")

        assert result["success"] is True
        mock_client.delete_note.assert_called_once_with("Old Note")

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_delete_note")
    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_not_found(
        self,
        mock_backup_class,
        mock_execute_delete,
        mock_is_gui,
    ):
        """Should raise ToolError when note doesn't exist."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_execute_delete.side_effect = NoteNotFoundError("Note 'X' not found")

        with pytest.raises(ToolError) as exc_info:
            delete_note(name="X")

        assert "not found" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_backup_failure(self, mock_backup_class):
        """Should raise ToolError when backup fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.side_effect = BackupError("Backup failed")
        mock_backup_class.return_value = mock_backup

        with pytest.raises(ToolError) as exc_info:
            delete_note(name="Old Note")

        assert "backup" in str(exc_info.value).lower()

    def test_delete_note_empty_name(self):
        """Should raise ToolError for empty note name."""
        with pytest.raises(ToolError) as exc_info:
            delete_note(name="")

        assert "empty" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_delete_note")
    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_applescript_error(
        self,
        mock_backup_class,
        mock_execute_delete,
        mock_is_gui,
    ):
        """Should raise ToolError when AppleScript fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_execute_delete.side_effect = AppleScriptError("Script failed")

        with pytest.raises(ToolError) as exc_info:
            delete_note(name="Old Note")

        assert "delete" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.is_gui_session", return_value=False)
    @patch("apple_notes.write_service.UIHelperClient")
    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_daemon_unavailable(
        self,
        mock_backup_class,
        mock_client_class,
        mock_is_gui,
    ):
        """Should raise ToolError when daemon is not available."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_client = MagicMock()
        mock_client.delete_note.side_effect = UIHelperNotAvailableError("Not available")
        mock_client_class.return_value = mock_client

        with pytest.raises(ToolError) as exc_info:
            delete_note(name="Old Note")

        assert "daemon" in str(exc_info.value).lower()

    @patch("apple_notes.write_service.is_gui_session", return_value=False)
    @patch("apple_notes.write_service.UIHelperClient")
    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_daemon_timeout_may_still_complete(
        self,
        mock_backup_class,
        mock_client_class,
        mock_is_gui,
    ):
        """A timed-out daemon may still be deleting (#143, #125 NTG-012)."""
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_client_class.return_value.delete_note.side_effect = UIHelperTimeoutError(
            "t/o"
        )

        with pytest.raises(ToolError, match="may still complete") as exc_info:
            delete_note(name="Old Note")

        assert "not available" not in str(exc_info.value)

    @patch("apple_notes.write_service.is_gui_session", return_value=False)
    @patch("apple_notes.write_service.UIHelperClient")
    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_daemon_busy_is_tool_error(
        self,
        mock_backup_class,
        mock_client_class,
        mock_is_gui,
    ):
        """The daemon's queue refusal (#143) reaches the caller verbatim."""
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_client_class.return_value.delete_note.side_effect = EditLockTimeoutError(
            "busy; this one didn't start within 30s"
        )

        with pytest.raises(ToolError, match="didn't start within 30s"):
            delete_note(name="Old Note")

    @patch("apple_notes.write_service.nudge_icloud_sync", return_value=True)
    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_delete_note")
    @patch("apple_notes.write_service.BackupManager")
    def test_delete_note_with_sync_nudge(
        self,
        mock_backup_class,
        mock_execute_delete,
        mock_is_gui,
        mock_nudge,
    ):
        """Should call nudge and include sync_nudged in response."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        result = delete_note(name="Old Note")

        assert result["sync_nudged"] is True
        mock_nudge.assert_called_once()


class TestHashtagRendering:
    """Tests for hashtag attachment rendering in note content."""

    def test_hashtag_renders_as_explicit_tag(self):
        """Hashtag attachments render as {#TagName} so reads round-trip to writes."""
        from apple_notes.note_content import NoteContentReader
        from apple_notes.protobuf_parser import AttachmentInfo

        reader = NoteContentReader.__new__(NoteContentReader)
        note = Note(name="Test", uuid="u1", folder="Notes", pk=1)

        # Text with a single UFFFC placeholder for the hashtag
        note_text = "Hello \ufffc world"

        attachment_infos = [
            AttachmentInfo(
                identifier="hash-1",
                type_uti="com.apple.notes.inlinetextattachment.hashtag",
            )
        ]
        db_attachments = {"hash-1": {"alt_text": "#DailyTodo"}}

        result = reader._build_note_content(
            note=note,
            note_text=note_text,
            attachment_infos=attachment_infos,
            external_link_infos=[],
            checklist_infos=[],
            text_format_infos=[],
            paragraph_style_infos=[],
            db_attachments=db_attachments,
            uuid_lookup={},
        )

        assert "{#DailyTodo}" in result.resolved_text
        assert "[hashtag]" not in result.resolved_text
        # Should still be tracked as an attachment (alt_text stays the raw #name)
        assert len(result.attachments) == 1
        assert result.attachments[0].alt_text == "#DailyTodo"

    def test_hashtag_without_alt_text_falls_back(self):
        """Hashtag without alt_text should fall back to [hashtag]."""
        from apple_notes.note_content import NoteContentReader
        from apple_notes.protobuf_parser import AttachmentInfo

        reader = NoteContentReader.__new__(NoteContentReader)
        note = Note(name="Test", uuid="u1", folder="Notes", pk=1)

        note_text = "Hello \ufffc world"

        attachment_infos = [
            AttachmentInfo(
                identifier="hash-2",
                type_uti="com.apple.notes.inlinetextattachment.hashtag",
            )
        ]
        db_attachments = {"hash-2": {}}

        result = reader._build_note_content(
            note=note,
            note_text=note_text,
            attachment_infos=attachment_infos,
            external_link_infos=[],
            checklist_infos=[],
            text_format_infos=[],
            paragraph_style_infos=[],
            db_attachments=db_attachments,
            uuid_lookup={},
        )

        assert "[hashtag]" in result.resolved_text


class TestToggleChecklistItem:
    """Tests for the toggle_checklist_item tool."""

    @patch("apple_notes_mcp.server.nudge_icloud_sync", return_value=False)
    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_toggle_checklist_item")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_success(
        self,
        mock_backup_class,
        mock_execute_toggle,
        mock_is_gui,
        mock_nudge,
    ):
        """Should toggle checklist item via local execution."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        result = toggle_checklist_item(name="My Note", item_text="Buy groceries")

        assert result["success"] is True
        assert result["note_name"] == "My Note"
        assert result["item_text"] == "Buy groceries"
        assert result["backup_path"] == "/path/to/backup.sqlite"
        assert "sync_nudged" in result
        mock_execute_toggle.assert_called_once_with("My Note", "Buy groceries")

    @patch("apple_notes_mcp.server.nudge_icloud_sync", return_value=False)
    @patch("apple_notes_mcp.server._is_gui_session", return_value=False)
    @patch("apple_notes_mcp.server.UIHelperClient")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_remote_session(
        self,
        mock_backup_class,
        mock_client_class,
        mock_is_gui,
        mock_nudge,
    ):
        """Should route through UI helper daemon for remote sessions."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        result = toggle_checklist_item(name="My Note", item_text="Buy groceries")

        assert result["success"] is True
        mock_client.toggle_checklist_item.assert_called_once_with(
            "My Note", "Buy groceries"
        )

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_toggle_checklist_item")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_not_found(
        self,
        mock_backup_class,
        mock_execute_toggle,
        mock_is_gui,
    ):
        """Should raise ToolError when checklist item not found."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_execute_toggle.side_effect = ValueError(
            "Checklist item 'Nonexistent' not found"
        )

        with pytest.raises(ToolError) as exc_info:
            toggle_checklist_item(name="My Note", item_text="Nonexistent")

        assert "not found" in str(exc_info.value).lower()

    def test_toggle_checklist_item_empty_name(self):
        """Should raise ToolError for empty note name."""
        with pytest.raises(ToolError):
            toggle_checklist_item(name="", item_text="Buy groceries")

    def test_toggle_checklist_item_empty_item_text(self):
        """Should raise ToolError for empty item text."""
        with pytest.raises(ToolError):
            toggle_checklist_item(name="My Note", item_text="")

    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_backup_failure(self, mock_backup_class):
        """Should raise ToolError when backup fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.side_effect = BackupError("Backup failed")
        mock_backup_class.return_value = mock_backup

        with pytest.raises(ToolError) as exc_info:
            toggle_checklist_item(name="My Note", item_text="Buy groceries")

        assert "backup" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_toggle_checklist_item")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_note_not_found(
        self,
        mock_backup_class,
        mock_execute_toggle,
        mock_is_gui,
    ):
        """Should raise ToolError when note doesn't exist."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_execute_toggle.side_effect = NoteNotFoundError("Not found")

        with pytest.raises(ToolError) as exc_info:
            toggle_checklist_item(name="Nonexistent", item_text="Buy groceries")

        assert "not found" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_toggle_checklist_item")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_accessibility_error(
        self,
        mock_backup_class,
        mock_execute_toggle,
        mock_is_gui,
    ):
        """Should raise ToolError when accessibility permissions missing."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_execute_toggle.side_effect = AccessibilityPermissionError(
            "Accessibility not granted"
        )

        with pytest.raises(ToolError) as exc_info:
            toggle_checklist_item(name="My Note", item_text="Buy groceries")

        assert "accessibility" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._is_gui_session", return_value=False)
    @patch("apple_notes_mcp.server.UIHelperClient")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_daemon_unavailable(
        self,
        mock_backup_class,
        mock_client_class,
        mock_is_gui,
    ):
        """Should raise ToolError when daemon not available for remote session."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_client = MagicMock()
        mock_client.toggle_checklist_item.side_effect = UIHelperNotAvailableError(
            "Daemon not running"
        )
        mock_client_class.return_value = mock_client

        with pytest.raises(ToolError) as exc_info:
            toggle_checklist_item(name="My Note", item_text="Buy groceries")

        assert "ui helper daemon" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._is_gui_session", return_value=False)
    @patch("apple_notes_mcp.server.UIHelperClient")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_daemon_timeout_may_still_complete(
        self,
        mock_backup_class,
        mock_client_class,
        mock_is_gui,
    ):
        """A timed-out daemon may still be toggling (#143, #125 NTG-012)."""
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_client_class.return_value.toggle_checklist_item.side_effect = (
            UIHelperTimeoutError("t/o")
        )

        with pytest.raises(ToolError, match="may still complete") as exc_info:
            toggle_checklist_item(name="My Note", item_text="Buy groceries")

        assert "not available" not in str(exc_info.value)

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_toggle_checklist_item")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_parse_error(
        self,
        mock_backup_class,
        mock_execute_toggle,
        mock_is_gui,
    ):
        """Should raise ToolError when richtext parsing fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_execute_toggle.side_effect = NotesRichtextParseError("Bad data")

        with pytest.raises(ToolError) as exc_info:
            toggle_checklist_item(name="My Note", item_text="Buy groceries")

        assert "parse" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_toggle_checklist_item")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_serialize_error(
        self,
        mock_backup_class,
        mock_execute_toggle,
        mock_is_gui,
    ):
        """Should raise ToolError when richtext serialization fails."""
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/path/to/backup.sqlite"
        mock_backup_class.return_value = mock_backup

        mock_execute_toggle.side_effect = NotesRichtextSerializeError(
            "Serialize failed"
        )

        with pytest.raises(ToolError) as exc_info:
            toggle_checklist_item(name="My Note", item_text="Buy groceries")

        assert "serialize" in str(exc_info.value).lower()


class TestGetBacklinksCore:
    """Tests for the get_backlinks core library function."""

    @patch("apple_notes.NotesDatabase")
    def test_returns_backlinks_for_note(self, mock_db_class):
        """Should return notes that link to the target note."""
        mock_db = MagicMock()
        mock_db.get_all_links.return_value = (
            SAMPLE_NOTES,
            [
                GraphLink(
                    source_name="Meeting Notes",
                    target_name="Project Ideas",
                    source_pk=1,
                    target_pk=3,
                ),
                GraphLink(
                    source_name="Shopping List",
                    target_name="Project Ideas",
                    source_pk=2,
                    target_pk=3,
                ),
            ],
        )
        mock_db.get_note_by_name.return_value = SAMPLE_NOTES[2]  # Project Ideas
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)

        result = fetch_backlinks("Project Ideas")

        assert result == ["Meeting Notes", "Shopping List"]

    @patch("apple_notes.NotesDatabase")
    def test_returns_empty_list_when_no_backlinks(self, mock_db_class):
        """Should return empty list when no notes link to the target."""
        mock_db = MagicMock()
        mock_db.get_all_links.return_value = (
            SAMPLE_NOTES,
            [
                GraphLink(
                    source_name="Meeting Notes",
                    target_name="Project Ideas",
                    source_pk=1,
                    target_pk=3,
                ),
            ],
        )
        mock_db.get_note_by_name.return_value = SAMPLE_NOTES[1]  # Shopping List
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)

        result = fetch_backlinks("Shopping List")

        assert result == []

    @patch("apple_notes.NotesDatabase")
    def test_deduplicates_backlinks(self, mock_db_class):
        """Should return unique note names even if multiple links exist."""
        mock_db = MagicMock()
        mock_db.get_all_links.return_value = (
            SAMPLE_NOTES,
            [
                GraphLink(
                    source_name="Meeting Notes",
                    target_name="Project Ideas",
                    source_pk=1,
                    target_pk=3,
                ),
                GraphLink(
                    source_name="Meeting Notes",
                    target_name="Project Ideas",
                    source_pk=1,
                    target_pk=3,
                ),
            ],
        )
        mock_db.get_note_by_name.return_value = SAMPLE_NOTES[2]  # Project Ideas
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)

        result = fetch_backlinks("Project Ideas")

        assert result == ["Meeting Notes"]

    @patch("apple_notes.NotesDatabase")
    def test_returns_sorted_backlinks(self, mock_db_class):
        """Should return backlinks sorted alphabetically."""
        mock_db = MagicMock()
        mock_db.get_all_links.return_value = (
            SAMPLE_NOTES,
            [
                GraphLink(
                    source_name="Shopping List",
                    target_name="Meeting Notes",
                    source_pk=2,
                    target_pk=1,
                ),
                GraphLink(
                    source_name="Project Ideas",
                    target_name="Meeting Notes",
                    source_pk=3,
                    target_pk=1,
                ),
            ],
        )
        mock_db.get_note_by_name.return_value = SAMPLE_NOTES[0]  # Meeting Notes
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)

        result = fetch_backlinks("Meeting Notes")

        assert result == ["Project Ideas", "Shopping List"]

    @patch("apple_notes.NotesDatabase")
    def test_excludes_backlinks_from_filtered_folders(self, mock_db_class):
        """Should exclude backlinks from notes in excluded folders."""
        archive_note = Note(
            name="Archived Note",
            uuid="uuid-4",
            folder="Other/Archive",
            pk=4,
            created=None,
            modified=None,
            last_opened=None,
        )
        mock_db = MagicMock()
        mock_db.get_all_links.return_value = (
            SAMPLE_NOTES + [archive_note],
            [
                GraphLink(
                    source_name="Meeting Notes",
                    target_name="Project Ideas",
                    source_pk=1,
                    target_pk=3,
                ),
                GraphLink(
                    source_name="Archived Note",
                    target_name="Project Ideas",
                    source_pk=4,
                    target_pk=3,
                ),
            ],
        )
        mock_db.get_note_by_name.return_value = SAMPLE_NOTES[2]  # Project Ideas
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)

        result = fetch_backlinks("Project Ideas", exclude_folders=["Archive"])

        assert result == ["Meeting Notes"]
        assert "Archived Note" not in result

    @patch("apple_notes.NotesDatabase")
    def test_raises_note_not_found(self, mock_db_class):
        """Should raise NoteNotFoundError when note doesn't exist."""
        mock_db = MagicMock()
        mock_db.get_note_by_name.side_effect = NoteNotFoundError(
            "Note 'Nonexistent' not found"
        )
        mock_db_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_db_class.return_value.__exit__ = MagicMock(return_value=None)

        with pytest.raises(NoteNotFoundError):
            fetch_backlinks("Nonexistent")


class TestGetBacklinks:
    """Tests for the get_backlinks MCP tool."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.fetch_backlinks")
    def test_get_backlinks_success(self, mock_fetch, mock_get_filters):
        """Should return backlinks with count and pass effective filters."""
        mock_get_filters.return_value = (["Archive"], None)
        mock_fetch.return_value = ["Meeting Notes", "Shopping List"]

        result = get_backlinks("Project Ideas")

        assert result["name"] == "Project Ideas"
        assert result["backlinks"] == ["Meeting Notes", "Shopping List"]
        assert result["count"] == 2
        mock_fetch.assert_called_once_with(
            "Project Ideas", exclude_folders=["Archive"], include_folders=None, note_pk=1
        )

    @patch("apple_notes_mcp.server.fetch_backlinks")
    def test_get_backlinks_empty(self, mock_fetch):
        """Should return empty list and count=0 when no backlinks."""
        mock_fetch.return_value = []

        result = get_backlinks("Shopping List")

        assert result["name"] == "Shopping List"
        assert result["backlinks"] == []
        assert result["count"] == 0

    @patch("apple_notes_mcp.server.fetch_backlinks")
    def test_get_backlinks_note_not_found(self, mock_fetch):
        """Should raise ToolError when note doesn't exist."""
        mock_fetch.side_effect = NoteNotFoundError("Note 'Nonexistent' not found")

        with pytest.raises(ToolError) as exc_info:
            get_backlinks("Nonexistent")

        assert "Nonexistent" in str(exc_info.value)

    @patch("apple_notes_mcp.server.fetch_backlinks")
    def test_get_backlinks_database_not_found(self, mock_fetch):
        """Should raise ToolError when database is not found."""
        mock_fetch.side_effect = DatabaseNotFoundError("Database not found")

        with pytest.raises(ToolError) as exc_info:
            get_backlinks("Some Note")

        assert "database not found" in str(exc_info.value).lower()


SESSION_LOCKED_MSG = (
    "macOS GUI session is unavailable for UI scripting (locked, screensaver, "
    "non-console, or pre-login). Unlock the Mac at the console and retry."
)


class TestSessionLockedToolErrorTranslation:
    """Every write tool must surface SessionLockedError as a ToolError that
    carries the preflight's message verbatim — not the generic
    'UI scripting failed: …' wrapper. Otherwise the caller sees the message
    that misled us into this bug in the first place."""

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_session_locked(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        mock_backup = MagicMock()
        mock_backup.create_backup.return_value = "/backup.sqlite"
        mock_backup_class.return_value = mock_backup
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"rt", pending_actions=[]
        )
        mock_create_resolver.return_value = MagicMock()
        mock_execute_create.side_effect = SessionLockedError(SESSION_LOCKED_MSG)

        with pytest.raises(ToolError) as exc_info:
            add_note(name="Note", body="body")

        message = str(exc_info.value)
        assert SESSION_LOCKED_MSG in message
        assert "UI scripting failed" not in message

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_note_append_session_locked(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_map,
        mock_execute_append,
        mock_is_gui,
    ):
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"rt", pending_actions=[]
        )
        mock_execute_append.side_effect = SessionLockedError(SESSION_LOCKED_MSG)

        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="N", body="x", operation="append")

        message = str(exc_info.value)
        assert SESSION_LOCKED_MSG in message
        assert "UI scripting failed" not in message

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_replace_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_note_replace_session_locked(
        self,
        mock_backup_class,
        mock_build_map,
        mock_execute_replace,
        mock_is_gui,
    ):
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_execute_replace.side_effect = SessionLockedError(SESSION_LOCKED_MSG)

        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="N", body="x", operation="replace")

        assert SESSION_LOCKED_MSG in str(exc_info.value)
        assert "UI scripting failed" not in str(exc_info.value)

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_find_replace_multiline")
    @patch("apple_notes_mcp.server.read_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_note_find_replace_session_locked(
        self,
        mock_backup_class,
        mock_build_map,
        mock_read_note,
        mock_execute_fr,
        mock_is_gui,
    ):
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        # Pre-check requires old_text to appear exactly once in both resolved
        # and raw body, so build content that satisfies both.
        content = MagicMock()
        content.resolved_text = "Title\nsome old text here"
        content.raw_text = "Title\nsome old text here"
        mock_read_note.return_value = content
        mock_execute_fr.side_effect = SessionLockedError(SESSION_LOCKED_MSG)

        with pytest.raises(ToolError) as exc_info:
            update_note_content(
                name="N",
                body="new",
                operation="find_replace",
                old_text="old text",
            )

        assert SESSION_LOCKED_MSG in str(exc_info.value)
        assert "UI scripting failed" not in str(exc_info.value)

    # delete_note has no session-lock case: it uses pure AppleScript and
    # works over Apple Events with the GUI locked, so the preflight is
    # never invoked along that path.

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_toggle_checklist_item")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_toggle_checklist_item_session_locked(
        self,
        mock_backup_class,
        mock_execute_toggle,
        mock_is_gui,
    ):
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_execute_toggle.side_effect = SessionLockedError(SESSION_LOCKED_MSG)

        with pytest.raises(ToolError) as exc_info:
            toggle_checklist_item(name="N", item_text="item")

        assert SESSION_LOCKED_MSG in str(exc_info.value)
        assert "UI scripting failed" not in str(exc_info.value)

    @patch("apple_notes_mcp.server._is_gui_session", return_value=False)
    @patch("apple_notes_mcp.server.UIHelperClient")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_session_locked_via_daemon(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_client_class,
        mock_is_gui,
    ):
        """Remote SSH path: SessionLockedError propagates from the daemon
        through UIHelperClient and still lands in the specific ToolError
        branch — not the 'UI scripting failed' wrapper."""
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"rt", pending_actions=[]
        )
        mock_create_resolver.return_value = MagicMock()
        mock_client = MagicMock()
        mock_client.create_note.side_effect = SessionLockedError(SESSION_LOCKED_MSG)
        mock_client_class.return_value = mock_client

        with pytest.raises(ToolError) as exc_info:
            add_note(name="N", body="body")

        assert SESSION_LOCKED_MSG in str(exc_info.value)
        assert "UI scripting failed" not in str(exc_info.value)


NOT_FRONTMOST_MSG = (
    "Aborted UI scripting: Apple Notes is not frontmost "
    "(frontmost app: 'Ghostty'). Refusing to send keystrokes to another app."
)
WRITE_UNVERIFIED_MSG = (
    "add_note reported no error but note 'N' does not exist afterward — "
    "the write did not take effect."
)


class TestFocusGuardToolErrorTranslation:
    """The focus-guard abort and verify-after-write failure must surface as a
    ToolError carrying the message verbatim — not the generic 'UI scripting
    failed: …' wrapper — for both add_note and update_note_content, local and
    remote. These are the messages that tell the caller the write was refused
    (safety) or didn't take (no false success), so they must not be swallowed."""

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_not_frontmost(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"rt", pending_actions=[]
        )
        mock_create_resolver.return_value = MagicMock()
        mock_execute_create.side_effect = NotesNotFrontmostError(NOT_FRONTMOST_MSG)

        with pytest.raises(ToolError) as exc_info:
            add_note(name="Note", body="body")

        message = str(exc_info.value)
        assert NOT_FRONTMOST_MSG in message
        assert "UI scripting failed" not in message

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_create_note")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_write_unverified(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_execute_create,
        mock_is_gui,
    ):
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"rt", pending_actions=[]
        )
        mock_create_resolver.return_value = MagicMock()
        mock_execute_create.side_effect = WriteVerificationError(WRITE_UNVERIFIED_MSG)

        with pytest.raises(ToolError) as exc_info:
            add_note(name="N", body="body")

        message = str(exc_info.value)
        assert WRITE_UNVERIFIED_MSG in message
        assert "UI scripting failed" not in message

    @patch("apple_notes.write_service.is_gui_session", return_value=True)
    @patch("apple_notes.write_service.execute_append_to_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes.write_service.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_note_append_not_frontmost(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_build_map,
        mock_execute_append,
        mock_is_gui,
    ):
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"rt", pending_actions=[]
        )
        mock_execute_append.side_effect = NotesNotFrontmostError(NOT_FRONTMOST_MSG)

        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="N", body="x", operation="append")

        message = str(exc_info.value)
        assert NOT_FRONTMOST_MSG in message
        assert "UI scripting failed" not in message

    @patch("apple_notes_mcp.server._is_gui_session", return_value=True)
    @patch("apple_notes_mcp.server.execute_replace_note")
    @patch("apple_notes_mcp.server._build_name_to_uuid", return_value={})
    @patch("apple_notes_mcp.server.BackupManager")
    def test_update_note_replace_write_unverified(
        self,
        mock_backup_class,
        mock_build_map,
        mock_execute_replace,
        mock_is_gui,
    ):
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        msg = "update_note_content(replace) reported no error but note 'N' is unchanged"
        mock_execute_replace.side_effect = WriteVerificationError(msg)

        with pytest.raises(ToolError) as exc_info:
            update_note_content(name="N", body="x", operation="replace")

        assert msg in str(exc_info.value)
        assert "UI scripting failed" not in str(exc_info.value)

    @patch("apple_notes_mcp.server._is_gui_session", return_value=False)
    @patch("apple_notes_mcp.server.UIHelperClient")
    @patch("apple_notes_mcp.server._link_targets", return_value={})
    @patch("apple_notes_mcp.server.markdown_to_richtext_new_linked")
    @patch("apple_notes_mcp.server.BackupManager")
    def test_add_note_not_frontmost_via_daemon(
        self,
        mock_backup_class,
        mock_md_convert,
        mock_create_resolver,
        mock_client_class,
        mock_is_gui,
    ):
        """Remote SSH path: NotesNotFrontmostError propagates from the daemon
        through UIHelperClient and still lands in the verbatim branch."""
        mock_backup_class.return_value.create_backup.return_value = "/b.sqlite"
        mock_md_convert.return_value = RichtextConversionResult(
            data=b"rt", pending_actions=[]
        )
        mock_create_resolver.return_value = MagicMock()
        mock_client = MagicMock()
        mock_client.create_note.side_effect = NotesNotFrontmostError(NOT_FRONTMOST_MSG)
        mock_client_class.return_value = mock_client

        with pytest.raises(ToolError) as exc_info:
            add_note(name="N", body="body")

        assert NOT_FRONTMOST_MSG in str(exc_info.value)
        assert "UI scripting failed" not in str(exc_info.value)


class TestExportNote:
    """Tests for the export_note tool (issue #79)."""

    @patch("apple_notes_mcp.server.read_note")
    def test_export_note_writes_markdown_file(self, mock_read_note, tmp_path):
        """Should write the note as a Markdown file in the output directory."""
        mock_read_note.return_value = SAMPLE_NOTE_CONTENT

        result = export_note("Meeting Notes", output_dir=str(tmp_path))

        assert result["success"] is True
        assert result["note_name"] == "Meeting Notes"
        exported = tmp_path / "Meeting Notes.md"
        assert result["path"] == str(exported)
        content = exported.read_text()
        assert content.startswith("# Meeting Notes\n")
        assert "[[Project Ideas]]" in content

    @patch("apple_notes_mcp.server.read_note")
    def test_export_note_creates_output_dir(self, mock_read_note, tmp_path):
        """Should create the output directory if it does not exist."""
        mock_read_note.return_value = SAMPLE_NOTE_CONTENT

        result = export_note("Meeting Notes", output_dir=str(tmp_path / "new" / "dir"))

        assert (tmp_path / "new" / "dir" / "Meeting Notes.md").exists()
        assert result["success"] is True

    @patch("apple_notes_mcp.server.read_note")
    def test_export_note_sanitizes_filename(self, mock_read_note, tmp_path):
        """Note names with path separators should produce safe filenames."""
        content = NoteContent(
            name="Home/Garden: Plan",
            uuid="uuid-9",
            folder="Personal",
            raw_text="Home/Garden: Plan\nbody",
            resolved_text="Home/Garden: Plan\nbody",
            note_links=[],
            external_links=[],
            attachments=[],
            created=None,
            modified=None,
            last_opened=None,
        )
        mock_read_note.return_value = content

        result = export_note("Home/Garden: Plan", output_dir=str(tmp_path))

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].suffix == ".md"
        assert "/" not in files[0].stem and ":" not in files[0].stem
        assert result["path"] == str(files[0])

    def test_export_note_invalid_format(self, tmp_path):
        """Should reject unsupported formats."""
        with pytest.raises(ToolError) as exc_info:
            export_note("Meeting Notes", output_dir=str(tmp_path), format="pdf")

        assert "format" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.read_note")
    def test_export_note_not_found(self, mock_read_note, tmp_path):
        """Should raise ToolError when the note does not exist."""
        mock_read_note.side_effect = NoteNotFoundError("nope")

        with pytest.raises(ToolError) as exc_info:
            export_note("Missing", output_dir=str(tmp_path))

        assert "not found" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.read_note")
    def test_export_note_encrypted(self, mock_read_note, tmp_path):
        """Should raise ToolError for encrypted notes."""
        mock_read_note.side_effect = EncryptedNoteError("locked")

        with pytest.raises(ToolError) as exc_info:
            export_note("Secret", output_dir=str(tmp_path))

        assert "encrypted" in str(exc_info.value).lower()


class TestExportFolder:
    """Tests for the export_folder tool (issue #79)."""

    def _reader_with(self, mock_reader_class, notes, contents):
        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = notes
        mock_reader.get_note_content_for_note.side_effect = lambda note: contents[
            note.name
        ]
        mock_reader_class.return_value = mock_reader
        return mock_reader

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_export_folder_writes_all_notes(self, mock_reader_class, tmp_path):
        """Should export every note whose folder matches (case-insensitive)."""
        contents = {
            "Meeting Notes": SAMPLE_NOTE_CONTENT,
        }
        self._reader_with(mock_reader_class, SAMPLE_NOTES, contents)

        result = export_folder("work", output_dir=str(tmp_path))

        assert result["success"] is True
        assert result["count"] == 1
        assert (tmp_path / "Meeting Notes.md").exists()
        assert result["exported"][0]["note_name"] == "Meeting Notes"

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_export_folder_no_matching_folder(self, mock_reader_class, tmp_path):
        """Should raise ToolError when no notes match the folder."""
        self._reader_with(mock_reader_class, SAMPLE_NOTES, {})

        with pytest.raises(ToolError) as exc_info:
            export_folder("Nonexistent", output_dir=str(tmp_path))

        assert "no notes found" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_export_folder_skips_unreadable_notes(self, mock_reader_class, tmp_path):
        """Encrypted/unparseable notes are skipped and reported, not fatal."""
        notes = [
            Note(
                name="Readable",
                uuid="u1",
                folder="Vault",
                pk=1,
                created=None,
                modified=None,
                last_opened=None,
            ),
            Note(
                name="Locked",
                uuid="u2",
                folder="Vault",
                pk=2,
                created=None,
                modified=None,
                last_opened=None,
            ),
        ]
        readable = NoteContent(
            name="Readable",
            uuid="u1",
            folder="Vault",
            raw_text="Readable\nbody",
            resolved_text="Readable\nbody",
            note_links=[],
            external_links=[],
            attachments=[],
            created=None,
            modified=None,
            last_opened=None,
        )

        def get_content(note):
            if note.name == "Locked":
                raise EncryptedNoteError("locked")
            return readable

        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = notes
        mock_reader.get_note_content_for_note.side_effect = get_content
        mock_reader_class.return_value = mock_reader

        result = export_folder("Vault", output_dir=str(tmp_path))

        assert result["count"] == 1
        assert (tmp_path / "Readable.md").exists()
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["note_name"] == "Locked"

    @patch("apple_notes_mcp.server.NoteContentReader")
    def test_export_folder_duplicate_names_get_suffix(
        self, mock_reader_class, tmp_path
    ):
        """Two notes with the same title must each export their OWN content.

        Content must be fetched by note identity (pk), not by name — a
        by-name fetch resolves to the first DB match and silently exports
        the first note's body twice (review finding, 2026-07-02).
        """
        notes = [
            Note(
                name="Ideas",
                uuid="u1",
                folder="Box",
                pk=1,
                created=None,
                modified=None,
                last_opened=None,
            ),
            Note(
                name="Ideas",
                uuid="u2",
                folder="Box",
                pk=2,
                created=None,
                modified=None,
                last_opened=None,
            ),
        ]

        def content_for(pk):
            return NoteContent(
                name="Ideas",
                uuid=f"u{pk}",
                folder="Box",
                raw_text=f"Ideas\nbody of note {pk}",
                resolved_text=f"Ideas\nbody of note {pk}",
                note_links=[],
                external_links=[],
                attachments=[],
                created=None,
                modified=None,
                last_opened=None,
            )

        mock_reader = MagicMock()
        mock_reader.get_all_notes.return_value = notes
        # By-name lookup always resolves to the FIRST match, like the real DB
        mock_reader.get_note_content.side_effect = lambda name: content_for(1)
        # Identity-based lookup returns each note's own content
        mock_reader.get_note_content_for_note.side_effect = lambda note: content_for(
            note.pk
        )
        mock_reader_class.return_value = mock_reader

        result = export_folder("Box", output_dir=str(tmp_path))

        assert result["count"] == 2
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["Ideas (2).md", "Ideas.md"]
        bodies = {(tmp_path / f).read_text() for f in files}
        assert bodies == {
            "# Ideas\n\nbody of note 1\n",
            "# Ideas\n\nbody of note 2\n",
        }

    def test_export_folder_invalid_format(self, tmp_path):
        """Should reject unsupported formats."""
        with pytest.raises(ToolError) as exc_info:
            export_folder("Work", output_dir=str(tmp_path), format="html")

        assert "format" in str(exc_info.value).lower()


class TestExportGraph:
    """Tests for the export_graph tool (issue #79)."""

    GRAPH = {
        "notes": [
            {"name": "Note A", "folder": "Work"},
            {"name": "Note B", "folder": "Personal"},
        ],
        "links": [{"source": "Note A", "target": "Note B"}],
        "stats": {"total_notes": 2, "total_links": 1},
    }

    @patch("apple_notes_mcp.server.get_effective_filters", return_value=(None, None))
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_export_graph_json(self, mock_get_graph, mock_filters, tmp_path):
        """Default format writes a D3-compatible JSON file."""
        mock_get_graph.return_value = self.GRAPH

        result = export_graph(output_dir=str(tmp_path))

        exported = tmp_path / "notes-graph.json"
        assert result["path"] == str(exported)
        data = json.loads(exported.read_text())
        assert data["nodes"][0]["id"] == "Note A"
        assert data["links"] == [{"source": "Note A", "target": "Note B"}]
        assert result["stats"]["total_notes"] == 2

    @patch("apple_notes_mcp.server.get_effective_filters", return_value=(None, None))
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_export_graph_dot(self, mock_get_graph, mock_filters, tmp_path):
        """DOT format writes a GraphViz digraph."""
        mock_get_graph.return_value = self.GRAPH

        result = export_graph(output_dir=str(tmp_path), format="dot")

        exported = tmp_path / "notes-graph.dot"
        assert result["path"] == str(exported)
        dot = exported.read_text()
        assert dot.startswith("digraph notes {")
        assert '"Note A" -> "Note B";' in dot

    @patch("apple_notes_mcp.server.get_effective_filters", return_value=(None, None))
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_export_graph_csv(self, mock_get_graph, mock_filters, tmp_path):
        """CSV format writes an edge list."""
        mock_get_graph.return_value = self.GRAPH

        result = export_graph(output_dir=str(tmp_path), format="csv")

        exported = tmp_path / "notes-graph.csv"
        lines = exported.read_text().splitlines()
        assert lines[0] == "source,target"
        assert lines[1] == "Note A,Note B"
        assert result["path"] == str(exported)

    def test_export_graph_invalid_format(self, tmp_path):
        """Should reject unsupported formats."""
        with pytest.raises(ToolError) as exc_info:
            export_graph(output_dir=str(tmp_path), format="graphml")

        assert "format" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.get_effective_filters", return_value=(None, None))
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_export_graph_passes_filters(self, mock_get_graph, mock_filters, tmp_path):
        """Folder filters resolve via config and pass through to get_graph_data."""
        mock_get_graph.return_value = self.GRAPH

        export_graph(output_dir=str(tmp_path), exclude_folders=["Archive"])

        mock_filters.assert_called_once_with(
            exclude_folders=["Archive"],
            include_folders=None,
            include_all=False,
        )
        mock_get_graph.assert_called_once_with(
            exclude_folders=None,
            include_folders=None,
            format="full",
        )

    @patch("apple_notes_mcp.server.get_effective_filters", return_value=(None, None))
    @patch("apple_notes_mcp.server.get_graph_data")
    def test_export_graph_database_not_found(
        self, mock_get_graph, mock_filters, tmp_path
    ):
        """Should raise ToolError when database is not found."""
        mock_get_graph.side_effect = DatabaseNotFoundError("missing")

        with pytest.raises(ToolError) as exc_info:
            export_graph(output_dir=str(tmp_path))

        assert "database not found" in str(exc_info.value).lower()


class TestFindOrphansTool:
    """Tests for the find_orphans MCP tool."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_orphans")
    def test_passes_effective_filters(self, mock_get_orphans, mock_get_filters):
        mock_get_filters.return_value = (["Archive"], ["Work"])
        mock_get_orphans.return_value = {"orphans": [], "count": 0, "total_notes": 0}

        result = find_orphans(exclude_folders=["Archive"], include_folders=["Work"])

        mock_get_filters.assert_called_once_with(
            exclude_folders=["Archive"], include_folders=["Work"], include_all=False
        )
        mock_get_orphans.assert_called_once_with(
            exclude_folders=["Archive"], include_folders=["Work"]
        )
        assert result == {"orphans": [], "count": 0, "total_notes": 0}

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_orphans")
    def test_database_not_found(self, mock_get_orphans, mock_get_filters):
        mock_get_filters.return_value = (None, None)
        mock_get_orphans.side_effect = DatabaseNotFoundError("nope")

        with pytest.raises(ToolError) as exc_info:
            find_orphans()

        assert "full disk access" in str(exc_info.value).lower()


class TestFindBrokenLinksTool:
    """Tests for the find_broken_links MCP tool."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_broken_links")
    def test_passes_effective_filters(self, mock_get_broken, mock_get_filters):
        mock_get_filters.return_value = (["Archive"], None)
        mock_get_broken.return_value = {"broken_links": [], "count": 0}

        result = find_broken_links()

        mock_get_broken.assert_called_once_with(
            exclude_folders=["Archive"], include_folders=None
        )
        assert result == {"broken_links": [], "count": 0}

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_broken_links")
    def test_database_not_found(self, mock_get_broken, mock_get_filters):
        mock_get_filters.return_value = (None, None)
        mock_get_broken.side_effect = DatabaseNotFoundError("nope")

        with pytest.raises(ToolError):
            find_broken_links()


class TestNoteHealthCheckTool:
    """Tests for the note_health_check MCP tool."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_health_report")
    def test_forwards_params(self, mock_report, mock_get_filters):
        mock_get_filters.return_value = (["Archive"], None)
        mock_report.return_value = {"total_notes": 0}

        note_health_check(stale_threshold_days=90, top_n=5)

        mock_report.assert_called_once_with(
            stale_threshold_days=90,
            top_n=5,
            exclude_folders=["Archive"],
            include_folders=None,
        )

    def test_rejects_negative_threshold(self):
        with pytest.raises(ToolError):
            note_health_check(stale_threshold_days=-1)

    def test_rejects_non_positive_top_n(self):
        with pytest.raises(ToolError):
            note_health_check(top_n=0)

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_health_report")
    def test_database_not_found(self, mock_report, mock_get_filters):
        mock_get_filters.return_value = (None, None)
        mock_report.side_effect = DatabaseNotFoundError("nope")

        with pytest.raises(ToolError):
            note_health_check()


class TestSuggestLinksTool:
    """Tests for the suggest_links MCP tool."""

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_link_suggestions")
    def test_forwards_params(self, mock_suggest, mock_get_filters):
        mock_get_filters.return_value = (["Archive"], None)
        mock_suggest.return_value = {"note": {}, "candidates": []}

        result = suggest_links("My Note", max_candidates=10)

        mock_suggest.assert_called_once_with(
            "My Note",
            max_candidates=10,
            exclude_folders=["Archive"],
            include_folders=None,
            note_pk=1,
        )
        assert result == {"note": {}, "candidates": [], "other_matches": []}

    def test_rejects_non_positive_max_candidates(self):
        with pytest.raises(ToolError):
            suggest_links("My Note", max_candidates=0)

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_link_suggestions")
    def test_note_not_found_maps_to_tool_error(self, mock_suggest, mock_get_filters):
        mock_get_filters.return_value = (None, None)
        mock_suggest.side_effect = NoteNotFoundError("nope")

        with pytest.raises(ToolError) as exc_info:
            suggest_links("Ghost")

        assert "list_notes" in str(exc_info.value)

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_link_suggestions")
    def test_encrypted_subject_maps_to_tool_error(self, mock_suggest, mock_get_filters):
        mock_get_filters.return_value = (None, None)
        mock_suggest.side_effect = EncryptedNoteError("locked")

        with pytest.raises(ToolError) as exc_info:
            suggest_links("Locked Note")

        assert "encrypted" in str(exc_info.value).lower()

    @patch("apple_notes_mcp.server.get_effective_filters")
    @patch("apple_notes_mcp.server.get_link_suggestions")
    def test_database_not_found(self, mock_suggest, mock_get_filters):
        mock_get_filters.return_value = (None, None)
        mock_suggest.side_effect = DatabaseNotFoundError("nope")

        with pytest.raises(ToolError):
            suggest_links("My Note")
