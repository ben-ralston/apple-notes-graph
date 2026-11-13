"""
Apple Notes Content Library

Read Apple Notes content with properly resolved links.

Usage:
    from apple_notes import read_note, NoteContentReader

    # Quick usage
    content = read_note("My Note Title")
    print(content.resolved_text)

    # Full API
    reader = NoteContentReader()
    content = reader.get_note_content("My Note Title")
    print(content.resolved_text)      # Text with [[links]] resolved
    print(content.note_links)         # List of internal note links
    print(content.external_links)     # List of external URLs
    print(content.attachments)        # List of attachments (images, tables, etc.)
"""

from typing import Optional

from .note_content import NoteContentReader
from .database import NotesDatabase
from .backup import BackupManager
from .applescript import AppleScriptExecutor
from .link_converter import LinkConverter
from .clipboard import ClipboardManager
from .ui_scripting import UIScriptingExecutor
from .models import (
    Note,
    NoteContent,
    NoteLink,
    ExternalLink,
    Attachment,
    AttachmentType,
    GraphLink,
    RawNoteLink,
    BrokenLink,
)
from .exceptions import (
    AppleNotesError,
    NoteNotFoundError,
    DatabaseNotFoundError,
    EncryptedNoteError,
    ProtobufParseError,
    AppleScriptError,
    UIScriptingError,
    AccessibilityPermissionError,
    SessionLockedError,
    NotesNotFrontmostError,
    NotesModalOpenError,
    WriteVerificationError,
    ClipboardError,
    FolderNotFoundError,
    BackupError,
    InvalidGraphParameterError,
    UIHelperNotAvailableError,
)
from .filters import filter_notes, filter_folders, matches_folder_filter
from .config import load_config, get_effective_filters
from .librarian import (
    get_orphans,
    get_broken_links,
    get_health_report,
    get_link_suggestions,
    resolve_note_links,
)

__all__ = [
    # Main API
    "read_note",
    "get_graph_data",
    "get_backlinks",
    # Librarian (notes hygiene analytics)
    "get_orphans",
    "get_broken_links",
    "get_health_report",
    "get_link_suggestions",
    "resolve_note_links",
    "NoteContentReader",
    "NotesDatabase",
    # Filtering
    "filter_notes",
    "filter_folders",
    "matches_folder_filter",
    # Config
    "load_config",
    "get_effective_filters",
    # Write operations
    "BackupManager",
    "AppleScriptExecutor",
    "LinkConverter",
    "ClipboardManager",
    "UIScriptingExecutor",
    # Models
    "Note",
    "NoteContent",
    "NoteLink",
    "ExternalLink",
    "Attachment",
    "AttachmentType",
    "GraphLink",
    "RawNoteLink",
    "BrokenLink",
    # Exceptions
    "AppleNotesError",
    "NoteNotFoundError",
    "DatabaseNotFoundError",
    "EncryptedNoteError",
    "ProtobufParseError",
    "AppleScriptError",
    "UIScriptingError",
    "AccessibilityPermissionError",
    "SessionLockedError",
    "NotesNotFrontmostError",
    "NotesModalOpenError",
    "WriteVerificationError",
    "ClipboardError",
    "FolderNotFoundError",
    "BackupError",
    "InvalidGraphParameterError",
    "UIHelperNotAvailableError",
]


def read_note(
    note_name: str, db_path: Optional[str] = None, *, note: Optional[Note] = None
) -> NoteContent:
    """
    Quick function to read a single note with resolved links.

    Args:
        note_name: The title of the note to read
        db_path: Optional path to the SQLite database

    Returns:
        NoteContent with resolved_text containing [[Note Name]] links
        and [text](url) external links

    Raises:
        NoteNotFoundError: If note doesn't exist
        DatabaseNotFoundError: If database not found

    Example:
        content = read_note("My Note")
        print(content.resolved_text)

    ``note``: an already-resolved Note to read by pk instead of by title
    (duplicate-title safe, #83).
    """
    reader = NoteContentReader(db_path)
    if note is not None:
        return reader.get_note_content_for_note(note)
    return reader.get_note_content(note_name)


def get_backlinks(
    note_name: str,
    db_path: Optional[str] = None,
    exclude_folders: Optional[list[str]] = None,
    include_folders: Optional[list[str]] = None,
    note_pk: Optional[int] = None,
) -> list[str]:
    """
    Get notes that link to the given note (backlinks / "what links here?").

    With ``note_pk`` the target is that exact note (links are matched by
    target pk, so a same-titled note's backlinks never mix in, #83);
    otherwise it is looked up by title.

    Fetches all links and filters for those targeting the given note.
    Source notes in excluded folders are filtered out.

    Args:
        note_name: The title of the note to find backlinks for
        db_path: Optional path to the SQLite database
        exclude_folders: Folder patterns to exclude from backlink sources
        include_folders: Only include backlinks from these folders (if set)

    Returns:
        Sorted list of unique note names that link to the given note

    Raises:
        NoteNotFoundError: If the note doesn't exist
    """
    with NotesDatabase(db_path) as db:
        if note_pk is None:
            db.get_note_by_name(note_name)  # Validate note exists
        notes, links = db.get_all_links()

    # Filter source notes by folder exclusions
    filtered_notes = filter_notes(notes, exclude_folders, include_folders)

    if note_pk is not None:
        filtered_pks = {n.pk for n in filtered_notes}
        return sorted(
            set(
                link.source_name
                for link in links
                if link.target_pk == note_pk and link.source_pk in filtered_pks
            )
        )

    filtered_names = {n.name for n in filtered_notes}
    return sorted(
        set(
            link.source_name
            for link in links
            if link.target_name == note_name and link.source_name in filtered_names
        )
    )


def _bfs_subgraph(
    notes: list[Note],
    links: list[GraphLink],
    start_note_name: str,
    max_depth: Optional[int],
) -> tuple[list[Note], list[GraphLink]]:
    """
    Extract subgraph reachable from start_note within depth limit.

    Uses bidirectional BFS (treats links as undirected edges for traversal).

    Args:
        notes: All notes (after folder filtering)
        links: All links between filtered notes
        start_note_name: Name of the starting note
        max_depth: Maximum BFS depth (None = unlimited)

    Returns:
        Tuple of (notes in subgraph, links in subgraph)

    Raises:
        NoteNotFoundError: If start_note doesn't exist
    """
    note_names = {n.name for n in notes}

    if start_note_name not in note_names:
        raise NoteNotFoundError(f"Start note '{start_note_name}' not found")

    # Build bidirectional adjacency list (links are undirected for traversal)
    adjacency: dict[str, set[str]] = {name: set() for name in note_names}
    for link in links:
        adjacency[link.source_name].add(link.target_name)
        adjacency[link.target_name].add(link.source_name)

    # BFS traversal
    visited: set[str] = {start_note_name}
    current_level: set[str] = {start_note_name}
    current_depth = 0

    while current_level and (max_depth is None or current_depth < max_depth):
        next_level: set[str] = set()
        for node in current_level:
            for neighbor in adjacency[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_level.add(neighbor)
        current_level = next_level
        current_depth += 1

    # Filter notes and links to subgraph
    subgraph_notes = [n for n in notes if n.name in visited]
    subgraph_links = [
        l for l in links if l.source_name in visited and l.target_name in visited
    ]

    return subgraph_notes, subgraph_links


VALID_FORMATS = {"full", "links_only"}


def _format_graph_output(
    notes: list[Note],
    links: list[GraphLink],
    format: str,
) -> dict:
    """
    Transform graph data into the requested output format.

    Args:
        notes: Notes to include in output
        links: Links to include in output
        format: Output format ("full" or "links_only")

    Returns:
        Dictionary in the requested format

    Raises:
        InvalidGraphParameterError: If format is not valid
    """
    if format not in VALID_FORMATS:
        raise InvalidGraphParameterError(
            f"Invalid format '{format}'. Must be one of: {VALID_FORMATS}"
        )

    if format == "links_only":
        return {
            "links": [
                {"source": l.source_name, "target": l.target_name} for l in links
            ],
            "stats": {"total_links": len(links)},
        }

    # Default: "full" format
    return {
        "notes": [{"name": n.name, "folder": n.folder} for n in notes],
        "links": [{"source": l.source_name, "target": l.target_name} for l in links],
        "stats": {
            "total_notes": len(notes),
            "total_links": len(links),
        },
    }


def get_graph_data(
    db_path: Optional[str] = None,
    exclude_folders: Optional[list[str]] = None,
    include_folders: Optional[list[str]] = None,
    start_note: Optional[str] = None,
    depth: Optional[int] = None,
    format: str = "full",
) -> dict:
    """
    Get notes and links for graph visualization.

    This is optimized for graph rendering - it queries links directly from
    the database without parsing protobuf content, making it much faster.

    Args:
        db_path: Optional path to the SQLite database
        exclude_folders: Folder patterns to exclude (case-insensitive substring match)
        include_folders: Only include notes matching these patterns (if set)
        start_note: Starting note name for BFS traversal (optional)
        depth: Maximum traversal depth from start_note (optional, requires start_note)
        format: Output format - "full" or "links_only" (default: "full")

    Returns:
        Dictionary with graph data in the requested format

    Raises:
        NoteNotFoundError: If start_note doesn't exist
        InvalidGraphParameterError: If depth is provided without start_note

    Example:
        # Full graph
        data = get_graph_data()

        # BFS traversal from a starting note
        data = get_graph_data(start_note="Home", depth=2)

        # Links only for smaller payload
        data = get_graph_data(format="links_only")
    """
    # Validate parameters
    if depth is not None and start_note is None:
        raise InvalidGraphParameterError("depth requires start_note to be specified")

    with NotesDatabase(db_path) as db:
        notes, links = db.get_all_links()

    # Apply folder filters
    filtered_notes = filter_notes(notes, exclude_folders, include_folders)

    # Filter links to only include notes that remain
    filtered_names = {n.name for n in filtered_notes}
    filtered_links = [
        l
        for l in links
        if l.source_name in filtered_names and l.target_name in filtered_names
    ]

    # Apply BFS traversal if start_note is specified
    if start_note is not None:
        filtered_notes, filtered_links = _bfs_subgraph(
            filtered_notes, filtered_links, start_note, depth
        )

    # Format and return the output
    return _format_graph_output(filtered_notes, filtered_links, format)
