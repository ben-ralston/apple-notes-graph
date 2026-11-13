"""Parser for Apple Notes richtext format (com.apple.notes.richtext).

The format is an NSKeyedArchive (binary plist) containing a protobuf payload
in the attributedStringData.NS.data field.
"""

import copy
import plistlib

import blackboxprotobuf
from blackboxprotobuf.lib.exceptions import DecoderException

from .models import (
    Attachment,
    AttributeRun,
    FontStyle,
    NotesRichtext,
    ParagraphStyle,
    ParagraphType,
)


def _message(fields: dict) -> dict:
    return {"type": "message", "message_typedef": fields}


# Types for the length-delimited fields this package reads (issue #198).
# Without a typedef, blackboxprotobuf guesses each field's type from its
# bytes, and in a repeated field the FIRST entry fixes the type for all of
# them. A paragraph UUID is 16 random bytes and about 1.4% of those parse as
# a protobuf message, so a note whose first paragraph UUID did came back with
# a dict there and could not be parsed. Short text such as "Hi" is valid
# protobuf too. Each type here is what guessing gives on ordinary Notes data,
# so notes that parsed before decode exactly as before. Every other field is
# still guessed; varints and fixed-width fields have only one possible type.
PINNED_TYPEDEF = {
    "2": {"type": "string"},  # note text
    "5": _message(  # attribute runs
        {
            "2": _message(  # paragraph style
                {
                    "5": _message({"1": {"type": "bytes"}}),  # checklist uuid
                    "9": {"type": "bytes"},  # paragraph uuid
                }
            ),
            "9": {"type": "string"},  # link URL
            "12": _message(  # attachment info
                {
                    "1": {"type": "string"},  # attachment identifier
                    "2": {"type": "string"},  # type UTI
                }
            ),
        }
    ),
    "6": _message(  # attachments
        {
            "2": {"type": "string"},  # attachment identifier
            "6": {"type": "string"},  # table cell text
            "7": {"type": "string"},  # link alt text (target title)
            "8": {"type": "string"},  # type UTI
            "34": {"type": "string"},  # link token JSON
        }
    ),
}


def _strings_as_bytes(typedef: dict) -> dict:
    """Copy of ``typedef`` with every ``string`` pin relaxed to ``bytes``."""
    relaxed = {}
    for field, spec in typedef.items():
        spec = dict(spec)
        if spec.get("type") == "string":
            spec["type"] = "bytes"
        if "message_typedef" in spec:
            spec["message_typedef"] = _strings_as_bytes(spec["message_typedef"])
        relaxed[field] = spec
    return relaxed


# Fallback when some string-pinned field isn't valid UTF-8: keep every pin
# (so the UUIDs still can't be guessed as messages) but accept any bytes.
BYTES_PINNED_TYPEDEF = _strings_as_bytes(PINNED_TYPEDEF)


class NotesRichtextParseError(Exception):
    """Error parsing Notes richtext format."""

    pass


class NotesRichtextParser:
    """Parser for com.apple.notes.richtext binary data."""

    # Mapping from protobuf paragraph type field (field 2.1) to ParagraphType enum.
    # Note: block quotes use field 2.8=1 instead of field 2.1, handled separately.
    PARAGRAPH_TYPE_MAP = {
        0: ParagraphType.DEFAULT,
        1: ParagraphType.HEADING,
        2: ParagraphType.SUBHEADING,
        4: ParagraphType.MONOSPACE,
        100: ParagraphType.BULLET_LIST,
        101: ParagraphType.DASHED_LIST,
        102: ParagraphType.NUMBERED_LIST,
        103: ParagraphType.CHECKLIST,
    }

    def parse(self, data: bytes) -> NotesRichtext:
        """Parse com.apple.notes.richtext binary data.

        Args:
            data: Raw binary data from clipboard or file

        Returns:
            NotesRichtext object with parsed content

        Raises:
            NotesRichtextParseError: If parsing fails
        """
        try:
            # Step 1: Decode NSKeyedArchive (binary plist)
            archive = self._decode_archive(data)

            # Step 2: Extract the NS.data protobuf payload
            ns_data = self._extract_ns_data(archive)

            # Step 3: Decode protobuf
            text, runs, attachments, typedef = self._decode_protobuf(ns_data)

            # Step 4: Build NotesRichtext object
            return NotesRichtext(
                text=text,
                runs=runs,
                attachments=attachments,
                _archive_template=archive,
                _protobuf_typedef=typedef,
                _original_ns_data=ns_data,
            )

        except Exception as e:
            raise NotesRichtextParseError(f"Failed to parse richtext: {e}") from e

    def _decode_archive(self, data: bytes) -> dict:
        """Decode NSKeyedArchive structure from binary plist.

        NSKeyedArchive stores objects in a flat $objects array,
        with references via CF$UID integers.
        """
        try:
            return plistlib.loads(data)
        except Exception as e:
            raise NotesRichtextParseError(f"Failed to decode plist: {e}") from e

    def _extract_ns_data(self, archive: dict) -> bytes:
        """Extract the NS.data payload from the archive.

        Structure: $objects[1] -> attributedStringData -> CF$UID -> $objects[N] -> NS.data
        """
        objects = archive.get("$objects", [])
        if len(objects) < 3:
            raise NotesRichtextParseError("Archive has insufficient objects")

        # Object 1 is the root, contains attributedStringData reference
        root = objects[1]
        if not isinstance(root, dict):
            raise NotesRichtextParseError("Root object is not a dict")

        # Get the attributedStringData reference
        attr_ref = root.get("attributedStringData", {})
        if isinstance(attr_ref, dict) and "CF$UID" in attr_ref:
            attr_idx = attr_ref["CF$UID"]
        else:
            # Try plistlib's UID type
            attr_idx = getattr(attr_ref, "data", None)
            if attr_idx is None:
                raise NotesRichtextParseError(
                    "Cannot find attributedStringData reference"
                )

        # Get the attributedStringData object
        if attr_idx >= len(objects):
            raise NotesRichtextParseError(f"Invalid UID reference: {attr_idx}")

        attr_obj = objects[attr_idx]
        if not isinstance(attr_obj, dict):
            raise NotesRichtextParseError("attributedStringData is not a dict")

        # Extract NS.data
        ns_data = attr_obj.get("NS.data")
        if ns_data is None:
            raise NotesRichtextParseError("NS.data not found in attributedStringData")

        return bytes(ns_data)

    def _decode_protobuf(
        self, ns_data: bytes
    ) -> tuple[str, list[AttributeRun], list[Attachment], dict]:
        """Decode protobuf payload to extract text, runs, and attachments.

        Protobuf structure:
        - Field 2: text content
        - Field 5 (repeated): attribute runs
        - Field 6 (repeated): attachments

        Returns:
            Tuple of (text, runs, attachments, typedef)
        """
        message, typedef = self._decode_pinned(ns_data)

        # Extract text (field 2)
        text = self._extract_text(message)

        # Extract attribute runs (field 5)
        runs = self._extract_runs(message)

        # Apple Notes counts attribute-run lengths in UTF-16 code units, but
        # `text` (decoded from UTF-8) is a Python str counted in code points.
        # A non-BMP character (e.g. the 🐸 emoji) is one code point but two
        # UTF-16 code units, so the two counts diverge by one per such char.
        # Normalize run lengths to code points so the whole pipeline
        # (validation, slicing, edits, serialization) works in a single unit
        # consistent with `text`. See issue #102.
        self._normalize_run_lengths_to_codepoints(text, runs)

        # Extract attachments (field 6)
        attachments = self._extract_attachments(message)

        return text, runs, attachments, typedef

    @staticmethod
    def _decode_pinned(ns_data: bytes) -> tuple[dict, dict]:
        """Decode with PINNED_TYPEDEF, degrading only when the bytes don't fit.

        Bytes that don't fit a pinned type (e.g. non-UTF-8 in a string field)
        are not Notes output we know. Rather than refuse the note, retry with
        the string pins relaxed to bytes, then with plain guessing (the
        pre-#198 behavior), and report the degradation once per process.
        """
        try:
            return blackboxprotobuf.decode_message(
                ns_data, copy.deepcopy(PINNED_TYPEDEF)
            )
        except DecoderException as e:
            from .. import unified_log

            unified_log.log_parse_error("richtext pinned decode", "clipboard", e)
        try:
            return blackboxprotobuf.decode_message(
                ns_data, copy.deepcopy(BYTES_PINNED_TYPEDEF)
            )
        except DecoderException:
            return blackboxprotobuf.decode_message(ns_data)

    @staticmethod
    def _normalize_run_lengths_to_codepoints(
        text: str, runs: list[AttributeRun]
    ) -> None:
        """Convert run lengths from UTF-16 code units to Unicode code points.

        Apple Notes stores attribute-run lengths in UTF-16 code units; `text`
        decoded from UTF-8 is counted in code points. The two differ by one for
        every non-BMP character (a surrogate pair). Walking `text` once, this
        rewrites each run's length (and its raw_data length field) to the number
        of code points it covers, keeping the invariant
        ``sum(run.length) == len(text)`` true in code-point units.

        Run boundaries fall on character boundaries in Apple Notes, so a run
        never splits a surrogate pair. For pure-BMP text this is a no-op (code
        points == UTF-16 units), preserving byte-identical roundtrips. If the
        stored lengths don't line up with `text` (genuinely corrupt data),
        lengths are left untouched so the sum-mismatch validation still fires.
        """
        if not runs:
            return

        cp_index = 0
        text_len = len(text)
        converted: list[int] = []
        for run in runs:
            utf16_remaining = run.length
            cp_count = 0
            while utf16_remaining > 0 and cp_index < text_len:
                utf16_remaining -= 2 if ord(text[cp_index]) > 0xFFFF else 1
                cp_index += 1
                cp_count += 1
            if utf16_remaining != 0:
                # Boundary didn't line up — leave originals for validation.
                return
            converted.append(cp_count)

        for run, cp_count in zip(runs, converted):
            run.length = cp_count
            if run.raw_data and "1" in run.raw_data:
                run.raw_data["1"] = cp_count

    def _extract_text(self, message: dict) -> str:
        """Extract text content from field 2."""
        text_data = message.get("2", "")

        if isinstance(text_data, bytes):
            return text_data.decode("utf-8", errors="replace")
        elif isinstance(text_data, str):
            return text_data
        else:
            return str(text_data)

    def _extract_runs(self, message: dict) -> list[AttributeRun]:
        """Extract attribute runs from field 5."""
        runs_data = message.get("5", [])

        # Ensure it's a list
        if not isinstance(runs_data, list):
            runs_data = [runs_data]

        runs = []
        for run_data in runs_data:
            run = self._parse_run(run_data)
            runs.append(run)

        return runs

    def _parse_run(self, run_data: dict) -> AttributeRun:
        """Parse a single attribute run from protobuf data."""
        # Field 1: length
        length = run_data.get("1", 0)
        if isinstance(length, bytes):
            length = int.from_bytes(length, "little")

        # Field 5: font style (0=normal, 1=bold, 2=italic)
        font_val = run_data.get("5", 0)
        if isinstance(font_val, bytes):
            font_val = int.from_bytes(font_val, "little")
        try:
            font_style = FontStyle(font_val)
        except ValueError:
            font_style = FontStyle.NORMAL

        # Field 6: underline
        underline = bool(run_data.get("6", 0))

        # Field 7: strikethrough
        strikethrough = bool(run_data.get("7", 0))

        # Field 2: paragraph style
        paragraph = None
        para_data = run_data.get("2")
        if para_data:
            paragraph = self._parse_paragraph_style(para_data)

        # Field 9: link URL
        link_url = None
        link_data = run_data.get("9")
        if link_data:
            if isinstance(link_data, bytes):
                link_url = link_data.decode("utf-8", errors="replace")
            elif isinstance(link_data, str):
                link_url = link_data

        # Color: check field 14 (highlight palette index) first, then field 10 (RGB)
        color = None
        highlight_index = run_data.get("14")
        if isinstance(highlight_index, int) and highlight_index > 0:
            # Convert palette index to a sentinel RGB tuple for roundtrip detection
            # Use (0, 0, index) as a marker — the index is stored in raw_data
            color = (0, 0, highlight_index)
        else:
            color_data = run_data.get("10")
            if color_data and isinstance(color_data, dict):
                r = color_data.get("1", 0)
                g = color_data.get("2", 0)
                b = color_data.get("3", 0)
                color = (r, g, b)

        return AttributeRun(
            length=length,
            font_style=font_style,
            underline=underline,
            strikethrough=strikethrough,
            paragraph=paragraph,
            link_url=link_url,
            color=color,
            raw_data=run_data,
        )

    def _parse_paragraph_style(self, para_data: dict) -> ParagraphStyle:
        """Parse paragraph style from protobuf field 2."""
        # Block quotes use field 8=1 instead of field 1
        field_8 = para_data.get("8", 0)
        if isinstance(field_8, bytes):
            field_8 = int.from_bytes(field_8, "little")

        if field_8:
            para_type = ParagraphType.BLOCK_QUOTE
        else:
            # Field 1: paragraph type
            type_val = para_data.get("1", 0)
            if isinstance(type_val, bytes):
                type_val = int.from_bytes(type_val, "little")
            para_type = self.PARAGRAPH_TYPE_MAP.get(type_val, ParagraphType.DEFAULT)

        # Field 5: checklist checked state (nested dict with field 2 as the actual value)
        checked_data = para_data.get("5", {})
        if isinstance(checked_data, dict):
            checklist_checked = bool(checked_data.get("2", 0))
        else:
            checklist_checked = bool(checked_data)

        # Field 9: UUID reference
        uuid_data = para_data.get("9")
        if uuid_data is None:
            uuid = None
        elif isinstance(uuid_data, str):
            uuid = uuid_data.encode("utf-8")
        elif isinstance(uuid_data, (bytes, bytearray)):
            uuid = bytes(uuid_data)
        else:
            # Random uuid bytes that bbpb guessed as a nested message (~1% per
            # paragraph): the bytes can't be recovered here. Nothing reads
            # .uuid back; the serializer re-encodes raw_data, so it round-trips.
            uuid = None

        # Field 2: indent level (sometimes used)
        indent = para_data.get("2", 0)
        if isinstance(indent, bytes):
            indent = int.from_bytes(indent, "little")

        return ParagraphStyle(
            type=para_type,
            indent_level=indent,
            checklist_checked=checklist_checked,
            uuid=uuid,
            raw_data=para_data,
        )

    def _extract_attachments(self, message: dict) -> list[Attachment]:
        """Extract attachments from field 6."""
        attach_data = message.get("6", [])

        # Ensure it's a list
        if not isinstance(attach_data, list):
            attach_data = [attach_data]

        attachments = []
        for item in attach_data:
            attachment = self._parse_attachment(item)
            attachments.append(attachment)

        return attachments

    def _parse_attachment(self, attach_data: dict) -> Attachment:
        """Parse a single attachment from protobuf data."""
        # Field 2: UUID
        uuid_data = attach_data.get("2", b"")
        if isinstance(uuid_data, str):
            uuid_data = uuid_data.encode("utf-8")
        uuid = bytes(uuid_data)

        # Field 8: type UTI
        type_data = attach_data.get("8", b"")
        if isinstance(type_data, bytes):
            type_uti = type_data.decode("utf-8", errors="replace")
        else:
            type_uti = str(type_data)

        # Field 6: content (for tables)
        content_data = attach_data.get("6")
        if content_data is None:
            content = None
        elif isinstance(content_data, str):
            content = content_data.encode("utf-8")
        else:
            content = bytes(content_data)

        return Attachment(
            uuid=uuid,
            type_uti=type_uti,
            content=content,
            raw_data=attach_data,
        )
