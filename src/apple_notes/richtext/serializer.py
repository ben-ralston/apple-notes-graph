"""Serializer for Apple Notes richtext format (com.apple.notes.richtext).

Converts NotesRichtext objects back to binary format that Apple Notes accepts.
The key challenge is producing byte-identical output for roundtrip fidelity.
"""

import plistlib

import blackboxprotobuf

from .models import NotesRichtext


class NotesRichtextSerializeError(Exception):
    """Error serializing Notes richtext format."""

    pass


class NotesRichtextSerializer:
    """Serializer for com.apple.notes.richtext binary data."""

    def serialize(self, richtext: NotesRichtext) -> bytes:
        """Serialize NotesRichtext to com.apple.notes.richtext binary format.

        Args:
            richtext: NotesRichtext object to serialize

        Returns:
            Binary data in com.apple.notes.richtext format

        Raises:
            NotesRichtextSerializeError: If serialization fails
        """
        try:
            # Validate the richtext object
            richtext.validate()

            # Strategy: Use the preserved archive template and raw data
            # to reconstruct the exact original bytes
            if richtext._archive_template is None:
                raise NotesRichtextSerializeError(
                    "Cannot serialize without archive template"
                )

            # Build protobuf payload from raw data
            ns_data = self._encode_protobuf(richtext)

            # Reconstruct the archive with updated NS.data
            archive = self._build_archive(richtext._archive_template, ns_data)

            # Serialize to binary plist
            return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)

        except Exception as e:
            raise NotesRichtextSerializeError(f"Failed to serialize: {e}") from e

    @staticmethod
    def _utf16_length(text: str) -> int:
        """Length of `text` in UTF-16 code units (non-BMP chars count as 2)."""
        return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)

    def _encode_protobuf(self, richtext: NotesRichtext) -> bytes:
        """Encode the protobuf payload from NotesRichtext data.

        To achieve byte-identical roundtrip, we use the raw_data preserved
        during parsing rather than reconstructing from parsed values.
        """
        # Build the message from raw data preserved during parsing
        message = {}

        # Field 2: text content (encode as bytes for protobuf)
        message["2"] = richtext.text.encode("utf-8")

        # Field 5: attribute runs. Run lengths are stored in UTF-16 code units
        # (Apple Notes' native unit), but the in-memory model counts them in
        # code points — see NotesRichtextParser._normalize_run_lengths_to_codepoints
        # (issue #102). Recompute each run's length field in UTF-16 units from the
        # text it covers. For pure-BMP text this is a no-op (byte-identical
        # roundtrip); it only differs when a run covers a non-BMP character.
        if richtext.runs:
            runs_data = []
            cp_index = 0
            for run in richtext.runs:
                covered = richtext.text[cp_index : cp_index + run.length]
                cp_index += run.length
                raw = dict(run.raw_data) if run.raw_data else {}
                raw["1"] = self._utf16_length(covered)
                runs_data.append(raw)
            message["5"] = runs_data

        # Field 6: attachments (use raw data for exact reproduction)
        if richtext.attachments:
            attach_data = [a.raw_data for a in richtext.attachments]
            message["6"] = attach_data

        # Use the preserved typedef from parsing for exact encoding
        if richtext._protobuf_typedef is None:
            raise NotesRichtextSerializeError(
                "Cannot serialize without preserved typedef"
            )

        # Encode to protobuf bytes
        return blackboxprotobuf.encode_message(message, richtext._protobuf_typedef)

    def _build_archive(self, template: dict, ns_data: bytes) -> dict:
        """Build NSKeyedArchive structure with updated NS.data.

        Uses UID resolution (same as the parser) to find the NS.data location
        dynamically, rather than assuming a fixed index.
        """
        import copy

        # Deep copy the template to avoid modifying the original
        archive = copy.deepcopy(template)

        objects = archive.get("$objects", [])
        if len(objects) < 2:
            raise NotesRichtextSerializeError("Archive has insufficient objects")

        # Follow UID references: root -> attributedStringData -> NS.data
        root = objects[1]
        if not isinstance(root, dict):
            raise NotesRichtextSerializeError("Root object is not a dict")

        attr_ref = root.get("attributedStringData", {})
        if isinstance(attr_ref, dict) and "CF$UID" in attr_ref:
            attr_idx = attr_ref["CF$UID"]
        else:
            attr_idx = getattr(attr_ref, "data", None)
            if attr_idx is None:
                raise NotesRichtextSerializeError(
                    "Cannot find attributedStringData reference in archive"
                )

        if attr_idx >= len(objects):
            raise NotesRichtextSerializeError(f"Invalid UID reference: {attr_idx}")

        attr_obj = objects[attr_idx]
        if not isinstance(attr_obj, dict) or "NS.data" not in attr_obj:
            raise NotesRichtextSerializeError(
                f"NS.data not found at archive index {attr_idx}"
            )

        attr_obj["NS.data"] = ns_data
        return archive
