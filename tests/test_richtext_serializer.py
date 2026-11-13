"""Tests for Apple Notes richtext serializer.

Following TDD: these tests are written first, before the serializer implementation.
The CRITICAL test is roundtrip: parse → serialize must produce identical bytes.
"""

from pathlib import Path

import pytest

from apple_notes.richtext import NotesRichtext, NotesRichtextParser
from apple_notes.richtext.serializer import NotesRichtextSerializer

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_data() -> bytes:
    """Load the 'great sentence' sample fixture."""
    return (FIXTURES / "notes_richtext_sample_with_great_sentence.bin").read_bytes()


@pytest.fixture
def sample_data_alt() -> bytes:
    """Load the 'amazing phrase' sample fixture."""
    return (FIXTURES / "notes_richtext_sample_with_amazing_phrase.bin").read_bytes()


@pytest.fixture
def parser() -> NotesRichtextParser:
    """Create a parser instance."""
    return NotesRichtextParser()


@pytest.fixture
def serializer() -> NotesRichtextSerializer:
    """Create a serializer instance."""
    return NotesRichtextSerializer()


class TestSerializerBasics:
    """Basic serializer functionality tests."""

    def test_serialize_returns_bytes(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Serializer returns bytes."""
        parsed = parser.parse(sample_data)
        result = serializer.serialize(parsed)
        assert isinstance(result, bytes)

    def test_serialize_produces_valid_plist(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Serialized output is valid binary plist."""
        import plistlib

        parsed = parser.parse(sample_data)
        result = serializer.serialize(parsed)

        # Should not raise
        archive = plistlib.loads(result)
        assert "$archiver" in archive
        assert archive["$archiver"] == "NSKeyedArchiver"


class TestRoundtrip:
    """Roundtrip tests: parse → serialize must produce functionally identical output.

    Note: The binary plist wrapper may differ slightly due to plistlib encoding,
    but the NS.data protobuf payload must be byte-identical.
    """

    def test_roundtrip_produces_identical_ns_data(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """CRITICAL: parse → serialize produces identical NS.data payload."""
        import plistlib

        parsed = parser.parse(sample_data)
        serialized = serializer.serialize(parsed)

        # The NS.data (protobuf) must be byte-identical
        orig_archive = plistlib.loads(sample_data)
        new_archive = plistlib.loads(serialized)

        orig_ns_data = bytes(orig_archive["$objects"][2]["NS.data"])
        new_ns_data = bytes(new_archive["$objects"][2]["NS.data"])

        assert new_ns_data == orig_ns_data, (
            f"NS.data mismatch: original {len(orig_ns_data)} bytes, "
            f"serialized {len(new_ns_data)} bytes"
        )

    def test_roundtrip_alt_fixture(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data_alt: bytes,
    ) -> None:
        """Roundtrip works for alternative fixture too."""
        import plistlib

        parsed = parser.parse(sample_data_alt)
        serialized = serializer.serialize(parsed)

        orig_archive = plistlib.loads(sample_data_alt)
        new_archive = plistlib.loads(serialized)

        orig_ns_data = bytes(orig_archive["$objects"][2]["NS.data"])
        new_ns_data = bytes(new_archive["$objects"][2]["NS.data"])

        assert new_ns_data == orig_ns_data

    def test_double_roundtrip(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Double roundtrip produces stable output."""
        # First roundtrip
        parsed1 = parser.parse(sample_data)
        serialized1 = serializer.serialize(parsed1)

        # Second roundtrip
        parsed2 = parser.parse(serialized1)
        serialized2 = serializer.serialize(parsed2)

        # The two serialized versions should be identical
        assert serialized2 == serialized1

    def test_serialized_can_be_reparsed(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Serialized output can be successfully parsed again."""
        parsed = parser.parse(sample_data)
        serialized = serializer.serialize(parsed)

        # Should not raise
        reparsed = parser.parse(serialized)
        assert reparsed.text == parsed.text
        assert len(reparsed.runs) == len(parsed.runs)


class TestFormattingPreservation:
    """Tests that formatting is preserved through roundtrip."""

    def test_roundtrip_preserves_run_count(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Roundtrip preserves the number of attribute runs."""
        original = parser.parse(sample_data)
        serialized = serializer.serialize(original)
        reparsed = parser.parse(serialized)

        assert len(reparsed.runs) == len(original.runs)

    def test_roundtrip_preserves_text(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Roundtrip preserves text content exactly."""
        original = parser.parse(sample_data)
        serialized = serializer.serialize(original)
        reparsed = parser.parse(serialized)

        assert reparsed.text == original.text

    def test_roundtrip_preserves_attachments(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Roundtrip preserves attachments."""
        original = parser.parse(sample_data)
        serialized = serializer.serialize(original)
        reparsed = parser.parse(serialized)

        assert len(reparsed.attachments) == len(original.attachments)
        for orig, repd in zip(original.attachments, reparsed.attachments):
            assert repd.type_uti == orig.type_uti


class TestProtobufEncoding:
    """Tests for protobuf encoding correctness."""

    def test_serialize_encodes_text_in_field_2(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Serializer encodes text in protobuf field 2."""
        import blackboxprotobuf
        import plistlib

        parsed = parser.parse(sample_data)
        serialized = serializer.serialize(parsed)

        # Extract and decode the protobuf
        archive = plistlib.loads(serialized)
        ns_data = archive["$objects"][2]["NS.data"]
        message, _ = blackboxprotobuf.decode_message(bytes(ns_data))

        assert "2" in message
        text = message["2"]
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        assert "Test note format" in text

    def test_serialize_encodes_runs_in_field_5(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Serializer encodes attribute runs in protobuf field 5."""
        import blackboxprotobuf
        import plistlib

        parsed = parser.parse(sample_data)
        serialized = serializer.serialize(parsed)

        archive = plistlib.loads(serialized)
        ns_data = archive["$objects"][2]["NS.data"]
        message, _ = blackboxprotobuf.decode_message(bytes(ns_data))

        assert "5" in message
        runs = message["5"]
        if not isinstance(runs, list):
            runs = [runs]
        assert len(runs) == 46  # Expected number of runs


class TestArchiveStructure:
    """Tests for NSKeyedArchive structure preservation."""

    def test_serialize_preserves_archive_structure(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Serializer preserves NSKeyedArchive object structure."""
        import plistlib

        original_archive = plistlib.loads(sample_data)
        parsed = parser.parse(sample_data)
        serialized = serializer.serialize(parsed)
        new_archive = plistlib.loads(serialized)

        # Same number of objects
        assert len(new_archive["$objects"]) == len(original_archive["$objects"])

    def test_serialize_preserves_data_persister(
        self,
        parser: NotesRichtextParser,
        serializer: NotesRichtextSerializer,
        sample_data: bytes,
    ) -> None:
        """Serializer preserves dataPersister object."""
        import plistlib

        original_archive = plistlib.loads(sample_data)
        parsed = parser.parse(sample_data)
        serialized = serializer.serialize(parsed)
        new_archive = plistlib.loads(serialized)

        # dataPersister should be preserved
        orig_root = original_archive["$objects"][1]
        new_root = new_archive["$objects"][1]

        assert "dataPersister" in new_root
