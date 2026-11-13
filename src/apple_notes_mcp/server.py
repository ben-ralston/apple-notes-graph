"""
Apple Notes MCP Server

Exposes Apple Notes with properly resolved internal [[links]] via MCP protocol.
"""

import functools
import logging
from contextlib import nullcontext

from pathlib import Path
from typing import Annotated, Optional, List

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from apple_notes import (
    read_note,
    get_graph_data,
    get_backlinks as fetch_backlinks,
    get_orphans,
    get_broken_links,
    get_health_report,
    get_link_suggestions,
    NoteContentReader,
    NotesDatabase,
    BackupManager,
    UIScriptingExecutor,
    ClipboardManager,
    NoteNotFoundError,
    DatabaseNotFoundError,
    EncryptedNoteError,
    ProtobufParseError,
    filter_notes,
    filter_folders,
    get_effective_filters,
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
from apple_notes import demo_mode, unified_log
from apple_notes.exceptions import (
    ACCESSIBILITY_SETTINGS,
    AmbiguousFolderError,
    AmbiguousNoteTitleError,
    EncryptedNoteError,
    NoteInTrashError,
    SectionEditDamagedError,
    SectionEditRolledBackError,
    UIHelperTimeoutError,
)
from apple_notes.richtext.section_fragment import (
    _TAGS_TABLES_REFUSAL,
    FragmentError,
    LinkTarget,
    link_resolver_for,
    markdown_to_richtext_new_linked,
)
from apple_notes.section_edit import SectionRequest
from apple_notes.sections import (
    SectionError,
    load_snapshot,
    plan_edit,
    refuse_heavy_media,
    resolve_section,
)
from apple_notes.richtext import (
    markdown_to_richtext,
    markdown_to_richtext_new,
    MarkdownConversionError,
    RichtextConversionResult,
)
from apple_notes.richtext.parser import NotesRichtextParseError
from apple_notes.richtext.serializer import NotesRichtextSerializeError
from apple_notes.ui_operations import (
    execute_create_note,
    execute_edit_note_section,
    execute_find_replace_multiline,
    execute_replace_note,
    execute_replace_note_link,
    execute_toggle_checklist_item,
)
from apple_notes.note_target import (
    is_note_id,
    match_summary,
    require_unique_title,
    resolve_read_note,
    title_for_ref,
    title_for_write,
)
from apple_notes.relink import RelinkError, await_relinked, plan_relink
from apple_notes.export import (
    GRAPH_EXPORTERS,
    note_to_markdown,
    safe_filename,
)
from apple_notes.ui_helper_client import WRITE_TIMEOUT, UIHelperClient
from apple_notes.icloud_sync import nudge_icloud_sync
from apple_notes.profiling import profiled, span
from apple_notes.title_scope import body_text
from apple_notes import write_service

logger = logging.getLogger(__name__)

mcp = FastMCP("Apple Notes")


VALID_FIELDS = {"name", "folder", "uuid", "created", "modified", "last_opened"}


def _live_only(tool_name: str):
    """Refuse a tool that changes notes when the server runs in demo mode.

    Outermost check, before any backup, database read or conversion. The
    layers below (AppleScript runner, clipboard, daemon client) refuse too.
    """

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if demo_mode.is_demo():
                raise ToolError(demo_mode.refusal(tool_name))
            return fn(*args, **kwargs)

        return wrapper

    return decorate



def _resolve_read(ref: str, *, case_insensitive: bool = False):
    """(Note, other_matches) for a read of ``ref``, a title or a note id (#83).

    A shared title reads one note (live before Recently Deleted, then the most
    recently modified); the others come back as ``other_matches`` entries so
    the caller can re-read the one it meant by id.
    """
    with NotesDatabase() as db:
        note, others = resolve_read_note(db, ref, case_insensitive=case_insensitive)
    return note, [match_summary(m) for m in others]


def _write_title(ref: str, *, unique: bool = False) -> str:
    """The title a write addresses for ``ref`` (a title or a note id, #83).

    Writes open a note by title, so an id becomes its note's title. With
    ``unique`` the title must also belong to exactly one note (the tools that
    had no such guard: update_note_content, toggle_checklist_item); the
    others run their own uniqueness guard on the returned title.
    """
    if not unique and not is_note_id(ref):
        return ref
    with NotesDatabase() as db:
        title = title_for_write(db, ref)
        if unique:
            require_unique_title(db, title)
    return title


@mcp.tool()
@profiled("list_notes")
def list_notes(
    limit: Annotated[Optional[int], "Maximum number of notes to return"] = None,
    offset: Annotated[Optional[int], "Number of notes to skip"] = None,
    fields: Annotated[
        Optional[List[str]], "Fields to include (name, folder, uuid)"
    ] = None,
    count_only: Annotated[bool, "Return only count"] = False,
    exclude_folders: Annotated[Optional[List[str]], "Folders to exclude"] = None,
    include_folders: Annotated[
        Optional[List[str]],
        "Folders to show (substring); lifts config exclusions they match",
    ] = None,
    include_all: Annotated[bool, "Bypass all filtering"] = False,
) -> dict:
    """
    List all available Apple Notes.

    Folder Filtering:
        Config file exclusions (e.g., "Archive", "Deleted") ALWAYS apply by default.
        - exclude_folders: ADDS to config exclusions (e.g., also exclude "Projects")
        - include_folders: shows folders the config hides. A pattern lifts every
          config exclusion it matches (case-insensitive substring), so
          include_folders=["Archive"] shows an emoji-prefixed "🗄️ Archive";
          other exclusions still apply. include_all=True shows every folder
        - include_all: Bypasses ALL filtering, returns every note

    Args:
        limit: Maximum number of notes to return (for pagination)
        offset: Number of notes to skip (for pagination)
        fields: List of fields to include in response (name, folder, uuid)
        count_only: If True, return only the total count of notes
        exclude_folders: Additional folder patterns to exclude (merged with config)
        include_folders: Folder patterns to show (case-insensitive substring); lifts
            any config exclusion they match, e.g. ["Archive"] shows "🗄️ Archive"
        include_all: If True, bypass ALL filtering and return every note

    Returns:
        JSON array of notes, or count object, with pagination metadata when applicable
    """
    try:
        # Validate parameters
        if limit is not None and limit < 1:
            raise ToolError("limit must be a positive integer")
        if offset is not None and offset < 0:
            raise ToolError("offset must be non-negative")
        if fields:
            invalid = set(fields) - VALID_FIELDS
            if invalid:
                raise ToolError(
                    f"Invalid fields: {invalid}. Valid fields: {VALID_FIELDS}"
                )

        reader = NoteContentReader()

        # Get all notes first, then apply filters
        notes = reader.get_all_notes()

        # Apply config-based exclusion filters
        effective_exclude, effective_include = get_effective_filters(
            exclude_folders=exclude_folders,
            include_folders=include_folders,
            include_all=include_all,
        )
        notes = filter_notes(notes, effective_exclude, effective_include)

        # Sort by modified date (most recent first), with nulls at the end
        notes.sort(key=lambda n: n.modified or "", reverse=True)

        # Get total count after filtering
        total = len(notes)

        # Handle count_only
        if count_only:
            return {"total": total}

        # Apply pagination
        if offset:
            notes = notes[offset:]
        if limit:
            notes = notes[:limit]

        # Determine which fields to include (uuid hidden by default)
        output_fields = fields if fields else ["name", "folder", "modified"]

        # Build note list
        note_list = [
            {field: getattr(n, field) for field in output_fields} for n in notes
        ]

        # Return with pagination metadata if pagination was requested
        if limit is not None or offset is not None:
            return {
                "notes": note_list,
                "total": total,
                "limit": limit,
                "offset": offset or 0,
            }

        # Simple array for backwards compatibility
        return note_list

    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted "
            "to the terminal/app running this server in System Settings > "
            "Privacy & Security > Full Disk Access."
        )


@mcp.tool()
@profiled("list_folders")
def list_folders(
    include_all: Annotated[bool, "Bypass all filtering"] = False,
) -> list:
    """
    List all folders in Apple Notes.

    Returns folder structure with note counts and hierarchy information.
    Useful for discovering available folders before querying notes.

    Args:
        include_all: If True, bypass ALL filtering and return every folder

    Returns:
        JSON array of folders with name, note_count, parent, and full_path
    """
    try:
        with NotesDatabase() as db:
            folders = db.get_all_folders()

        # Apply config-based exclusion filters
        effective_exclude, _ = get_effective_filters(include_all=include_all)
        folders = filter_folders(folders, effective_exclude, None)

        return folders

    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted "
            "to the terminal/app running this server in System Settings > "
            "Privacy & Security > Full Disk Access."
        )


@mcp.tool()
@profiled("read_note_content", detail_arg="name")
def read_note_content(
    name: str,
    numbered_lines: Annotated[
        bool, "Format content with line numbers (cat -n style)"
    ] = False,
    include_backlinks: Annotated[
        bool, "Include backlinks (notes that link to this note)"
    ] = False,
) -> dict:
    """
    Read a note's full content with resolved internal links.

    Internal note links appear as [[Note Name]] format.
    External URLs appear as [text](url) markdown format.
    Checklists appear as - [ ] or - [x] format.

    Args:
        name: The note's exact title, or its id (the uuid this tool returns as
            `id` and in `other_matches`)
        numbered_lines: If True, format content with line numbers (cat -n style).
            Line 1 is always the note title; body content starts at line 2.
        include_backlinks: If True, include a list of notes that link to this note.
            Requires an additional database query; omitted by default for performance.

    Several notes can share a title. Then this reads one of them (a live note
    before one in Recently Deleted, then the most recently modified) and lists
    the rest in `other_matches` (id, title, folder, modified; titles compared
    case-insensitively). Check `other_matches` before acting on a note, and
    re-read by id to get a different one. It is empty when the title is unique.

    Returns:
        JSON with note content and metadata (including its `id`), resolved
        links and `other_matches`
    """
    try:
        note, other_matches = _resolve_read(name)
        content = read_note(note.name, note=note)
        text = content.resolved_text
        if numbered_lines:
            lines = text.split("\n")
            text = "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(lines, 1))
        result = {
            "id": note.uuid,
            "name": content.name,
            "folder": content.folder,
            "created": content.created,
            "modified": content.modified,
            "last_opened": content.last_opened,
            "content": text,
            "note_links": [
                {"target": link.target_note_name, "uuid": link.target_uuid}
                for link in content.note_links
            ],
            "external_links": [
                {"url": link.url, "text": link.text} for link in content.external_links
            ],
            "attachments": [
                {"type": att.type_uti, "position": att.position}
                for att in content.attachments
            ],
            "other_matches": other_matches,
        }

        if include_backlinks:
            effective_exclude, effective_include = get_effective_filters()
            result["backlinks"] = fetch_backlinks(
                note.name,
                exclude_folders=effective_exclude,
                include_folders=effective_include,
                note_pk=note.pk,
            )

        return result
    except NoteNotFoundError:
        raise ToolError(
            f"Note '{name}' not found. Use list_notes to see available notes."
        )
    except EncryptedNoteError:
        raise ToolError(f"Note '{name}' is encrypted and cannot be read.")
    except ProtobufParseError as e:
        raise ToolError(f"Failed to parse note content: {e}")
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


@mcp.tool()
@profiled("get_backlinks", detail_arg="name")
def get_backlinks(
    name: str,
) -> dict:
    """
    Get notes that link to the given note (backlinks / "what links here?").

    Returns all notes containing a [[Note Name]] link to the specified note.
    Useful for understanding context and discovering related notes without
    reading full note content.

    Args:
        name: The note's exact title, or its id. A shared title is resolved
            as read_note_content resolves it; the others are in `other_matches`.

    Returns:
        JSON with note name, id, backlinks list, count and other_matches
    """
    try:
        note, other_matches = _resolve_read(name)
        effective_exclude, effective_include = get_effective_filters()
        backlinks = fetch_backlinks(
            note.name,
            exclude_folders=effective_exclude,
            include_folders=effective_include,
            note_pk=note.pk,
        )
        return {
            "name": note.name,
            "id": note.uuid,
            "backlinks": backlinks,
            "count": len(backlinks),
            "other_matches": other_matches,
        }
    except NoteNotFoundError:
        raise ToolError(
            f"Note '{name}' not found. Use list_notes to see available notes."
        )
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


@mcp.tool()
@profiled("search_notes", detail_arg="query")
def search_notes(
    query: str,
    search_content: bool = False,
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    include_all: bool = False,
) -> list:
    """
    Search for notes by title or content.

    Folder Filtering:
        Config file exclusions ALWAYS apply by default.
        - exclude_folders: ADDS to config exclusions
        - include_folders: shows folders the config hides. A pattern lifts every
          config exclusion it matches (case-insensitive substring), so
          include_folders=["Archive"] shows an emoji-prefixed "🗄️ Archive";
          other exclusions still apply. include_all=True shows every folder
        - include_all: Bypasses ALL filtering, searches every note

    Args:
        query: Search term to find in note titles (and optionally content)
        search_content: If True, also search within note content (slower)
        exclude_folders: Additional folder patterns to exclude (merged with config)
        include_folders: Folder patterns to show (case-insensitive substring); lifts
            any config exclusion they match, e.g. ["Archive"] shows "🗄️ Archive"
        include_all: If True, bypass ALL filtering and search every note

    Returns:
        JSON array of matching notes with name, folder, and optional snippet
    """
    try:
        reader = NoteContentReader()
        notes = reader.get_all_notes()

        # Apply config-based exclusion filters before searching
        effective_exclude, effective_include = get_effective_filters(
            exclude_folders=exclude_folders,
            include_folders=include_folders,
            include_all=include_all,
        )
        notes = filter_notes(notes, effective_exclude, effective_include)

        query_lower = query.lower()
        results = []

        for note in notes:
            # Title match
            if query_lower in note.name.lower():
                results.append(
                    {"name": note.name, "folder": note.folder, "match_type": "title"}
                )
                continue

            # Content match (if enabled)
            if search_content:
                try:
                    content = reader.get_note_content(note.name)
                    if query_lower in content.resolved_text.lower():
                        # Find snippet around match
                        text = content.resolved_text
                        idx = text.lower().find(query_lower)
                        start = max(0, idx - 50)
                        end = min(len(text), idx + len(query) + 50)
                        snippet = text[start:end]
                        if start > 0:
                            snippet = "..." + snippet
                        if end < len(text):
                            snippet = snippet + "..."

                        results.append(
                            {
                                "name": note.name,
                                "folder": note.folder,
                                "match_type": "content",
                                "snippet": snippet,
                            }
                        )
                except (EncryptedNoteError, ProtobufParseError) as e:
                    # Skip notes we can't read
                    unified_log.log_parse_error("search_notes", note.pk, e)

        return results
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


VALID_FORMATS = {"full", "links_only"}


@mcp.tool()
@profiled("get_note_graph", detail_arg="start_note")
def get_note_graph(
    start_note: Annotated[Optional[str], "Starting note for BFS traversal"] = None,
    depth: Annotated[
        Optional[int], "Max depth from start note (1 = direct connections)"
    ] = None,
    format: Annotated[str, "Output format: 'full' or 'links_only'"] = "full",
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    include_all: bool = False,
) -> dict:
    """
    Get all notes and their link relationships for graph visualization.

    Returns a complete graph structure showing how notes link to each other
    via [[Note Name]] internal links. Optimized for performance - queries
    links directly without parsing note content.

    BFS Traversal:
        - start_note: Begin traversal from this note
        - depth: Limit traversal depth (1 = only direct connections)
        - Without start_note: returns complete graph

    Output Formats:
        - full: Default format with verbose note/link objects
        - links_only: Just links and stats, no note details

    Folder Filtering:
        Config file exclusions ALWAYS apply by default.
        - exclude_folders: ADDS to config exclusions
        - include_folders: shows folders the config hides. A pattern lifts every
          config exclusion it matches (case-insensitive substring), so
          include_folders=["Archive"] shows an emoji-prefixed "🗄️ Archive";
          other exclusions still apply. include_all=True shows every folder
        - include_all: Bypasses ALL filtering, returns every note

    Args:
        start_note: Starting note name for BFS traversal
        depth: Maximum traversal depth from start_note
        format: Output format (full or links_only)
        exclude_folders: Additional folder patterns to exclude (merged with config)
        include_folders: Folder patterns to show (case-insensitive substring); lifts
            any config exclusion they match, e.g. ["Archive"] shows "🗄️ Archive"
        include_all: If True, bypass ALL filtering and return every note

    Returns:
        JSON with nodes (notes), edges (links), and stats
    """
    # Validate parameters
    if depth is not None:
        if not isinstance(depth, int) or depth < 1:
            raise ToolError("depth must be a positive integer (1 or greater)")
        if start_note is None:
            raise ToolError("depth requires start_note to be specified")

    if format not in VALID_FORMATS:
        raise ToolError(f"Invalid format '{format}'. Must be one of: {VALID_FORMATS}")

    try:
        # Get effective filters from config or parameters
        effective_exclude, effective_include = get_effective_filters(
            exclude_folders=exclude_folders,
            include_folders=include_folders,
            include_all=include_all,
        )

        data = get_graph_data(
            exclude_folders=effective_exclude,
            include_folders=effective_include,
            start_note=start_note,
            depth=depth,
            format=format,
        )
        return data
    except NoteNotFoundError as e:
        raise ToolError(f"Start note not found: {e}")
    except InvalidGraphParameterError as e:
        raise ToolError(str(e))
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


VALID_NOTE_EXPORT_FORMATS = {"markdown"}


def _prepare_output_dir(output_dir: str) -> Path:
    """Expand and create the export output directory."""
    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@mcp.tool()
@profiled("export_note", detail_arg="name")
def export_note(
    name: str,
    output_dir: Annotated[str, "Directory to write the exported file into"],
    format: Annotated[str, "Export format: 'markdown'"] = "markdown",
) -> dict:
    """
    Export a single note as a Markdown file.

    Writes <output_dir>/<note name>.md with the note title as an H1
    heading and the body as Markdown. Internal [[Note Name]] links are
    preserved as-is (compatible with Obsidian, Logseq, etc.). The output
    directory is created if needed; an existing file with the same name
    is overwritten (repeat exports refresh the file).

    Args:
        name: The note's exact title, or its id. A shared title is resolved
            as read_note_content resolves it; the others are in `other_matches`.
        output_dir: Directory to write the exported file into
        format: Export format (only 'markdown' is supported)

    Returns:
        JSON with the exported file path and other_matches
    """
    if format not in VALID_NOTE_EXPORT_FORMATS:
        raise ToolError(
            f"Invalid format '{format}'. Must be one of: {VALID_NOTE_EXPORT_FORMATS}"
        )

    try:
        note, other_matches = _resolve_read(name)
        content = read_note(note.name, note=note)
        directory = _prepare_output_dir(output_dir)
        path = directory / f"{safe_filename(note.name)}.md"
        path.write_text(note_to_markdown(content), encoding="utf-8")
        return {
            "success": True,
            "note_name": note.name,
            "id": note.uuid,
            "path": str(path),
            "format": format,
            "other_matches": other_matches,
        }
    except NoteNotFoundError:
        raise ToolError(
            f"Note '{name}' not found. Use list_notes to see available notes."
        )
    except EncryptedNoteError:
        raise ToolError(f"Note '{name}' is encrypted and cannot be exported.")
    except ProtobufParseError as e:
        raise ToolError(f"Failed to parse note content: {e}")
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )
    except OSError as e:
        raise ToolError(f"Failed to write export file: {e}")


@mcp.tool()
@profiled("export_folder", detail_arg="folder")
def export_folder(
    folder: Annotated[str, "Full folder path to export (e.g. 'Work/Projects')"],
    output_dir: Annotated[str, "Directory to write the exported files into"],
    format: Annotated[str, "Export format: 'markdown'"] = "markdown",
) -> dict:
    """
    Export all notes in a folder as individual Markdown files.

    Each note becomes <output_dir>/<note name>.md with [[Note Name]]
    links preserved as-is (compatible with Obsidian, Logseq, etc.).
    The folder must match the note's full folder path exactly
    (case-insensitive); subfolders are not included. Notes that cannot
    be read (encrypted, unparseable) are skipped and reported. Notes
    with duplicate titles get a numeric suffix. Existing files are
    overwritten (repeat exports refresh the files).

    Args:
        folder: Full folder path whose notes should be exported
        output_dir: Directory to write the exported files into
        format: Export format (only 'markdown' is supported)

    Returns:
        JSON with exported file paths, count, and any skipped notes
    """
    if format not in VALID_NOTE_EXPORT_FORMATS:
        raise ToolError(
            f"Invalid format '{format}'. Must be one of: {VALID_NOTE_EXPORT_FORMATS}"
        )

    try:
        reader = NoteContentReader()
        notes = reader.get_all_notes()

        folder_lower = folder.lower()
        matching = [n for n in notes if n.folder and n.folder.lower() == folder_lower]
        if not matching:
            raise ToolError(
                f"No notes found in folder '{folder}'. "
                "Use list_folders to see available folders."
            )

        directory = _prepare_output_dir(output_dir)
        used_stems: set[str] = set()
        exported = []
        skipped = []

        for note in matching:
            try:
                # Fetch by identity (pk), not name — duplicate titles would
                # all resolve to the first title match otherwise.
                content = reader.get_note_content_for_note(note)
            except (EncryptedNoteError, ProtobufParseError) as e:
                unified_log.log_parse_error("export_folder", note.pk, e)
                skipped.append(
                    {"note_name": note.name, "reason": f"{type(e).__name__}: {e}"}
                )
                continue

            stem = safe_filename(note.name)
            candidate = stem
            counter = 2
            while candidate.lower() in used_stems:
                candidate = f"{stem} ({counter})"
                counter += 1
            used_stems.add(candidate.lower())

            path = directory / f"{candidate}.md"
            path.write_text(note_to_markdown(content), encoding="utf-8")
            exported.append({"note_name": note.name, "path": str(path)})

        return {
            "success": True,
            "folder": folder,
            "output_dir": str(directory),
            "count": len(exported),
            "exported": exported,
            "skipped": skipped,
        }
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )
    except OSError as e:
        raise ToolError(f"Failed to write export file: {e}")


@mcp.tool()
@profiled("export_graph")
def export_graph(
    output_dir: Annotated[str, "Directory to write the exported file into"],
    format: Annotated[str, "Export format: 'json', 'dot', or 'csv'"] = "json",
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    include_all: bool = False,
) -> dict:
    """
    Export the full note graph structure to a file.

    Writes <output_dir>/notes-graph.<format>. Supported formats:
    - json: Nodes and edges as JSON (D3/vis.js-compatible: nodes use "id")
    - dot: GraphViz DOT directed graph
    - csv: Edge list with a source,target header

    Folder Filtering:
        Config file exclusions ALWAYS apply by default.
        - exclude_folders: ADDS to config exclusions
        - include_folders: shows folders the config hides. A pattern lifts every
          config exclusion it matches (case-insensitive substring), so
          include_folders=["Archive"] shows an emoji-prefixed "🗄️ Archive";
          other exclusions still apply. include_all=True shows every folder
        - include_all: Bypasses ALL filtering, exports every note

    Args:
        output_dir: Directory to write the exported file into
        format: Export format (json, dot, or csv)
        exclude_folders: Additional folder patterns to exclude (merged with config)
        include_folders: Folder patterns to show (case-insensitive substring); lifts
            any config exclusion they match, e.g. ["Archive"] shows "🗄️ Archive"
        include_all: If True, bypass ALL filtering and export every note

    Returns:
        JSON with the exported file path and graph stats
    """
    if format not in GRAPH_EXPORTERS:
        raise ToolError(
            f"Invalid format '{format}'. Must be one of: {set(GRAPH_EXPORTERS)}"
        )

    try:
        effective_exclude, effective_include = get_effective_filters(
            exclude_folders=exclude_folders,
            include_folders=include_folders,
            include_all=include_all,
        )
        data = get_graph_data(
            exclude_folders=effective_exclude,
            include_folders=effective_include,
            format="full",
        )
        directory = _prepare_output_dir(output_dir)
        path = directory / f"notes-graph.{format}"
        path.write_text(GRAPH_EXPORTERS[format](data), encoding="utf-8")
        return {
            "success": True,
            "path": str(path),
            "format": format,
            "stats": data["stats"],
        }
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )
    except OSError as e:
        raise ToolError(f"Failed to write export file: {e}")


_DB_NOT_FOUND_MESSAGE = (
    "Apple Notes database not found. Ensure Full Disk Access is granted."
)


@mcp.tool()
@profiled("find_orphans")
def find_orphans(
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    include_all: bool = False,
) -> dict:
    """
    Find orphan notes: notes with no incoming or outgoing note links.

    Orphans are computed on the filtered view (a note linked only to
    excluded-folder notes counts as an orphan here), and broken links do
    not count as connections. Fast — no note content is parsed.

    Librarian workflow: run this during on-demand triage ("clean up my
    notes"), then call suggest_links on each orphan to find where it could
    connect.

    Folder Filtering:
        Config file exclusions ALWAYS apply by default.
        - exclude_folders: ADDS to config exclusions
        - include_folders: shows folders the config hides. A pattern lifts every
          config exclusion it matches (case-insensitive substring), so
          include_folders=["Archive"] shows an emoji-prefixed "🗄️ Archive";
          other exclusions still apply. include_all=True shows every folder
        - include_all: Bypasses ALL filtering

    Returns:
        JSON with orphans (name/folder/modified, newest first), count,
        and total_notes in the filtered view.
    """
    try:
        effective_exclude, effective_include = get_effective_filters(
            exclude_folders=exclude_folders,
            include_folders=include_folders,
            include_all=include_all,
        )
        return get_orphans(
            exclude_folders=effective_exclude,
            include_folders=effective_include,
        )
    except DatabaseNotFoundError:
        raise ToolError(_DB_NOT_FOUND_MESSAGE)


@mcp.tool()
@profiled("find_broken_links")
def find_broken_links(
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    include_all: bool = False,
) -> dict:
    """
    Find broken note links: links whose target note no longer exists
    (deleted, or moved to another account).

    Each entry reports the source note, the intended target title as it
    was when the link was created (may be null), the dangling target uuid,
    and how many times that link occurs in the source note. Folder filters
    apply to the source note only. Fast — no note content is parsed.

    Librarian workflow: surface these during periodic health checks and
    offer to remove or re-point the dead references (via
    update_note_content find_replace).

    Returns:
        JSON with broken_links (sorted by source note) and count.
    """
    try:
        effective_exclude, effective_include = get_effective_filters(
            exclude_folders=exclude_folders,
            include_folders=include_folders,
            include_all=include_all,
        )
        return get_broken_links(
            exclude_folders=effective_exclude,
            include_folders=effective_include,
        )
    except DatabaseNotFoundError:
        raise ToolError(_DB_NOT_FOUND_MESSAGE)


@mcp.tool()
@profiled("note_health_check")
def note_health_check(
    stale_threshold_days: int = 180,
    top_n: int = 10,
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    include_all: bool = False,
) -> dict:
    """
    Comprehensive notes-graph health report: totals, orphans, broken
    links, stale notes, most-connected notes, clusters, and duplicate
    titles. Fast — metadata and link rows only, no note content parsing.

    Semantics:
        - Orphans: no links in either direction (filtered view).
        - Stale: modified longer than stale_threshold_days ago; notes with
          no known modified date count as stale and sort first. The notes
          list shows the top_n oldest; count is the full number.
        - Connections: distinct linked neighbors (duplicate links between
          the same pair count once).
        - Clusters: communities of size >= 2, hub = the highest-degree
          member; top_n largest shown. Connected components larger than
          30 notes are subdivided by deterministic community detection
          (greedy modularity), so hub-linked mega-components report their
          thematic neighborhoods instead of one giant blob.
        - Duplicate names: case-insensitive title collisions — name-based
          tools resolve these arbitrarily, so consider renaming.

    Librarian workflow: run this periodically ("how are my notes doing?"),
    report the findings, then offer fixes — suggest_links for orphans,
    find_replace for broken links, review/archive for stale notes.

    Args:
        stale_threshold_days: Age in days after which a note counts as
            stale (default 180, must be >= 0)
        top_n: Cap for stale-note list, top_connected, and clusters
            (default 10, must be >= 1)

    Returns:
        JSON health report (see issue #77 for the full shape).
    """
    if stale_threshold_days < 0:
        raise ToolError("stale_threshold_days must be >= 0")
    if top_n < 1:
        raise ToolError("top_n must be >= 1")

    try:
        effective_exclude, effective_include = get_effective_filters(
            exclude_folders=exclude_folders,
            include_folders=include_folders,
            include_all=include_all,
        )
        return get_health_report(
            stale_threshold_days=stale_threshold_days,
            top_n=top_n,
            exclude_folders=effective_exclude,
            include_folders=effective_include,
        )
    except DatabaseNotFoundError:
        raise ToolError(_DB_NOT_FOUND_MESSAGE)


@mcp.tool()
@profiled("suggest_links", detail_arg="note_name")
def suggest_links(
    note_name: str,
    max_candidates: int = 25,
    exclude_folders: Optional[List[str]] = None,
    include_folders: Optional[List[str]] = None,
    include_all: bool = False,
) -> dict:
    """
    Gather context for suggesting [[links]] from a note: the note's
    content plus candidate notes surfaced by transparent recall signals.
    No scoring happens server-side — read the content and each candidate's
    reasons, judge semantic fit yourself, and propose only links that make
    sense.

    Candidate reasons (a candidate may have several):
        - forward_mention: candidate's title appears in this note's text
          without a link (snippets included)
        - reverse_mention: this note's title appears in the candidate's
          text without a link (snippets included)
        - co_citation: shares linked neighbors with this note
        - title_overlap: shares meaningful title words
        - same_folder: lives in the same folder
        - backlink_not_reciprocated: candidate links here, no link back

    Notes already linked from this note are excluded. Candidates are
    ordered by how many signals fired (not by relevance — that's your
    job); candidates whose only signal is same_folder sort last. Short or
    generic titles (e.g. "Home") never produce mention matches.

    This is the one librarian tool that scans note bodies (per-note
    protobuf parse, like search_notes with search_content=true), so it
    takes a few seconds on large collections. Encrypted/unparseable notes
    are skipped and counted in skipped_notes.

    Librarian workflow: call this right after add_note to offer link
    opportunities for the new note, or per-orphan during triage.

    Args:
        note_name: The note's exact title, or its id (ignoring folder
            filters). A shared title is resolved as read_note_content
            resolves it; the others are in `other_matches`.
        max_candidates: Cap on returned candidates (default 25, >= 1)

    Returns:
        JSON with note (content, existing links/backlinks), candidates
        (each with reasons), candidate_count, skipped_notes and other_matches.
    """
    if max_candidates < 1:
        raise ToolError("max_candidates must be >= 1")

    try:
        # suggest_links has always found its note case-insensitively.
        note, other_matches = _resolve_read(note_name, case_insensitive=True)
        effective_exclude, effective_include = get_effective_filters(
            exclude_folders=exclude_folders,
            include_folders=include_folders,
            include_all=include_all,
        )
        result = get_link_suggestions(
            note.name,
            max_candidates=max_candidates,
            exclude_folders=effective_exclude,
            include_folders=effective_include,
            note_pk=note.pk,
        )
        result["other_matches"] = other_matches
        return result
    except NoteNotFoundError:
        raise ToolError(
            f"Note '{note_name}' not found. Use list_notes to see available notes."
        )
    except EncryptedNoteError:
        raise ToolError(f"Note '{note_name}' is encrypted and cannot be read.")
    except ProtobufParseError as e:
        raise ToolError(f"Failed to parse note content: {e}")
    except DatabaseNotFoundError:
        raise ToolError(_DB_NOT_FOUND_MESSAGE)


def _is_gui_session() -> bool:
    """Check if running in a macOS GUI session (not SSH/remote).

    Delegates to write_service (issue #117) so the MCP server and the
    Electron add-link script share one definition.
    """
    return write_service.is_gui_session()


def _daemon_error(exc: UIHelperNotAvailableError) -> ToolError:
    """The ToolError for a daemon-routed write that got no answer.

    A timeout means the daemon took the command and may still be running it
    (issues #143, #125 NTG-012): telling the caller to reinstall the daemon
    invited a retry that applied the write twice.
    """
    if isinstance(exc, UIHelperTimeoutError):
        return ToolError(
            "The UI helper daemon didn't answer in time; the write may still "
            "complete. Re-read the note before retrying."
        )
    return ToolError(
        "Running in a remote/SSH session but the UI helper daemon is not available. "
        "Install and start it with: ./run ui-helper-install"
    )


def _build_name_to_uuid() -> dict:
    """Case-insensitive note name → UUID mapping; a title several live notes
    share maps to a write_service.SharedTitle, so a [[link]] to it is refused
    instead of silently picking one (#83)."""
    return write_service.build_name_to_uuid()


def _link_targets(markdown: Optional[str], db=None) -> dict[str, LinkTarget]:
    """Targets for every [[Name]] in `markdown` that names a live note.

    Keyed by lower-case title; each carries what a synthesized link needs
    (uuid, live title, ownerIdentifier — issues #184, #212). Every write tool
    pastes links as attachments built from these, never through the Cmd+K
    picker. A match with no owner id is refused (ToolError) before anything
    is written. `db`: an already-open NotesDatabase to reuse.
    """
    try:
        return write_service.link_targets(
            markdown,
            name_to_uuid=_build_name_to_uuid,
            open_db=(lambda: nullcontext(db)) if db is not None else NotesDatabase,
        )
    except FragmentError as e:
        raise ToolError(str(e)) from None


def _perform_backup() -> str:
    """
    Perform a backup before write operations.

    Returns:
        Path to the backup file as a string
    """
    backup_manager = BackupManager()
    backup_path = backup_manager.create_backup()
    backup_manager.cleanup_old_backups()
    return str(backup_path)


# Maximum allowed note name length
MAX_NOTE_NAME_LENGTH = 255


@mcp.tool()
@profiled("add_note", detail_arg="name", attach_response=True)
@_live_only("add_note")
def add_note(
    name: str,
    body: str,
    folder: str = "Notes",
) -> dict:
    """
    Create a new note in Apple Notes.

    Automatically creates a backup of the Notes database before making changes.

    Body is interpreted as markdown and converted to native Notes richtext
    formatting. Supports: checklists (- [ ] / - [x]), bold (**text**),
    italic (*text*), headings (## / ###), lists (- / * / 1.), block quotes (>),
    and monospace (`code`). Plain text works as-is since markdown is a superset.

    Newlines: Use actual newline characters in the body content, not escaped
    sequences like "\\n". Multiple newlines create paragraph breaks.

    Use [[Note Name]] syntax in the body to reference other notes.

    To insert an Apple Notes tag, write {#tag-name} (e.g. {#daily-note}).
    Ordinary "#word" text (e.g. "#1", "ranked #3") is written as literal
    characters, not a tag.

    Args:
        name: The name/title for the new note (max 255 characters, no newlines)
        body: The body content in markdown (plain text also works).
              Use [[Note Name]] syntax to reference other notes.
              Use {#tag-name} for an Apple Notes tag; plain "#word" stays literal.
        folder: The folder to create the note in (default: "Notes")

    Returns:
        JSON with created note info and backup path, or error response
    """
    # Validate note name before any expensive operations
    if not name or not name.strip():
        raise ToolError("Note name cannot be empty or whitespace-only")

    if len(name) > MAX_NOTE_NAME_LENGTH:
        raise ToolError(
            f"Note name too long (max {MAX_NOTE_NAME_LENGTH} characters, "
            f"got {len(name)})"
        )

    if "\n" in name or "\r" in name:
        raise ToolError("Note name cannot contain newlines")

    # UI scripting only supports creating notes in the default "Notes" folder
    if folder != "Notes":
        raise ToolError(
            f"Only 'Notes' folder is supported. Got '{folder}'. "
            "Move notes to other folders manually after creation."
        )

    try:
        # Create backup first
        with span("backup"):
            backup_path = _perform_backup()

        # Convert markdown to richtext (always done MCP-side); [[links]] are
        # synthesized as link attachments, not typed (issue #212)
        links = _link_targets(body)
        with span("convert"):
            result = markdown_to_richtext_new_linked(body, links)

        if _is_gui_session():
            logger.info("add_note: GUI session detected, using direct execution")
            with span("execute"):
                execute_create_note(name, result.data, folder, result.pending_actions)
        else:
            logger.info(
                "add_note: Remote session detected, routing through UI helper daemon"
            )
            client = UIHelperClient()
            with span("execute"):
                client.create_note(name, result.data, folder, result.pending_actions)

        with span("sync"):
            sync_nudged = nudge_icloud_sync()

        return {
            "success": True,
            "note_name": name,
            "folder": folder,
            "backup_path": backup_path,
            "sync_nudged": sync_nudged,
        }

    except BackupError as e:
        raise ToolError(f"Failed to create backup, write operation aborted: {e}")
    except UIHelperNotAvailableError as e:
        raise _daemon_error(e) from e
    except AccessibilityPermissionError:
        raise ToolError(
            "Accessibility permissions required for UI scripting. "
            f"Grant access in {ACCESSIBILITY_SETTINGS} "
            "for the terminal/app running this server."
        )
    except ClipboardError as e:
        raise ToolError(f"Clipboard operation failed: {e}")
    except MarkdownConversionError as e:
        raise ToolError(f"Markdown conversion failed: {e}")
    except SessionLockedError as e:
        raise ToolError(str(e))
    except NotesNotFrontmostError as e:
        # Verbatim: the safety message names the app keystrokes were refused to.
        raise ToolError(str(e))
    except WriteVerificationError as e:
        # Verbatim: tells the caller the write did not take (no false success).
        raise ToolError(str(e))
    except UIScriptingError as e:
        raise ToolError(f"UI scripting failed: {e}")
    except AppleScriptError as e:
        raise ToolError(f"Failed to create note: {e}")
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


VALID_OPERATIONS = {"append", "replace", "find_replace"}


@mcp.tool()
@profiled("update_note_content", detail_arg="name", attach_response=True)
@_live_only("update_note_content")
def update_note_content(
    name: str,
    body: str,
    operation: Annotated[str, "Operation mode: 'append', 'replace', or 'find_replace'"],
    old_text: Annotated[
        Optional[str],
        "Text to find and replace (required for find_replace). Must match exactly once.",
    ] = None,
) -> dict:
    """
    Update an existing note's content using UI scripting.

    IMPORTANT: This operation uses UI scripting and requires:
    - Apple Notes app to be open (will be launched if not running)
    - Accessibility permissions for the terminal/app running this server
      (System Settings > Privacy & Security > Device Control and Data Access)
    - The note will be briefly focused during the operation

    Automatically creates a backup of the Notes database before making changes.

    Body is interpreted as markdown and converted to native Notes richtext
    formatting. Supports: checklists (- [ ] / - [x]), bold (**text**),
    italic (*text*), headings (## / ###), lists (- / * / 1.), block quotes (>),
    monospace (`code`), and 2-column tables (| A | B |, any number of rows;
    a table of any other width is refused before anything is written).
    Plain text works as-is since markdown is a superset.

    Operations:
    - append: Add content at end of note (preserves all existing content)
    - replace: Replace entire note body with new content
    - find_replace: Find old_text in the note and replace with body.
      Preferred for targeted edits — preserves all formatting (bold, colors,
      block quotes, checklists) and native table attachments.
      old_text must match exactly once in the note body (case-sensitive).
      Empty body deletes the matched text. Searches only the body (not the
      title). Use surrounding context to disambiguate if multiple matches
      exist. Both old_text and body can be multi-line. Body supports
      markdown formatting (bold, italic, etc.). Plain text body inherits
      the existing formatting at the replacement position.

    Newlines: Use actual newline characters in the body content, not escaped
    sequences like "\\n". Multiple newlines create paragraph breaks.

    Use [[Note Name]] syntax in the body to reference other notes.

    To insert an Apple Notes tag, write {#tag-name} (e.g. {#daily-note}).
    Ordinary "#word" text (e.g. "#1", "ranked #3") is written as literal
    characters, not a tag.

    Args:
        name: The exact title of the note to update, or its id. Writes open notes by title, so a title several notes share is refused (rename one first), even by id
        body: The new content in markdown (plain text also works).
              Use [[Note Name]] syntax to reference other notes.
              Use {#tag-name} for an Apple Notes tag; plain "#word" stays literal.
              For find_replace, this is the replacement text (empty to delete).
        operation: Operation mode - 'append', 'replace', or 'find_replace'
        old_text: Text to find and replace (required for find_replace).
              Must match exactly once in the note body (case-sensitive).

    Returns:
        JSON with updated note info and backup path, or error response
    """
    # Validate note name
    if not name or not name.strip():
        raise ToolError("Note name cannot be empty or whitespace-only")

    # Validate operation
    if operation not in VALID_OPERATIONS:
        raise ToolError(
            f"Invalid operation '{operation}'. Must be one of: {VALID_OPERATIONS}"
        )

    # Validate find_replace parameters
    if operation == "find_replace":
        if old_text is None:
            raise ToolError("old_text is required for find_replace operation")
        if old_text == "":
            raise ToolError("old_text cannot be empty for find_replace operation")
    else:
        if old_text is not None:
            raise ToolError("old_text is only valid for find_replace operation")

    try:
        # The title must name exactly one note before anything happens, as on
        # the remote queue: the UI path opens the note by title (#83).
        name = _write_title(name, unique=True)

        # Create backup first
        with span("backup"):
            backup_path = _perform_backup()

        # Link targets for every operation: [[links]] are pasted as
        # synthesized attachments, never typed through Cmd+K (issue #212)
        links = _link_targets(body)
        is_local = _is_gui_session()
        if is_local:
            logger.info(
                "update_note_content(%s): GUI session detected, using direct execution",
                operation,
            )
        else:
            logger.info(
                "update_note_content(%s): Remote session detected, routing through UI helper daemon",
                operation,
            )

        if operation == "append":
            # Shared chain with the Electron add-link script (write_service):
            # backup + resolver already prepared above for all operations.
            write_service.append_markdown_to_note(
                name, body, links=links, skip_backup=True
            )

        elif operation == "find_replace":
            # Pre-check: verify old_text appears exactly once in note body
            # The richtext engine scopes replacements to the body through the
            # same helper (body_only in find_replace_in_notes_richtext_variable),
            # so the guard and the replacement agree about the title.
            # The live note (never a same-titled copy in Recently Deleted,
            # which a by-title read could pick): the one the write targets.
            live_note, _ = _resolve_read(name)
            content = read_note(live_note.name, note=live_note)
            resolved_body = body_text(content.resolved_text)

            count = resolved_body.count(old_text)
            if count == 0:
                raise ToolError(
                    "old_text not found in note body. "
                    "Verify the text matches exactly (case-sensitive)."
                )
            if count > 1:
                raise ToolError(
                    f"old_text found {count} times in note body. "
                    "Provide more surrounding context to uniquely identify the text."
                )

            # Check if old_text exists in the raw protobuf text (which has
            # U+FFFC placeholders for tables/attachments and plain text for
            # links instead of rendered markdown). If found in resolved_text
            # but not raw_text, the text is inside a table cell or spans
            # rendered markup (e.g. [[links]], [text](url), **bold**) that
            # doesn't exist in the underlying note data.
            if body_text(content.raw_text).count(old_text) == 0:
                raise ToolError(
                    "old_text was found in the rendered note but not in the "
                    "underlying note data. This usually means the text is "
                    "inside a table cell, or includes rendered link/formatting "
                    "markup (e.g. [[Note Name]], [text](url), **bold**). "
                    "For table cells, edit directly in Apple Notes. "
                    "For links/formatting, use the plain text form without "
                    "markup characters as old_text."
                )

            # Use richtext binary manipulation: copies entire note, does
            # text find/replace in the binary, pastes back. Preserves all
            # formatting (block quotes, colors, tables) and attachments.
            # Replacement body supports markdown formatting.
            if is_local:
                with span("execute"):
                    execute_find_replace_multiline(
                        name,
                        old_text,
                        body,
                        links=links,
                    )
            else:
                client = UIHelperClient()
                with span("execute"):
                    client.find_replace(name, old_text, body, links)

        else:  # operation == "replace"
            if is_local:
                with span("execute"):
                    execute_replace_note(name, body, links)
            else:
                client = UIHelperClient()
                with span("execute"):
                    client.replace_note(name, body, links)

        with span("sync"):
            sync_nudged = nudge_icloud_sync()

        return {
            "success": True,
            "note_name": name,
            "operation": operation,
            "backup_path": backup_path,
            "sync_nudged": sync_nudged,
        }

    except (AmbiguousNoteTitleError, NoteInTrashError) as e:
        # Verbatim: the refusal names every same-titled match, or why an id
        # of a note in Recently Deleted can't be written (#83).
        raise ToolError(str(e)) from e
    except BackupError as e:
        raise ToolError(f"Failed to create backup, write operation aborted: {e}")
    except UIHelperNotAvailableError as e:
        raise _daemon_error(e) from e
    except NoteNotFoundError:
        raise ToolError(
            f"Note '{name}' not found. Use list_notes to see available notes."
        )
    except EncryptedNoteError:
        raise ToolError(f"Note '{name}' is encrypted and cannot be edited.")
    except ProtobufParseError as e:
        raise ToolError(f"Failed to parse note content for editing: {e}")
    except AccessibilityPermissionError:
        raise ToolError(
            "Accessibility permissions required for UI scripting. "
            f"Grant access in {ACCESSIBILITY_SETTINGS} "
            "for the terminal/app running this server."
        )
    except ClipboardError as e:
        raise ToolError(f"Clipboard operation failed: {e}")
    except SessionLockedError as e:
        raise ToolError(str(e))
    except NotesNotFrontmostError as e:
        # Verbatim: the safety message names the app keystrokes were refused to.
        raise ToolError(str(e))
    except WriteVerificationError as e:
        # Verbatim: tells the caller the write did not take (no false success).
        raise ToolError(str(e))
    except UIScriptingError as e:
        raise ToolError(f"UI scripting failed: {e}")
    except MarkdownConversionError as e:
        raise ToolError(f"Markdown conversion failed: {e}")
    except AppleScriptError as e:
        raise ToolError(f"Failed to update note: {e}")
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


@mcp.tool()
@profiled("delete_note", detail_arg="name", attach_response=True)
@_live_only("delete_note")
def delete_note(
    name: str,
) -> dict:
    """
    Delete a note from Apple Notes (moves to Recently Deleted).

    Only call this when the owner has explicitly asked to delete that specific note.

    The note is moved to the "Recently Deleted" folder, identical to manual
    deletion in Notes.app. It can be recovered from Recently Deleted within
    30 days. This never deletes permanently.

    Refused, with nothing changed: a title no note has, a title several notes
    share (case-insensitive, Recently Deleted included; the refusal lists each
    match's folder and modified date), a note already in Recently Deleted,
    and a locked (password-protected) note.

    Automatically creates a backup of the Notes database before making changes.

    Args:
        name: The exact title of the note to delete, or its id. Writes open notes by title, so a title several notes share is refused (rename one first), even by id

    Returns:
        The deleted note's title, folder, modified date and the first ~200
        characters of its body (check it is the note you meant), plus the
        backup path
    """
    # Validate note name
    if not name or not name.strip():
        raise ToolError("Note name cannot be empty or whitespace-only")

    try:
        # Shared chain (guards -> backup -> local/daemon route -> sync nudge)
        # lives in write_service so the Electron delete-note script and the
        # remote queue can't fork from it.
        result = write_service.delete_note_by_name(_write_title(name))

        return {
            "success": True,
            "note_name": result["title"],
            "folder": result["folder"],
            "modified": result["modified"],
            "preview": result["preview"],
            "backup_path": result["backup_path"],
            "sync_nudged": result["sync_nudged"],
        }

    except (AmbiguousNoteTitleError, NoteInTrashError, EncryptedNoteError) as e:
        # Verbatim: the refusal names the reason (and every same-titled match).
        raise ToolError(str(e)) from e
    except BackupError as e:
        raise ToolError(f"Failed to create backup, delete operation aborted: {e}")
    except UIHelperNotAvailableError as e:
        raise _daemon_error(e) from e
    except NoteNotFoundError as e:
        raise ToolError(str(e)) from e
    except AppleScriptError as e:
        raise ToolError(f"Failed to delete note: {e}")
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


@mcp.tool()
@profiled("move_note", detail_arg="name", attach_response=True)
@_live_only("move_note")
def move_note(
    name: str,
    folder: Annotated[str, "Existing folder to move the note into"],
) -> dict:
    """
    Move a note into an existing folder (e.g. to archive it).

    The folder must already exist; it is never created. It is matched
    case-insensitively by full path ("Other/🗄️ Archive"), then by folder
    name ("🗄️ Archive"), then ignoring leading emoji or symbols ("Archive");
    the first of those that matches decides, and more than one match there is
    refused (use the full path). Recently Deleted and smart folders are not
    move targets: use delete_note to delete.

    Refused, with nothing changed: a title no note has, a title several notes
    share (case-insensitive, Recently Deleted included; the refusal lists each
    match's folder and modified date), a note in Recently Deleted, and a
    locked (password-protected) note. Moving a note to the folder it is
    already in changes nothing (moved: false).

    Pure AppleScript (no keystrokes, no focus change). Creates a backup of
    the Notes database first.

    Args:
        name: The exact title of the note to move, or its id. Writes open notes by title, so a title several notes share is refused (rename one first), even by id
        folder: The destination folder (see list_folders)

    Returns:
        old_folder and new_folder (full paths), and whether the note moved
    """
    if not name or not name.strip():
        raise ToolError("Note name cannot be empty or whitespace-only")
    if not folder or not folder.strip():
        raise ToolError("Folder name cannot be empty or whitespace-only")

    try:
        result = write_service.move_note_by_name(_write_title(name), folder)
        return {
            "success": True,
            "note_name": result["title"],
            "old_folder": result["old_folder"],
            "new_folder": result["new_folder"],
            "moved": result["moved"],
            "backup_path": result["backup_path"],
            "sync_nudged": result["sync_nudged"],
        }
    except (
        AmbiguousNoteTitleError,
        AmbiguousFolderError,
        NoteInTrashError,
        EncryptedNoteError,
        NoteNotFoundError,
        FolderNotFoundError,
    ) as e:
        raise ToolError(str(e)) from e
    except BackupError as e:
        raise ToolError(f"Failed to create backup, move aborted: {e}")
    except UIHelperNotAvailableError as e:
        raise _daemon_error(e) from e
    except AppleScriptError as e:
        raise ToolError(f"Failed to move note: {e}")
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


@mcp.tool()
@profiled("toggle_checklist_item", detail_arg="name", attach_response=True)
@_live_only("toggle_checklist_item")
def toggle_checklist_item(
    name: str,
    item_text: Annotated[str, "Text of the checklist item to toggle"],
) -> dict:
    """
    Toggle a checklist item's checked/unchecked state in a note.

    Finds a checklist item by its text content and toggles its state
    (checked → unchecked, or unchecked → checked). Works directly on
    the note's richtext format, preserving all other formatting.

    Automatically creates a backup of the Notes database before making changes.

    Args:
        name: The exact title of the note containing the checklist, or its id. Writes open notes by title, so a title several notes share is refused (rename one first), even by id
        item_text: Text content of the checklist item to toggle.
            Must match text within a checklist line (partial match OK).

    Returns:
        JSON with toggled item info and backup path
    """
    # Validate parameters
    if not name or not name.strip():
        raise ToolError("Note name cannot be empty or whitespace-only")
    if not item_text or not item_text.strip():
        raise ToolError("item_text cannot be empty or whitespace-only")

    try:
        # Exactly one note may carry the title: the toggle opens it by title (#83).
        name = _write_title(name, unique=True)

        # Create backup first
        with span("backup"):
            backup_path = _perform_backup()

        if _is_gui_session():
            logger.info(
                "toggle_checklist_item: GUI session detected, using direct execution"
            )
            with span("execute"):
                execute_toggle_checklist_item(name, item_text)
        else:
            logger.info(
                "toggle_checklist_item: Remote session detected, routing through UI helper daemon"
            )
            client = UIHelperClient()
            with span("execute"):
                client.toggle_checklist_item(name, item_text)

        with span("sync"):
            sync_nudged = nudge_icloud_sync()

        return {
            "success": True,
            "note_name": name,
            "item_text": item_text,
            "backup_path": backup_path,
            "sync_nudged": sync_nudged,
        }

    except (AmbiguousNoteTitleError, NoteInTrashError) as e:
        # Verbatim: the refusal names every same-titled match, or why an id
        # of a note in Recently Deleted can't be written (#83).
        raise ToolError(str(e)) from e
    except BackupError as e:
        raise ToolError(f"Failed to create backup, toggle operation aborted: {e}")
    except UIHelperNotAvailableError as e:
        raise _daemon_error(e) from e
    except NoteNotFoundError:
        raise ToolError(
            f"Note '{name}' not found. Use list_notes to see available notes."
        )
    except ValueError as e:
        raise ToolError(str(e))
    except NotesRichtextParseError as e:
        raise ToolError(f"Failed to parse note richtext: {e}")
    except NotesRichtextSerializeError as e:
        raise ToolError(f"Failed to serialize note richtext: {e}")
    except AccessibilityPermissionError:
        raise ToolError(
            "Accessibility permissions required for UI scripting. "
            f"Grant access in {ACCESSIBILITY_SETTINGS} "
            "for the terminal/app running this server."
        )
    except ClipboardError as e:
        raise ToolError(f"Clipboard operation failed: {e}")
    except SessionLockedError as e:
        raise ToolError(str(e))
    except NotesNotFrontmostError as e:
        # Verbatim: the safety message names the app keystrokes were refused to.
        raise ToolError(str(e))
    except WriteVerificationError as e:
        # Verbatim: tells the caller the write did not take (no false success).
        raise ToolError(str(e))
    except UIScriptingError as e:
        raise ToolError(f"UI scripting failed: {e}")
    except AppleScriptError as e:
        raise ToolError(f"Failed to toggle checklist item: {e}")
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


@mcp.tool()
@profiled("replace_note_link", detail_arg="name", attach_response=True)
@_live_only("replace_note_link")
def replace_note_link(
    name: str,
    old_target: Annotated[str, "Title or uuid of the note the link points at now"],
    new_target: Annotated[str, "Title or uuid of the note the link should point at"],
    replace_all: bool = False,
) -> dict:
    """
    Re-point an existing note link to a different note.

    In note `name`, every [[link]] to old_target is changed into a link to
    new_target. Nothing else in the note changes (text, formatting, other
    links). Use this instead of update_note_content(find_replace), which can't
    edit links: a link is a native attachment, not "[[Title]]" text.

    Targets may be a note title or a note uuid (as read_note_content shows in
    note_links):
    - old_target by title matches links to a live note with that title
      (case-insensitive). A link whose target note was deleted can only be
      matched by uuid.
    - new_target must be an existing note (not in Recently Deleted). If
      several notes share its title, pass its uuid.

    By default exactly one link may match; with more than one the call is
    refused unless replace_all=true, which re-points all of them. Links
    inside table cells are not supported (reported as not found).

    Automatically creates a backup of the Notes database before making changes.

    Args:
        name: The exact title of the note containing the link, or its id. Writes open notes by title, so a title several notes share is refused (rename one first), even by id
        old_target: Title or uuid of the note the link points at now
        new_target: Title or uuid of the note the link should point at
        replace_all: Re-point every matching link instead of exactly one

    Returns:
        JSON with old/new target uuids, links_replaced, verified (true once
        the Notes database shows the change; false if Notes hadn't saved it
        yet), and backup path
    """
    if not name or not name.strip():
        raise ToolError("Note name cannot be empty or whitespace-only")
    if not old_target or not old_target.strip():
        raise ToolError("old_target cannot be empty or whitespace-only")
    if not new_target or not new_target.strip():
        raise ToolError("new_target cannot be empty or whitespace-only")

    try:
        with NotesDatabase() as db:
            name = title_for_ref(db, name)  # an id addresses its note's title (#83)
            titled = db.count_notes_titled(name)
            if titled == 0:
                raise NoteNotFoundError(name)
            if titled > 1:
                raise RelinkError(
                    f"{titled} notes are titled '{name}' (case-insensitive, Recently "
                    "Deleted included); rename one first."
                )
            try:
                note = db.get_note_by_name(name)
            except NoteNotFoundError:
                raise RelinkError(
                    f"Note '{name}' not found under that exact title; use the note's exact "
                    "title (case-sensitive) as list_notes shows it."
                ) from None
            plan = plan_relink(db, note, old_target, new_target, replace_all)
            before_fp = db.note_fingerprint(note.pk)

        with span("backup"):
            backup_path = _perform_backup()

        args = (name, plan.old_uuid, plan.new_uuid, plan.new_title, plan.count)
        if _is_gui_session():
            with span("execute"):
                execute_replace_note_link(*args)
        else:
            logger.info(
                "replace_note_link: Remote session detected, routing through UI helper daemon"
            )
            with span("execute"):
                UIHelperClient().replace_note_link(*args)

        with span("verify"):
            verified = await_relinked(
                NotesDatabase, note.pk, plan, before_fingerprint=before_fp
            )

        with span("sync"):
            sync_nudged = nudge_icloud_sync()

        return {
            "success": True,
            "note_name": name,
            "old_target_uuid": plan.old_uuid,
            "new_target": plan.new_title,
            "new_target_uuid": plan.new_uuid,
            "links_replaced": plan.count,
            "verified": verified,
            "backup_path": backup_path,
            "sync_nudged": sync_nudged,
        }

    except RelinkError as e:
        raise ToolError(str(e))
    except BackupError as e:
        raise ToolError(f"Failed to create backup, relink aborted: {e}")
    except UIHelperNotAvailableError as e:
        raise _daemon_error(e) from e
    except NoteNotFoundError:
        raise ToolError(
            f"Note '{name}' not found. Use list_notes to see available notes."
        )
    except EncryptedNoteError:
        raise ToolError(f"Note '{name}' is encrypted and cannot be edited.")
    except ProtobufParseError as e:
        raise ToolError(f"Failed to parse note content: {e}") from e
    except NotesRichtextParseError as e:
        raise ToolError(f"Failed to parse note richtext: {e}") from e
    except NotesRichtextSerializeError as e:
        raise ToolError(f"Failed to serialize note richtext: {e}") from e
    except AccessibilityPermissionError:
        raise ToolError(
            "Accessibility permissions required for UI scripting. "
            f"Grant access in {ACCESSIBILITY_SETTINGS} "
            "for the terminal/app running this server."
        )
    except ClipboardError as e:
        raise ToolError(f"Clipboard operation failed: {e}") from e
    except SessionLockedError as e:
        raise ToolError(str(e))
    except NotesNotFrontmostError as e:
        # Verbatim: the safety message names the app keystrokes were refused to.
        raise ToolError(str(e))
    except WriteVerificationError as e:
        # Verbatim: tells the caller the write did not take (no false success).
        raise ToolError(str(e)) from e
    except UIScriptingError as e:
        raise ToolError(f"UI scripting failed: {e}") from e
    except AppleScriptError as e:
        raise ToolError(f"Failed to re-point link: {e}") from e
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


_SECTION_OPERATIONS = ("replace", "append", "find_replace", "delete")


def _blank(value: Optional[str]) -> bool:
    return value is None or not value.strip()


def _validate_section_args(
    operation: str,
    heading: Optional[str],
    section_text: Optional[str],
    body: Optional[str],
    old_text: Optional[str],
    new_text: Optional[str],
) -> None:
    if operation not in _SECTION_OPERATIONS:
        raise ToolError(
            f"Invalid operation '{operation}'. Use one of: {', '.join(_SECTION_OPERATIONS)}."
        )
    if (heading is None) == (section_text is None):
        raise ToolError("Pass exactly one of heading or section_text.")
    if heading is not None and _blank(heading):
        raise ToolError("heading cannot be empty or whitespace-only")
    if section_text is not None and _blank(section_text):
        raise ToolError("section_text cannot be empty or whitespace-only")
    if operation in ("replace", "append"):
        if _blank(body):
            raise ToolError(f"body is required for the {operation} operation")
        if old_text is not None or new_text is not None:
            raise ToolError(
                f"old_text/new_text are only valid for find_replace, not {operation}"
            )
    elif operation == "find_replace":
        if body is not None:
            raise ToolError(
                "body is not valid for find_replace; use old_text and new_text"
            )
        if _blank(old_text):
            raise ToolError("old_text is required for find_replace and cannot be blank")
        if new_text is None:
            raise ToolError(
                "new_text is required for find_replace (use '' to delete the text)"
            )
    else:  # delete
        if body is not None or old_text is not None or new_text is not None:
            raise ToolError("delete takes no body, old_text or new_text")


_SECTION_DAEMON_TIMEOUT = WRITE_TIMEOUT  # same budget as every daemon write (#143)


def _refuse_new_tags_and_tables(
    links: dict[str, LinkTarget], *markdown: Optional[str]
) -> None:
    """Refuse new {#tags} / tables before any backup or keystroke (v1 limit)."""
    text = "\n".join(m for m in markdown if m)
    if not text:
        return
    converted = markdown_to_richtext_new(text, link_resolver_for(links))
    if any(a.action_type in ("hashtag", "table") for a in converted.pending_actions):
        raise FragmentError(_TAGS_TABLES_REFUSAL)


@mcp.tool()
@profiled("edit_note_section", detail_arg="name", attach_response=True)
@_live_only("edit_note_section")
def edit_note_section(
    name: str,
    operation: Annotated[str, "replace, append, find_replace or delete"],
    heading: Annotated[Optional[str], "Heading text that names the section"] = None,
    section_text: Annotated[
        Optional[str],
        "Exact text of the section's first line(s), as read_note_content shows",
    ] = None,
    body: Annotated[Optional[str], "Markdown for replace/append"] = None,
    old_text: Annotated[
        Optional[str], "Text to find inside the section (find_replace)"
    ] = None,
    new_text: Annotated[Optional[str], "Replacement text (find_replace)"] = None,
) -> dict:
    """
    Edit one section of a note: the note is copied, the section changed, and the
    whole note pasted back.

    A section is a run of whole paragraphs. Name it with exactly one of:
    - heading: the section under that heading. It stops at the next heading of
      the same or a higher level and does not include the heading line itself.
      Pass the heading as read_note_content shows it minus the leading #s,
      keeping any inline markdown: `## **Plan**` is heading="**Plan**".
    - section_text: paragraphs matched by their exact text, in the notation
      read_note_content shows.

    Operations: replace (the section becomes `body`), append (`body` is added
    at the end of the section), find_replace (old_text -> new_text inside the
    section only; both required, new_text may be empty) and delete (removes the
    section's paragraphs; the heading stays). Trailing newlines in `body` are
    dropped for replace and append, so they never add a blank paragraph.

    Everything outside the section is checked afterwards for the same content
    (text, formatting, link targets, table cells, attachments); internal ids and
    timestamps change on every write. If any check fails the edit is undone with a
    single Cmd+Z and the call errors with rolled_back; if the undo can't be
    confirmed the error says so and gives the backup path. If the note changes
    while the edit is being prepared, nothing is written; retry.

    Limits (v1): no new {#tags} and no new tables in `body`/`new_text`; table
    paragraphs can't be matched by text; a [[link]] to a note that doesn't
    exist stays plain text; notes holding over 25 MB of images and files are
    refused, because every edit re-copies them (use update_note_content append
    there).

    Automatically creates a backup of the Notes database before making changes.

    Returns:
        JSON with the edited section's first_paragraph and paragraph_count,
        verified_outside_unchanged (content, not bytes), backup path and
        sync_nudged
    """
    if not name or not name.strip():
        raise ToolError("Note name cannot be empty or whitespace-only")
    _validate_section_args(operation, heading, section_text, body, old_text, new_text)

    try:
        with NotesDatabase() as db:
            name = title_for_ref(db, name)  # an id addresses its note's title (#83)
            titled = db.count_notes_titled(name)
            if titled == 0:
                raise NoteNotFoundError(name)
            if titled > 1:
                raise ToolError(
                    f"{titled} notes are titled '{name}' (case-insensitive, Recently "
                    "Deleted included); rename one first."
                )
            try:
                note = db.get_note_by_name(name)
            except NoteNotFoundError:
                raise ToolError(
                    f"Note '{name}' not found under that exact title; use the note's exact "
                    "title (case-sensitive) as list_notes shows it."
                ) from None
            refuse_heavy_media(db, note.pk, name)
            render = NoteContentReader().paragraph_renderer(note)
            snapshot = load_snapshot(db, note.pk, render)
            section = resolve_section(
                snapshot, name, heading=heading, section_text=section_text
            )
            plan_edit(snapshot, section, operation, name)
            links = _link_targets("\n".join(t for t in (body, new_text) if t), db)

        _refuse_new_tags_and_tables(links, body, new_text)

        with span("backup"):
            backup_path = _perform_backup()

        request = SectionRequest(
            name=name,
            pk=note.pk,
            operation=operation,
            heading=heading,
            section_text=section_text,
            body=body,
            old_text=old_text,
            new_text=new_text,
            links=links,
            expected_fingerprint=section.fingerprint,
            backup_path=backup_path,
        )
        if _is_gui_session():
            with span("execute"):
                result = execute_edit_note_section(request)
            first, count = result.first_paragraph, result.paragraph_count
        else:
            logger.info(
                "edit_note_section: Remote session detected, routing through UI helper daemon"
            )
            with span("execute"):
                data = UIHelperClient(
                    timeout=_SECTION_DAEMON_TIMEOUT
                ).edit_note_section(request)
            first, count = data["first_paragraph"], data["paragraph_count"]

        with span("sync"):
            sync_nudged = nudge_icloud_sync()

        return {
            "success": True,
            "note_name": name,
            "operation": operation,
            "section": {"first_paragraph": first, "paragraph_count": count},
            "verified_outside_unchanged": True,
            "backup_path": backup_path,
            "sync_nudged": sync_nudged,
        }

    except (SectionError, FragmentError) as e:
        # Caller-facing refusals (never system faults for the logger).
        raise ToolError(str(e)) from e
    except SectionEditRolledBackError as e:
        # FastMCP errors are text, so the flag travels in the message.
        raise ToolError(f"{e} (rolled_back=true: the note was restored)") from e
    except SectionEditDamagedError as e:
        # The engine already sent the logger event; mark the wrapper so the
        # profiler hook doesn't report the same failure twice.
        tool_error = ToolError(str(e))
        unified_log.mark_reported(tool_error)
        raise tool_error from e
    except BackupError as e:
        raise ToolError(f"Failed to create backup, edit aborted: {e}")
    except UIHelperTimeoutError as e:
        raise ToolError(
            f"The UI helper daemon didn't answer within {int(_SECTION_DAEMON_TIMEOUT)}s; "
            "the section edit may still complete. Re-read the note before retrying."
        ) from e
    except UIHelperNotAvailableError as e:
        raise _daemon_error(e) from e
    except NoteNotFoundError:
        raise ToolError(
            f"Note '{name}' not found. Use list_notes to see available notes."
        )
    except EncryptedNoteError:
        raise ToolError(f"Note '{name}' is encrypted and cannot be edited.")
    except ProtobufParseError as e:
        raise ToolError(f"Failed to parse note content: {e}") from e
    except NotesRichtextParseError as e:
        raise ToolError(f"Failed to parse note richtext: {e}") from e
    except NotesRichtextSerializeError as e:
        raise ToolError(f"Failed to serialize note richtext: {e}") from e
    except AccessibilityPermissionError:
        raise ToolError(
            "Accessibility permissions required for UI scripting. "
            f"Grant access in {ACCESSIBILITY_SETTINGS} "
            "for the terminal/app running this server."
        )
    except ClipboardError as e:
        raise ToolError(f"Clipboard operation failed: {e}") from e
    except SessionLockedError as e:
        raise ToolError(str(e))
    except NotesNotFrontmostError as e:
        raise ToolError(str(e))
    except WriteVerificationError as e:
        raise ToolError(str(e)) from e
    except UIScriptingError as e:
        raise ToolError(f"UI scripting failed: {e}") from e
    except AppleScriptError as e:
        raise ToolError(f"Failed to edit section: {e}") from e
    except DatabaseNotFoundError:
        raise ToolError(
            "Apple Notes database not found. Ensure Full Disk Access is granted."
        )


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
