"""Regression tests for issue #102: non-BMP emoji break the richtext parse.

Apple Notes stores attribute-run lengths in UTF-16 code units, but the parser
decodes the note text into a Python ``str`` (counted in Unicode code points). A
non-BMP emoji such as 🐸 (U+1F438) is a single code point but two UTF-16 code
units, so ``sum(run.length)`` came out one greater than ``len(text)`` per astral
character. That tripped ``NotesRichtext.validate()`` with::

    Run lengths sum (N) does not match text length (N-1)

while parsing the note's *existing* rich text — before any write — which made
any note containing such an emoji un-editable via ``update_note_content``
(both ``replace`` and ``find_replace``). ``add_note`` (fresh create) was
unaffected because ``build_richtext`` counts run lengths in code points.

These tests build a richtext binary that mimics Apple Notes' native storage
(run lengths in UTF-16 code units) and exercise the three paths called out in
the issue: round-trip read, create-then-find_replace, and create-then-replace.
"""

import plistlib
from pathlib import Path

import blackboxprotobuf

from apple_notes.clipboard import ClipboardManager
from apple_notes.richtext import (
    NotesRichtextParser,
    NotesRichtextSerializer,
    build_richtext,
    markdown_to_richtext,
    parse_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures"

FROG = "\U0001f438"  # 🐸, a non-BMP (astral-plane) emoji: 1 code point, 2 UTF-16 units
SAMPLE_MARKDOWN = f"# {FROG} The Frog\nHello frog world"


def _astral_count(text: str) -> int:
    """Number of non-BMP code points (each worth 2 UTF-16 code units)."""
    return sum(1 for ch in text if ord(ch) > 0xFFFF)


def _apple_native_richtext(markdown: str) -> bytes:
    """Build a richtext binary mimicking Apple Notes' native on-disk storage.

    ``build_richtext`` counts run lengths in code points, but Apple Notes counts
    them in UTF-16 code units (a non-BMP char counts as 2). This re-counts each
    run in UTF-16 units and encodes the protobuf directly (not via the project
    serializer), so the fixture carries genuine UTF-16 run lengths independent of
    the code under test — exactly the shape that triggers issue #102 when parsed.
    """
    template_bytes = (
        FIXTURES / "notes_richtext_sample_with_great_sentence.bin"
    ).read_bytes()
    template = NotesRichtextParser().parse(template_bytes)

    doc = parse_markdown(markdown)
    richtext, _ = build_richtext(doc, template)

    message: dict = {"2": richtext.text.encode("utf-8")}
    runs_data = []
    pos = 0
    for run in richtext.runs:
        covered = richtext.text[pos : pos + run.length]
        pos += run.length
        raw = dict(run.raw_data)
        raw["1"] = run.length + _astral_count(covered)  # UTF-16 code units
        runs_data.append(raw)
    message["5"] = runs_data
    if richtext.attachments:
        message["6"] = [a.raw_data for a in richtext.attachments]

    ns_data = blackboxprotobuf.encode_message(message, richtext._protobuf_typedef)
    archive = NotesRichtextSerializer()._build_archive(
        richtext._archive_template, ns_data
    )
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def _stored_run_length_sum(data: bytes) -> int:
    """Sum of the raw attribute-run length fields (field 5[*].1) as stored in
    the serialized binary — i.e. the on-the-wire unit, before the parser
    normalizes it to code points.
    """
    parser = NotesRichtextParser()
    ns_data = parser._extract_ns_data(parser._decode_archive(data))
    message, _ = blackboxprotobuf.decode_message(ns_data)
    runs = message.get("5", [])
    if not isinstance(runs, list):
        runs = [runs]
    return sum(int(run.get("1", 0)) for run in runs)


class TestEmojiRoundTripRead:
    """Parsing a note that already contains a non-BMP emoji must succeed."""

    def test_parse_does_not_raise(self) -> None:
        data = _apple_native_richtext(SAMPLE_MARKDOWN)

        richtext = NotesRichtextParser().parse(data)

        assert FROG in richtext.text
        # After normalization the invariant holds in code points.
        assert sum(run.length for run in richtext.runs) == len(richtext.text)

    def test_round_trip_read_then_serialize(self) -> None:
        data = _apple_native_richtext(SAMPLE_MARKDOWN)
        parser = NotesRichtextParser()
        serializer = NotesRichtextSerializer()

        richtext = parser.parse(data)
        out = serializer.serialize(richtext)

        assert parser.parse(out).text == richtext.text

    def test_serialized_output_uses_utf16_run_lengths(self) -> None:
        """The serializer must emit Apple-native UTF-16 run lengths, not code
        points. Re-parsing normalizes lengths, so a text-only round-trip check
        would still pass if the serializer regressed to code-point lengths;
        this asserts the on-the-wire unit directly.
        """
        data = _apple_native_richtext(SAMPLE_MARKDOWN)
        richtext = NotesRichtextParser().parse(data)

        out = NotesRichtextSerializer().serialize(richtext)

        text = richtext.text
        expected_utf16 = len(text) + _astral_count(text)
        # The 🐸 emoji makes the UTF-16 count strictly larger than code points.
        assert expected_utf16 > len(text)
        assert _stored_run_length_sum(out) == expected_utf16


class TestEmojiFindReplace:
    """find_replace on a note containing a non-BMP emoji must work and stay
    correctly aligned across the surrogate pair."""

    def test_find_replace_after_emoji(self) -> None:
        data = _apple_native_richtext(SAMPLE_MARKDOWN)

        modified_data, count, _ = (
            ClipboardManager().find_replace_in_notes_richtext_variable(
                data, "frog", "toad", replace_all=False
            )
        )

        assert count == 1
        reparsed = NotesRichtextParser().parse(modified_data)
        # Emoji heading preserved, body word replaced at the right position.
        assert f"{FROG} The Frog" in reparsed.text
        assert "Hello toad world" in reparsed.text


class TestEmojiReplaceWholeBody:
    """A full-body replace parses the existing note as a template; that parse
    must not fail when the note contains a non-BMP emoji."""

    def test_replace_via_markdown_to_richtext(self) -> None:
        data = _apple_native_richtext(SAMPLE_MARKDOWN)

        result = markdown_to_richtext(f"# {FROG} The Toad\nGoodbye frog", data)

        reparsed = NotesRichtextParser().parse(result.data)
        assert f"{FROG} The Toad" in reparsed.text
        assert "Goodbye frog" in reparsed.text
