"""Main API for reading Apple Notes content with resolved links."""

from typing import Optional, List, Dict, Tuple

from . import folder_scope
from .database import NotesDatabase
from .link_tokens import note_link_uuid
from .protobuf_parser import NoteProtobufParser
from .models import Note, NoteContent, NoteLink, ExternalLink, Attachment
from .exceptions import NoteNotFoundError


def _wrap_formatted_segment(
    segment: str,
    color_tag: Optional[str],
    is_strikethrough: bool,
    is_underline: bool,
    is_bold: bool,
    is_italic: bool,
) -> str:
    """Wrap a text segment in markdown markers, keeping edge whitespace outside.

    A formatting run can cover leading/trailing spaces, but the markdown
    parser's italic flanking rule (NTG-007) rejects markers that hug
    whitespace ('* word *'), so emit ' *word* ' to keep read→edit→write
    round-trips lossless. Caller guarantees segment has non-whitespace content.
    """
    leading = segment[: len(segment) - len(segment.lstrip())]
    trailing = segment[len(segment.rstrip()) :]
    formatted = segment.strip()
    # Color wrapping is innermost
    if color_tag:
        formatted = f"{{color:{color_tag}}}{formatted}{{/color}}"
    if is_strikethrough:
        formatted = f"~~{formatted}~~"
    if is_underline:
        formatted = f"<u>{formatted}</u>"
    if is_bold:
        formatted = f"**{formatted}**"
    if is_italic:
        formatted = f"*{formatted}*"
    return leading + formatted + trailing


class NoteContentReader:
    """
    Main API for reading note content with resolved links.

    Usage:
        reader = NoteContentReader()
        content = reader.get_note_content("My Note")
        print(content.resolved_text)  # Text with [[links]] resolved
    """

    INTERNAL_LINK_TYPE = "com.apple.notes.inlinetextattachment.link"

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize the reader.

        Args:
            db_path: Optional path to the SQLite database.
                     Uses default macOS location if not provided.
        """
        self.db = NotesDatabase(db_path)
        self.parser = NoteProtobufParser()
        self._uuid_lookup: Optional[Dict[str, Note]] = None
        self._hidden_uuids: Optional[set] = None

    def _get_uuid_lookup(self) -> Dict[str, Note]:
        """Get or build the UUID lookup dictionary."""
        if self._uuid_lookup is None:
            self._uuid_lookup = self.db.build_uuid_lookup()
        return self._uuid_lookup

    def _is_hidden(self, uuid: str) -> bool:
        """Whether a uuid is a live note the folder scope hides (looked up once)."""
        if not folder_scope.is_active():
            return False
        if self._hidden_uuids is None:
            self._hidden_uuids = self.db.hidden_note_uuids()
        return uuid in self._hidden_uuids

    def get_note_content(self, note_name: str) -> NoteContent:
        """
        Get note content with all links resolved.

        Args:
            note_name: The title of the note to read

        Returns:
            NoteContent with raw_text, resolved_text, links, and attachments

        Raises:
            NoteNotFoundError: If note with given name doesn't exist
        """
        with self.db:
            # Get note metadata
            note = self.db.get_note_by_name(note_name)
            return self._read_note_content(note)

    def get_note_content_for_note(self, note: Note) -> NoteContent:
        """
        Get content for an already-resolved Note.

        Addresses the note by pk instead of re-resolving its title, so it is
        safe when multiple notes share the same name (get_note_content would
        resolve one of them arbitrarily).
        """
        with self.db:
            return self._read_note_content(note)

    def _read_note_content(self, note: Note) -> NoteContent:
        """Build NoteContent for a resolved note (caller holds the db context)."""
        # Get raw data
        zdata = self.db.get_note_data(note.pk)
        if not zdata:
            return NoteContent(
                name=note.name,
                uuid=note.uuid,
                folder=note.folder,
                raw_text="",
                resolved_text="",
                created=note.created,
                modified=note.modified,
                last_opened=note.last_opened,
            )

        # Get attachments from database
        db_attachments = self.db.get_attachments_for_note(note.pk)

        # Get UUID lookup for resolving internal links
        uuid_lookup = self._get_uuid_lookup()

        # Parse protobuf
        note_text, attribute_runs = self.parser.parse(zdata)

        # Extract attachment infos, external links, checklist info, text formatting, and paragraph styles
        attachment_infos = self.parser.extract_attachment_infos(attribute_runs)
        external_link_infos = self.parser.extract_external_links(
            note_text, attribute_runs
        )
        checklist_infos = self.parser.extract_checklist_info(attribute_runs)
        text_format_infos = self.parser.extract_text_formatting(attribute_runs)
        paragraph_style_infos = self.parser.extract_paragraph_styles(attribute_runs)

        # Build result
        return self._build_note_content(
            note=note,
            note_text=note_text,
            attachment_infos=attachment_infos,
            external_link_infos=external_link_infos,
            checklist_infos=checklist_infos,
            text_format_infos=text_format_infos,
            paragraph_style_infos=paragraph_style_infos,
            db_attachments=db_attachments,
            uuid_lookup=uuid_lookup,
        )

    def paragraph_lines(self, note: Note) -> List[Optional[str]]:
        """One rendered line per raw paragraph (issue #184), as read_note_content
        prints it; None for a paragraph holding a table."""
        with self.db:
            return self._paragraph_lines(note)

    def paragraph_renderer(self, note: Note):
        """``render`` callable for ``sections.load_snapshot(db, pk, render=...)``."""
        return lambda pk: self.paragraph_lines(note)

    def _paragraph_lines(self, note: Note) -> List[Optional[str]]:
        """``paragraph_lines`` for an already-open db (the testable seam).

        Reuses the ``_build_note_content`` pass; the only mapping done here is
        consuming the extra lines a rendered table expands to.
        """
        content = self._read_note_content(note)
        raw_paragraphs = content.raw_text.split("\n")
        lines = content.resolved_text.split("\n")
        table_extra: Dict[int, int] = {}
        for att in content.attachments:
            if att.type_uti != "com.apple.notes.table":
                continue
            table_text = self._try_render_table(att.identifier)
            index = content.raw_text.count("\n", 0, att.position)
            table_extra[index] = table_extra.get(index, 0) + (
                table_text.count("\n") if table_text else 0
            )
        out: List[Optional[str]] = []
        pos = 0
        for i in range(len(raw_paragraphs)):
            count = 1 + table_extra.get(i, 0)
            chunk = lines[pos : pos + count]
            pos += count
            out.append(None if i in table_extra else "\n".join(chunk))
        if pos != len(lines):  # mapping disagrees with the render: refuse to guess
            return [None] * len(raw_paragraphs)
        return out

    def _build_note_content(
        self,
        note: Note,
        note_text: str,
        attachment_infos: list,
        external_link_infos: list,
        checklist_infos: list,
        text_format_infos: list,
        paragraph_style_infos: list,
        db_attachments: dict,
        uuid_lookup: dict,
    ) -> NoteContent:
        """Build NoteContent with resolved text."""
        # Find all U+FFFC positions
        fffc_positions = [i for i, c in enumerate(note_text) if c == "\ufffc"]

        # Build checklist line info - map line numbers to checked state
        checklist_lines = self._build_checklist_line_map(note_text, checklist_infos)

        # Build paragraph style line info - map line numbers to style type
        paragraph_style_lines = self._build_paragraph_style_line_map(
            note_text, paragraph_style_infos
        )

        # Build lists for the result
        note_links: List[NoteLink] = []
        external_links: List[ExternalLink] = []
        attachments: List[Attachment] = []

        # Collect all replacements
        replacements = []

        # Process FFFC attachments
        for i, pos in enumerate(fffc_positions):
            if i >= len(attachment_infos):
                break

            info = attachment_infos[i]

            if info.type_uti == self.INTERNAL_LINK_TYPE:
                # Internal note link
                db_info = db_attachments.get(info.identifier, {})
                target_uuid = note_link_uuid(db_info.get("target_url"))

                if target_uuid:
                    target_note = uuid_lookup.get(target_uuid)
                    if target_note:
                        note_links.append(
                            NoteLink(
                                target_note_name=target_note.name,
                                target_uuid=target_uuid,
                                position=pos,
                                alt_text=db_info.get("alt_text"),
                            )
                        )
                        replacements.append(
                            {
                                "position": pos,
                                "length": 1,
                                "text": f"[[{target_note.name}]]",
                            }
                        )
                    elif self._is_hidden(target_uuid):
                        # Live but out of scope: not broken, and its title stays hidden.
                        replacements.append(
                            {"position": pos, "length": 1, "text": "[[unknown link]]"}
                        )
                    else:
                        # Broken link
                        alt = db_info.get("alt_text", "unknown")
                        replacements.append(
                            {
                                "position": pos,
                                "length": 1,
                                "text": f"[[broken link: {alt}]]",
                            }
                        )
                else:
                    replacements.append(
                        {"position": pos, "length": 1, "text": "[[unknown link]]"}
                    )
            else:
                # Other attachment
                attachments.append(
                    Attachment(
                        identifier=info.identifier,
                        type_uti=info.type_uti,
                        position=pos,
                        alt_text=db_attachments.get(info.identifier, {}).get(
                            "alt_text"
                        ),
                    )
                )

                # Try to render table content
                if info.type_uti == "com.apple.notes.table":
                    table_text = self._try_render_table(info.identifier)
                    if table_text:
                        replacements.append(
                            {"position": pos, "length": 1, "text": table_text}
                        )
                        continue

                # Render tags in the explicit {#tag} write syntax (issue #100) so a
                # read -> edit -> write round-trip preserves them. alt_text is the
                # raw "#DailyTodo"; wrap it as "{#DailyTodo}".
                if info.type_uti == "com.apple.notes.inlinetextattachment.hashtag":
                    alt_text = db_attachments.get(info.identifier, {}).get("alt_text")
                    if alt_text:
                        replacements.append(
                            {"position": pos, "length": 1, "text": f"{{{alt_text}}}"}
                        )
                        continue

                label = self._get_attachment_label(info.type_uti)
                replacements.append(
                    {"position": pos, "length": 1, "text": f"[{label}]"}
                )

        # Process external links
        for link_info in external_link_infos:
            external_links.append(
                ExternalLink(
                    url=link_info.url,
                    text=link_info.text,
                    position=link_info.position,
                    length=link_info.length,
                )
            )
            replacements.append(
                {
                    "position": link_info.position,
                    "length": link_info.length,
                    "text": f"[{link_info.text}]({link_info.url})",
                }
            )

        # Build set of positions already covered by other replacements
        covered_positions = set()
        for r in replacements:
            for i in range(r["position"], r["position"] + r["length"]):
                covered_positions.add(i)

        # Process text formatting (bold, italic, strikethrough)
        # Consolidate adjacent runs with same formatting
        format_replacements = self._build_format_replacements(
            note_text, text_format_infos, covered_positions
        )
        replacements.extend(format_replacements)

        # Adjust line maps for replacements that change line counts (e.g., tables)
        replacements.sort(key=lambda x: x["position"])
        checklist_lines, paragraph_style_lines = self._adjust_line_maps(
            note_text, replacements, checklist_lines, paragraph_style_lines
        )

        # Apply replacements from end to start
        resolved_text = note_text
        replacements.sort(key=lambda x: x["position"], reverse=True)
        for r in replacements:
            pos = r["position"]
            length = r["length"]
            resolved_text = (
                resolved_text[:pos] + r["text"] + resolved_text[pos + length :]
            )

        # Apply paragraph style prefixes (headings, lists, monospace)
        resolved_text = self._apply_paragraph_styles(
            resolved_text, paragraph_style_lines, checklist_lines
        )

        # Apply checklist prefixes (after paragraph styles so they don't conflict)
        resolved_text = self._apply_checklist_prefixes(resolved_text, checklist_lines)

        return NoteContent(
            name=note.name,
            uuid=note.uuid,
            folder=note.folder,
            raw_text=note_text,
            resolved_text=resolved_text,
            note_links=note_links,
            external_links=external_links,
            attachments=attachments,
            created=note.created,
            modified=note.modified,
            last_opened=note.last_opened,
        )

    def _try_render_table(self, identifier: str) -> str | None:
        """Try to render a table attachment as a markdown table.

        Args:
            identifier: The table attachment's ZIDENTIFIER

        Returns:
            Markdown table string, or None if parsing fails
        """
        from .table_parser import parse_table_crdt, format_table_as_markdown

        table_data = self.db.get_table_data(identifier)
        if not table_data:
            return None

        grid = parse_table_crdt(table_data)
        if not grid:
            return None

        return format_table_as_markdown(grid)

    def _get_attachment_label(self, type_uti: str) -> str:
        """Get a human-readable label for an attachment type."""
        type_map = {
            "com.apple.notes.table": "table",
            "com.apple.drawing.2": "drawing",
            "com.apple.notes.inlinetextattachment.hashtag": "hashtag",
            "com.apple.notes.inlinetextattachment.mention": "mention",
            "public.jpeg": "image",
            "public.png": "image",
            "public.heic": "image",
            "public.url": "url",
            "com.apple.notes.gallery": "gallery",
        }

        if type_uti in type_map:
            return type_map[type_uti]

        if type_uti and type_uti.startswith("public."):
            return "file"

        return type_uti or "attachment"

    def get_all_notes(self) -> List[Note]:
        """Get all notes (metadata only)."""
        with self.db:
            return self.db.get_all_notes()

    def get_notes_paginated(
        self,
        folder: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Note], int]:
        """
        Get notes with pagination, handling folder filtering correctly.

        Args:
            folder: Optional folder name to filter notes
            limit: Maximum number of notes to return
            offset: Number of notes to skip

        Returns:
            Tuple of (notes, total_count)
        """
        with self.db:
            if folder:
                # Must fetch all, filter, then paginate
                # because folder paths are computed in Python
                all_notes = self.db.get_all_notes()
                folder_lower = folder.lower()
                filtered = [n for n in all_notes if folder_lower in n.folder.lower()]
                total = len(filtered)

                # Apply offset/limit
                start = offset or 0
                end = start + limit if limit else None
                return filtered[start:end], total
            else:
                # Can use SQL-level pagination (more efficient)
                return self.db.get_notes_paginated(limit, offset)

    def _build_paragraph_style_line_map(
        self, note_text: str, paragraph_style_infos: list
    ) -> Dict[int, int]:
        """
        Build a map of line numbers to their paragraph style type.

        Args:
            note_text: The original note text
            paragraph_style_infos: List of ParagraphStyleInfo from protobuf parser

        Returns:
            Dict mapping line number (0-indexed) to style type
        """
        if not paragraph_style_infos:
            return {}

        # Build position-to-line-number lookup
        line_starts = [0]
        for i, char in enumerate(note_text):
            if char == "\n":
                line_starts.append(i + 1)

        def get_line_number(pos: int) -> int:
            """Get line number for a position."""
            for i, start in enumerate(line_starts):
                if i + 1 >= len(line_starts) or pos < line_starts[i + 1]:
                    return i
            return len(line_starts) - 1

        # Map line numbers to style type (first wins)
        style_lines: Dict[int, int] = {}

        for info in paragraph_style_infos:
            line_num = get_line_number(info.position)
            if line_num not in style_lines:
                style_lines[line_num] = info.style_type

        return style_lines

    def _apply_paragraph_styles(
        self,
        text: str,
        paragraph_style_lines: Dict[int, int],
        checklist_lines: Optional[Dict[int, bool]] = None,
    ) -> str:
        """
        Apply paragraph style prefixes (headings, lists, monospace).

        Args:
            text: The text to modify
            paragraph_style_lines: Dict mapping line number to style type
            checklist_lines: Optional dict of checklist line numbers to skip
                (those lines get their own prefix from _apply_checklist_prefixes)

        Returns:
            Text with paragraph style prefixes added
        """
        if checklist_lines is None:
            checklist_lines = {}
        if not paragraph_style_lines:
            return text

        # Style type to prefix mapping
        # 0 = title (note title - use single #)
        # 1 = heading
        # 2 = subheading
        # 4 = monospace (we'll wrap in backticks)
        # 101 = dashed list
        # 102 = numbered list (handled specially for incrementing)
        # 104 = bullet list (stored style type 100)
        # 105 = block quote (virtual style for field 2.8 = 1)
        style_prefixes = {
            0: "# ",  # Title
            1: "## ",  # Heading
            2: "### ",  # Subheading
            101: "- ",  # Dashed list
            104: "* ",  # Bullet list
            105: "> ",  # Block quote
        }

        # List styles that should only apply to non-empty lines
        list_styles = {101, 102, 104}  # dashed, numbered, bullet

        lines = text.split("\n")
        result_lines = []
        numbered_list_counter = 0  # Track current number for numbered lists

        for i, line in enumerate(lines):
            if i in paragraph_style_lines:
                style_type = paragraph_style_lines[i]

                # Skip lines that are checklist items — they get their own prefix
                if i in checklist_lines:
                    result_lines.append(line)
                    continue

                # Skip list prefixes for empty lines
                if style_type in list_styles and not line.strip():
                    result_lines.append(line)
                    # Reset numbered list counter on empty line
                    if style_type == 102:
                        numbered_list_counter = 0
                elif style_type == 4:  # Monospace
                    # Wrap in backticks (only if non-empty)
                    if line.strip():
                        result_lines.append(f"`{line}`")
                    else:
                        result_lines.append(line)
                elif style_type == 102:  # Numbered list
                    numbered_list_counter += 1
                    result_lines.append(f"{numbered_list_counter}. {line}")
                elif style_type in style_prefixes:
                    prefix = style_prefixes[style_type]
                    result_lines.append(prefix + line)
                else:
                    result_lines.append(line)
            else:
                result_lines.append(line)
                # Reset numbered list counter when not in a list
                numbered_list_counter = 0

        return "\n".join(result_lines)

    def _build_checklist_line_map(
        self, note_text: str, checklist_infos: list
    ) -> Dict[int, bool]:
        """
        Build a map of line numbers to their checked state.

        Args:
            note_text: The original note text
            checklist_infos: List of ChecklistInfo from protobuf parser

        Returns:
            Dict mapping line number (0-indexed) to is_checked boolean
        """
        if not checklist_infos:
            return {}

        # Build position-to-line-number lookup
        line_starts = [0]
        for i, char in enumerate(note_text):
            if char == "\n":
                line_starts.append(i + 1)

        def get_line_number(pos: int) -> int:
            """Get line number for a position."""
            for i, start in enumerate(line_starts):
                if i + 1 >= len(line_starts) or pos < line_starts[i + 1]:
                    return i
            return len(line_starts) - 1

        # Map line numbers to checked state
        # If any part of a line is a checklist item, the whole line is
        # Use the checked state from the first checklist run on that line
        checklist_lines: Dict[int, bool] = {}

        for info in checklist_infos:
            line_num = get_line_number(info.position)
            # Only set if not already set (first wins)
            if line_num not in checklist_lines:
                checklist_lines[line_num] = info.is_checked

        return checklist_lines

    def _adjust_line_maps(
        self,
        note_text: str,
        replacements: list[dict],
        checklist_lines: Dict[int, bool],
        paragraph_style_lines: Dict[int, int],
    ) -> tuple[Dict[int, bool], Dict[int, int]]:
        """Adjust line-number maps for replacements that change line counts.

        When a single-character table placeholder (U+FFFC) is replaced with a
        multi-line markdown table, every line after it shifts down. This method
        computes the cumulative offset and remaps the line-number dictionaries.

        Args:
            note_text: The original note text (before any replacements)
            replacements: List of replacement dicts sorted ascending by position
            checklist_lines: Original checklist line map
            paragraph_style_lines: Original paragraph style line map

        Returns:
            Tuple of (adjusted_checklist_lines, adjusted_paragraph_style_lines)
        """
        if not replacements:
            return checklist_lines, paragraph_style_lines

        # Compute line-count delta for each replacement
        deltas = []
        for r in replacements:
            pos = r["position"]
            original_span = note_text[pos : pos + r["length"]]
            original_newlines = original_span.count("\n")
            replacement_newlines = r["text"].count("\n")
            delta = replacement_newlines - original_newlines
            if delta != 0:
                deltas.append((pos, delta))

        if not deltas:
            return checklist_lines, paragraph_style_lines

        # Build position-to-line-number lookup for the original text
        line_starts = [0]
        for i, char in enumerate(note_text):
            if char == "\n":
                line_starts.append(i + 1)

        def get_line_for_pos(pos: int) -> int:
            for i, start in enumerate(line_starts):
                if i + 1 >= len(line_starts) or pos < line_starts[i + 1]:
                    return i
            return len(line_starts) - 1

        # For each delta, compute which original line it affects
        delta_by_line = []
        for pos, delta in deltas:
            line = get_line_for_pos(pos)
            delta_by_line.append((line, delta))

        def remap(original_map: dict) -> dict:
            new_map = {}
            for orig_line, value in original_map.items():
                offset = sum(d for (l, d) in delta_by_line if l < orig_line)
                new_map[orig_line + offset] = value
            return new_map

        return remap(checklist_lines), remap(paragraph_style_lines)

    def _build_format_replacements(
        self, note_text: str, text_format_infos: list, covered_positions: set
    ) -> List[dict]:
        """
        Build text formatting replacements, consolidating adjacent runs with same formatting.

        Args:
            note_text: The original note text
            text_format_infos: List of TextFormatInfo from protobuf parser
            covered_positions: Set of positions already covered by other replacements

        Returns:
            List of replacement dicts
        """
        if not text_format_infos:
            return []

        replacements = []

        # Group consecutive runs with same formatting
        i = 0
        while i < len(text_format_infos):
            fmt = text_format_infos[i]

            # Skip if overlaps with existing replacements
            if any(
                p in covered_positions
                for p in range(fmt.position, fmt.position + fmt.length)
            ):
                i += 1
                continue

            # Find all consecutive runs with the same formatting
            start_pos = fmt.position
            end_pos = fmt.position + fmt.length
            is_bold = fmt.is_bold
            is_italic = fmt.is_italic
            is_underline = fmt.is_underline
            is_strikethrough = fmt.is_strikethrough
            color = fmt.color

            j = i + 1
            while j < len(text_format_infos):
                next_fmt = text_format_infos[j]

                # Check if adjacent (next run starts where current ends)
                if next_fmt.position != end_pos:
                    break

                # Check if same formatting
                if (
                    next_fmt.is_bold != is_bold
                    or next_fmt.is_italic != is_italic
                    or next_fmt.is_underline != is_underline
                    or next_fmt.is_strikethrough != is_strikethrough
                    or next_fmt.color != color
                ):
                    break

                # Check if overlaps with covered positions
                if any(
                    p in covered_positions
                    for p in range(
                        next_fmt.position, next_fmt.position + next_fmt.length
                    )
                ):
                    break

                # Extend the range
                end_pos = next_fmt.position + next_fmt.length
                j += 1

            # Get the consolidated text
            text = note_text[start_pos:end_pos]

            # Skip empty or whitespace-only
            if not text.strip():
                i = j
                continue

            # Color is already resolved to a name/hex string by protobuf_parser
            color_tag = color

            # Handle newlines by formatting each line segment separately
            # This prevents markdown from breaking across lines
            if "\n" in text:
                # Split into line segments and format each
                current_pos = start_pos
                segments = text.split("\n")
                for k, segment in enumerate(segments):
                    if segment.strip():  # Only format non-empty segments
                        formatted = _wrap_formatted_segment(
                            segment,
                            color_tag,
                            is_strikethrough,
                            is_underline,
                            is_bold,
                            is_italic,
                        )

                        if formatted != segment:
                            replacements.append(
                                {
                                    "position": current_pos,
                                    "length": len(segment),
                                    "text": formatted,
                                }
                            )
                    current_pos += len(segment) + 1  # +1 for the newline
            else:
                # No newlines, format the whole text
                formatted = _wrap_formatted_segment(
                    text,
                    color_tag,
                    is_strikethrough,
                    is_underline,
                    is_bold,
                    is_italic,
                )

                if formatted != text:
                    replacements.append(
                        {
                            "position": start_pos,
                            "length": end_pos - start_pos,
                            "text": formatted,
                        }
                    )

            i = j

        return replacements

    def _apply_checklist_prefixes(
        self, text: str, checklist_lines: Dict[int, bool]
    ) -> str:
        """
        Apply checklist prefixes to lines that are checklist items.

        Args:
            text: The text to modify
            checklist_lines: Dict mapping line number to is_checked

        Returns:
            Text with checklist prefixes added
        """
        if not checklist_lines:
            return text

        lines = text.split("\n")
        result_lines = []

        for i, line in enumerate(lines):
            if i in checklist_lines:
                is_checked = checklist_lines[i]
                prefix = "- [x] " if is_checked else "- [ ] "
                result_lines.append(prefix + line)
            else:
                result_lines.append(line)

        return "\n".join(result_lines)
