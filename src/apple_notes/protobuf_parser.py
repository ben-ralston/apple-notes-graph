"""Protobuf parsing for Apple Notes content."""

import gzip
from typing import List, Tuple, Optional
from dataclasses import dataclass

try:
    import blackboxprotobuf
except ImportError:
    raise ImportError(
        "bbpb is required (provides the blackboxprotobuf module). "
        "Install with: pip install bbpb"
    )

from .exceptions import EncryptedNoteError, ProtobufParseError

# A password-protected note's ZICNOTEDATA.ZDATA is not gzip: Notes stores a
# `bplist00` blob (with ZCRYPTOTAG set) that we cannot decrypt (issue #126).
ENCRYPTED_ZDATA_SIGNATURE = b"bplist"


@dataclass
class AttachmentInfo:
    """Attachment info extracted from protobuf."""

    identifier: str
    type_uti: str


@dataclass
class ExternalLinkInfo:
    """External link info extracted from protobuf.

    Note: ``text`` and ``length`` reflect the display text only (trailing
    newlines stripped).  ``position`` is the byte offset in the original
    note text.  ``position + length`` may NOT equal the start of the next
    protobuf run when a trailing newline was stripped.
    """

    url: str
    text: str
    position: int
    length: int


@dataclass
class ChecklistInfo:
    """Checklist/todo info for a text range."""

    position: int
    length: int
    is_checked: bool


@dataclass
class TextFormatInfo:
    """Text formatting info for a text range."""

    position: int
    length: int
    is_bold: bool = False
    is_italic: bool = False
    is_strikethrough: bool = False
    is_underline: bool = False
    color: str | None = None  # Color name (e.g. "red") or hex (e.g. "#FF3B30")


@dataclass
class ParagraphStyleInfo:
    """Paragraph style info for a text range."""

    position: int
    length: int
    style_type: int  # 0=title, 1=heading, 2=subheading, 4=monospace, 101=dashed, 102=numbered, 104=bullet, 105=quote


def _text_typedef() -> dict:
    """Decode hint pinning only the note text field; built fresh per call
    because decode_message fills guessed fields into it.

    Pinned as bytes, not string: a nested string pin that fails on invalid
    UTF-8 makes blackboxprotobuf silently re-key the parent ("3-1") and the
    text vanishes. Bytes can't fail; ``parse`` decodes them with
    errors="replace", as before the pin."""
    text = {"type": "bytes", "name": ""}
    note = {"type": "message", "message_typedef": {"2": text}, "name": ""}
    document = {"type": "message", "message_typedef": {"3": note}, "name": ""}
    return {"2": document}


class NoteProtobufParser:
    """Parse Apple Notes protobuf format."""

    # Field numbers from notestore.proto
    FIELD_DOCUMENT = "2"
    FIELD_NOTE = "3"
    FIELD_NOTE_TEXT = "2"
    FIELD_ATTRIBUTE_RUNS = "5"
    FIELD_RUN_LENGTH = "1"
    FIELD_RUN_LINK = "9"
    FIELD_RUN_ATTACHMENT_INFO = "12"
    FIELD_ATTACHMENT_IDENTIFIER = "1"
    FIELD_ATTACHMENT_TYPE_UTI = "2"

    # Paragraph style fields (field 2 in attribute run)
    FIELD_PARAGRAPH_STYLE = "2"
    FIELD_CHECKLIST = "5"  # Checklist info within paragraph style
    FIELD_CHECKLIST_CHECKED = "2"  # 0 = unchecked, 1 = checked

    # Text formatting fields (outer level in attribute run)
    # Field 5 is font style: 1 = bold, 2 = italic
    FIELD_FONT_STYLE = "5"
    FONT_STYLE_BOLD = 1
    FONT_STYLE_ITALIC = 2
    FIELD_UNDERLINE = "6"  # 1 = underline
    FIELD_STRIKETHROUGH = "7"  # 1 = strikethrough

    # Color fields
    FIELD_COLOR_INDEX = "14"  # Palette index for text color
    FIELD_COLOR_RGB = "10"  # RGB sub-fields (field 10) - fallback

    # Apple Notes highlight palette indices → color names.
    # Only indices 1-5 are valid. Indices 6-9 render incorrectly and crash
    # Apple Notes when the formatting (Aa) menu is opened.
    COLOR_INDEX_MAP = {
        1: "purple",
        2: "pink",
        3: "orange",
        4: "green",
        5: "blue",
    }

    # Paragraph style type field (field 2.1)
    FIELD_PARAGRAPH_TYPE = "1"  # Within field 2
    # Paragraph style sub-fields. Field 3 is NOT a bullet flag: Notes sets it on
    # most paragraphs (dashed/numbered/checklist items, a table's line, plain
    # text after a table), and reading it as one added stray "* " (#235).
    FIELD_BLOCK_QUOTE = "8"  # Within field 2, value 1 = block quote

    # Paragraph style type values
    STYLE_TITLE = 0
    STYLE_HEADING = 1
    STYLE_SUBHEADING = 2
    STYLE_MONOSPACE = 4
    STYLE_STORED_BULLET = 100  # Stored style type of a bulleted ("dotted") list
    STYLE_DASHED_LIST = 101
    STYLE_NUMBERED_LIST = 102
    STYLE_CHECKLIST = 103  # Already handled separately
    STYLE_BULLET_LIST = 104  # How a stored 100 is reported (prefix "* ")
    STYLE_BLOCK_QUOTE = 105  # Virtual style for field 2.8 = 1

    def parse(self, compressed_data: bytes) -> Tuple[str, List[dict]]:
        """
        Parse compressed protobuf data.

        Args:
            compressed_data: Gzip-compressed protobuf data from ZICNOTEDATA.ZDATA

        Returns:
            Tuple of (note_text, attribute_runs)

        Raises:
            EncryptedNoteError: If the note is password-protected (locked)
            ProtobufParseError: If parsing fails
        """
        # Detect a locked note before gzip so it surfaces as EncryptedNoteError,
        # not as a misleading "Not a gzipped file (b'bp')" parse failure.
        if compressed_data.startswith(ENCRYPTED_ZDATA_SIGNATURE):
            raise EncryptedNoteError(
                "Note is password-protected (locked); its content cannot be read"
            )

        # Decompress gzip
        try:
            decompressed = gzip.decompress(compressed_data)
        except Exception as e:
            raise ProtobufParseError(f"Failed to decompress data: {e}")

        # Parse protobuf
        try:
            # Pin the note text (2 -> 3 -> 2); unpinned, blackboxprotobuf
            # decodes some short texts ("Empty") as nested messages
            # (issue #184, R12).
            message, typedef = blackboxprotobuf.decode_message(
                decompressed, _text_typedef()
            )
        except Exception as e:
            raise ProtobufParseError(f"Failed to parse protobuf: {e}")

        # Navigate the protobuf structure
        try:
            document = message.get(self.FIELD_DOCUMENT, {})
            note = document.get(self.FIELD_NOTE, {})
            note_text = note.get(self.FIELD_NOTE_TEXT, "")

            # Decode bytes to string if needed
            if isinstance(note_text, bytes):
                note_text = note_text.decode("utf-8", errors="replace")

            attribute_runs = note.get(self.FIELD_ATTRIBUTE_RUNS, [])
            # Ensure it's a list
            if not isinstance(attribute_runs, list):
                attribute_runs = [attribute_runs] if attribute_runs else []

            self._normalize_run_lengths_to_codepoints(note_text, attribute_runs)

            return (note_text, attribute_runs)
        except Exception as e:
            raise ProtobufParseError(f"Failed to extract note data: {e}")

    def _normalize_run_lengths_to_codepoints(
        self, note_text: str, attribute_runs: List[dict]
    ) -> None:
        """Convert run lengths from UTF-16 code units to Unicode code points.

        Apple Notes stores attribute-run lengths in UTF-16 code units, but
        `note_text` is a Python str indexed in code points — the two differ by
        one for every non-BMP character (surrogate pair). Walking the text
        once, this rewrites each run's length field so `position += length`
        arithmetic in the extract_* helpers stays aligned with the text
        (mirrors the write path's RichTextParser normalization from #102).

        Run boundaries fall on character boundaries, so a run never splits a
        surrogate pair. Pure-BMP text is a no-op. If the stored lengths don't
        line up with the text (corrupt data), all lengths are left untouched.
        """
        runs = [run for run in attribute_runs if isinstance(run, dict)]
        if not runs:
            return

        cp_index = 0
        text_len = len(note_text)
        converted: list[int] = []
        for run in runs:
            utf16_remaining = self.get_run_length(run)
            cp_count = 0
            while utf16_remaining > 0 and cp_index < text_len:
                utf16_remaining -= 2 if ord(note_text[cp_index]) > 0xFFFF else 1
                cp_index += 1
                cp_count += 1
            if utf16_remaining != 0:
                # Boundary didn't line up — leave originals untouched.
                return
            converted.append(cp_count)

        for run, cp_count in zip(runs, converted):
            run[self.FIELD_RUN_LENGTH] = cp_count

    def extract_attachment_infos(
        self, attribute_runs: List[dict]
    ) -> List[AttachmentInfo]:
        """
        Extract all attachment infos from attribute runs in order.

        Args:
            attribute_runs: List of attribute run dicts from protobuf

        Returns:
            List of AttachmentInfo in order of appearance
        """
        infos = []
        for run in attribute_runs:
            if not isinstance(run, dict):
                continue

            attachment_info = run.get(self.FIELD_RUN_ATTACHMENT_INFO)
            if attachment_info and isinstance(attachment_info, dict):
                att_id = attachment_info.get(self.FIELD_ATTACHMENT_IDENTIFIER, "")
                if isinstance(att_id, bytes):
                    att_id = att_id.decode("utf-8", errors="replace")

                type_uti = attachment_info.get(self.FIELD_ATTACHMENT_TYPE_UTI, "")
                if isinstance(type_uti, bytes):
                    type_uti = type_uti.decode("utf-8", errors="replace")

                infos.append(AttachmentInfo(identifier=att_id, type_uti=type_uti))

        return infos

    def extract_external_links(
        self, note_text: str, attribute_runs: List[dict]
    ) -> List[ExternalLinkInfo]:
        """
        Extract external URL links from attribute runs.

        Args:
            note_text: The note text content
            attribute_runs: List of attribute run dicts from protobuf

        Returns:
            List of ExternalLinkInfo
        """
        links = []
        position = 0

        for run in attribute_runs:
            if not isinstance(run, dict):
                continue

            length = run.get(self.FIELD_RUN_LENGTH, 0)
            if isinstance(length, bytes):
                length = int.from_bytes(length, "little")

            # Field 9 is the link URL for external hyperlinks
            link_url = run.get(self.FIELD_RUN_LINK)
            if link_url:
                if isinstance(link_url, bytes):
                    link_url = link_url.decode("utf-8", errors="replace")

                # Get the link text from the note_text
                link_text = note_text[position : position + length]

                # Strip trailing newline — run length may include the line-ending \n
                link_length = length
                if link_text.endswith("\n"):
                    link_text = link_text[:-1]
                    link_length -= 1

                links.append(
                    ExternalLinkInfo(
                        url=link_url,
                        text=link_text,
                        position=position,
                        length=link_length,
                    )
                )

            position += length

        return links

    def get_run_length(self, run: dict) -> int:
        """Extract length from an attribute run."""
        length = run.get(self.FIELD_RUN_LENGTH, 0)
        if isinstance(length, bytes):
            length = int.from_bytes(length, "little")
        return length

    def extract_checklist_info(self, attribute_runs: List[dict]) -> List[ChecklistInfo]:
        """
        Extract checklist/todo info from attribute runs.

        Args:
            attribute_runs: List of attribute run dicts from protobuf

        Returns:
            List of ChecklistInfo indicating which text ranges are checklist items
        """
        checklist_items = []
        position = 0

        for run in attribute_runs:
            if not isinstance(run, dict):
                continue

            length = self.get_run_length(run)

            # Check for paragraph style with checklist info
            paragraph_style = run.get(self.FIELD_PARAGRAPH_STYLE)
            if paragraph_style and isinstance(paragraph_style, dict):
                checklist = paragraph_style.get(self.FIELD_CHECKLIST)
                if checklist and isinstance(checklist, dict):
                    checked_value = checklist.get(self.FIELD_CHECKLIST_CHECKED, 0)
                    is_checked = checked_value == 1

                    checklist_items.append(
                        ChecklistInfo(
                            position=position, length=length, is_checked=is_checked
                        )
                    )

            position += length

        return checklist_items

    def extract_text_formatting(
        self, attribute_runs: List[dict]
    ) -> List[TextFormatInfo]:
        """
        Extract text formatting (bold, italic, strikethrough) from attribute runs.

        Args:
            attribute_runs: List of attribute run dicts from protobuf

        Returns:
            List of TextFormatInfo for runs that have formatting
        """
        format_infos = []
        position = 0

        for run in attribute_runs:
            if not isinstance(run, dict):
                continue

            length = self.get_run_length(run)

            # Check for formatting fields
            # Field 5 is a font-style bitmask: 1 = bold, 2 = italic (3 = both)
            font_style = run.get(self.FIELD_FONT_STYLE)
            has_font_style = isinstance(font_style, int)
            is_bold = has_font_style and bool(font_style & self.FONT_STYLE_BOLD)
            is_italic = has_font_style and bool(font_style & self.FONT_STYLE_ITALIC)
            is_underline = run.get(self.FIELD_UNDERLINE) == 1
            is_strikethrough = run.get(self.FIELD_STRIKETHROUGH) == 1

            # Color: check field 14 (palette index) first, fall back to field 10 (RGB)
            color = None
            color_index = run.get(self.FIELD_COLOR_INDEX)
            if isinstance(color_index, int) and color_index in self.COLOR_INDEX_MAP:
                color = self.COLOR_INDEX_MAP[color_index]
            else:
                # Fallback: field 10 with RGB sub-fields (uint32 IEEE 754 float32)
                color_data = run.get(self.FIELD_COLOR_RGB)
                if color_data and isinstance(color_data, dict):
                    from .colors import (
                        rgb_ints_to_floats,
                        rgb_to_color_name,
                        rgb_floats_to_hex,
                    )

                    r = color_data.get("1", 0)
                    g = color_data.get("2", 0)
                    b = color_data.get("3", 0)
                    r_f, g_f, b_f = rgb_ints_to_floats(r, g, b)
                    name = rgb_to_color_name(r_f, g_f, b_f, highlight_only=True)
                    color = name if name else rgb_floats_to_hex(r_f, g_f, b_f)

            if is_bold or is_italic or is_underline or is_strikethrough or color:
                format_infos.append(
                    TextFormatInfo(
                        position=position,
                        length=length,
                        is_bold=is_bold,
                        is_italic=is_italic,
                        is_underline=is_underline,
                        is_strikethrough=is_strikethrough,
                        color=color,
                    )
                )

            position += length

        return format_infos

    def extract_paragraph_styles(
        self, attribute_runs: List[dict]
    ) -> List[ParagraphStyleInfo]:
        """
        Extract paragraph style info from attribute runs.

        Args:
            attribute_runs: List of attribute run dicts from protobuf

        Returns:
            List of ParagraphStyleInfo for runs that have special paragraph styles
        """
        style_infos = []
        position = 0

        for run in attribute_runs:
            if not isinstance(run, dict):
                continue

            length = self.get_run_length(run)

            # Check for paragraph style
            paragraph_style = run.get(self.FIELD_PARAGRAPH_STYLE)
            if paragraph_style and isinstance(paragraph_style, dict):
                style_type = paragraph_style.get(self.FIELD_PARAGRAPH_TYPE)
                if style_type == self.STYLE_STORED_BULLET:
                    style_type = self.STYLE_BULLET_LIST

                if paragraph_style.get(self.FIELD_BLOCK_QUOTE) == 1:
                    # Block quote (field 2.8 = 1) wins, as in the clipboard
                    # parser and sections._style_of
                    style_infos.append(
                        ParagraphStyleInfo(
                            position=position,
                            length=length,
                            style_type=self.STYLE_BLOCK_QUOTE,
                        )
                    )
                elif isinstance(style_type, int) and style_type != self.STYLE_CHECKLIST:
                    # Heading, subheading, monospace, dashed, numbered, bullet
                    style_infos.append(
                        ParagraphStyleInfo(
                            position=position, length=length, style_type=style_type
                        )
                    )

            position += length

        return style_infos
