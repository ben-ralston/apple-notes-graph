"""Tests for protobuf parser extract_external_links."""

import gzip

import pytest

import blackboxprotobuf

from apple_notes.protobuf_parser import ExternalLinkInfo, NoteProtobufParser


def _utf16_len(s: str) -> int:
    """Length of a string in UTF-16 code units (how Apple stores run lengths)."""
    return len(s.encode("utf-16-le")) // 2


def _encode_note(text: str, runs: list[dict]) -> bytes:
    """Build a gzip-compressed note protobuf like ZICNOTEDATA.ZDATA."""
    run_td = {
        "1": {"type": "int", "name": ""},
        "5": {"type": "int", "name": ""},
        "9": {"type": "bytes", "name": ""},
        "2": {
            "type": "message",
            "message_typedef": {
                "1": {"type": "int", "name": ""},
                "5": {
                    "type": "message",
                    "message_typedef": {"2": {"type": "int", "name": ""}},
                    "name": "",
                },
            },
            "name": "",
        },
    }
    typedef = {
        "2": {
            "type": "message",
            "message_typedef": {
                "3": {
                    "type": "message",
                    "message_typedef": {
                        "2": {"type": "bytes", "name": ""},
                        "5": {"type": "message", "message_typedef": run_td, "name": ""},
                    },
                    "name": "",
                },
            },
            "name": "",
        },
    }
    message = {"2": {"3": {"2": text.encode("utf-8"), "5": runs}}}
    return gzip.compress(blackboxprotobuf.encode_message(message, typedef))


class TestUtf16RunLengthNormalization:
    """Apple stores run lengths in UTF-16 code units; Python slices in code
    points. After any non-BMP char (emoji), un-normalized positions overshoot
    (NTG-009). parse() must normalize lengths so the extract_* helpers slice
    correctly."""

    def setup_method(self):
        self.parser = NoteProtobufParser()

    def test_external_link_text_correct_after_emoji(self):
        text = "\U0001f600 link tail"  # emoji is 2 UTF-16 units, 1 code point
        runs = [
            {"1": _utf16_len("\U0001f600 ")},  # 3 units, 2 code points
            {"1": _utf16_len("link"), "9": b"https://example.com"},
            {"1": _utf16_len(" tail")},
        ]
        note_text, attribute_runs = self.parser.parse(_encode_note(text, runs))

        links = self.parser.extract_external_links(note_text, attribute_runs)

        assert len(links) == 1
        assert links[0].text == "link"
        assert links[0].position == 2  # code points, not UTF-16 units

    def test_formatting_position_correct_after_emoji(self):
        text = "\U0001f600\U0001f600bold"
        runs = [
            {"1": _utf16_len("\U0001f600\U0001f600")},  # 4 units, 2 code points
            {"1": _utf16_len("bold"), "5": 1},
        ]
        _, attribute_runs = self.parser.parse(_encode_note(text, runs))

        formats = self.parser.extract_text_formatting(attribute_runs)

        assert len(formats) == 1
        assert formats[0].position == 2
        assert formats[0].length == 4

    def test_checklist_position_correct_after_emoji(self):
        text = "\U0001f600x\ntodo"
        runs = [
            {"1": _utf16_len("\U0001f600x\n")},  # 4 units, 3 code points
            {"1": _utf16_len("todo"), "2": {"1": 103, "5": {"2": 1}}},
        ]
        _, attribute_runs = self.parser.parse(_encode_note(text, runs))

        items = self.parser.extract_checklist_info(attribute_runs)

        assert len(items) == 1
        assert items[0].position == 3
        assert items[0].is_checked is True

    def test_paragraph_style_position_correct_after_emoji(self):
        text = "\U0001f600 head"
        runs = [
            {"1": _utf16_len("\U0001f600 ")},
            {"1": _utf16_len("head"), "2": {"1": 1}},  # heading
        ]
        _, attribute_runs = self.parser.parse(_encode_note(text, runs))

        styles = self.parser.extract_paragraph_styles(attribute_runs)

        assert len(styles) == 1
        assert styles[0].position == 2
        assert styles[0].style_type == 1

    def test_run_lengths_rewritten_to_codepoints(self):
        text = "\U0001f600\U0001f600"
        runs = [{"1": 4}]  # 4 UTF-16 units = 2 code points
        _, attribute_runs = self.parser.parse(_encode_note(text, runs))

        assert self.parser.get_run_length(attribute_runs[0]) == 2

    def test_bmp_only_text_lengths_unchanged(self):
        text = "plain text"
        runs = [{"1": 6}, {"1": 4}]
        _, attribute_runs = self.parser.parse(_encode_note(text, runs))

        assert self.parser.get_run_length(attribute_runs[0]) == 6
        assert self.parser.get_run_length(attribute_runs[1]) == 4

    def test_mismatched_lengths_left_untouched(self):
        """Corrupt lengths that don't line up with the text stay as stored."""
        text = "abc"
        runs = [{"1": 5}]
        _, attribute_runs = self.parser.parse(_encode_note(text, runs))

        assert self.parser.get_run_length(attribute_runs[0]) == 5


class TestExtractExternalLinksTrailingNewline:
    """Tests that extract_external_links strips trailing newlines from link text."""

    def setup_method(self):
        self.parser = NoteProtobufParser()

    def test_link_text_excludes_trailing_newline(self):
        """Link text should NOT include trailing \\n even when run length includes it."""
        # Simulates: "Hotel Neri\n" where run length=11 includes the newline
        note_text = "Hotel Neri\nnext line"
        attribute_runs = [
            {"1": 11, "9": b"https://hotelneri.com"},  # length=11 includes \n
            {"1": 9},  # "next line"
        ]

        links = self.parser.extract_external_links(note_text, attribute_runs)

        assert len(links) == 1
        assert links[0].text == "Hotel Neri"  # NOT "Hotel Neri\n"
        assert links[0].length == 10  # Length of text without \n
        assert links[0].url == "https://hotelneri.com"
        assert links[0].position == 0

    def test_link_text_without_trailing_newline_unchanged(self):
        """Link text without trailing \\n should be unchanged."""
        note_text = "Visit Hotel Neri today"
        attribute_runs = [
            {"1": 6},  # "Visit "
            {"1": 10, "9": b"https://hotelneri.com"},  # "Hotel Neri"
            {"1": 6},  # " today"
        ]

        links = self.parser.extract_external_links(note_text, attribute_runs)

        assert len(links) == 1
        assert links[0].text == "Hotel Neri"
        assert links[0].length == 10
        assert links[0].position == 6

    def test_link_at_end_of_note_with_trailing_newline(self):
        """Link at the end of note text where run includes trailing \\n."""
        note_text = "Check out Hotel Neri\n"
        attribute_runs = [
            {"1": 10},  # "Check out "
            {"1": 11, "9": b"https://hotelneri.com"},  # "Hotel Neri\n"
        ]

        links = self.parser.extract_external_links(note_text, attribute_runs)

        assert len(links) == 1
        assert links[0].text == "Hotel Neri"
        assert links[0].length == 10

    def test_multiple_links_positions_correct_after_newline_strip(self):
        """Position tracking must use original run length, not stripped length."""
        # Two links on consecutive lines, both with trailing \n in run length
        note_text = "Link A\nLink B\nrest"
        attribute_runs = [
            {"1": 7, "9": b"https://a.com"},  # "Link A\n" (length 7)
            {"1": 7, "9": b"https://b.com"},  # "Link B\n" (length 7)
            {"1": 4},  # "rest"
        ]

        links = self.parser.extract_external_links(note_text, attribute_runs)

        assert len(links) == 2
        # First link
        assert links[0].text == "Link A"
        assert links[0].length == 6
        assert links[0].position == 0
        # Second link — position should be 7 (0 + original length 7), not 6
        assert links[1].text == "Link B"
        assert links[1].length == 6
        assert links[1].position == 7


class TestExtractTextFormattingFontStyleBitmask:
    """Font style field 5 is a bitmask (1=bold, 2=italic), not an enum (NTG-010)."""

    def setup_method(self):
        self.parser = NoteProtobufParser()

    def test_bold_italic_combined_reads_both(self):
        """A run with font style 3 (bold|italic) must report both flags."""
        attribute_runs = [{"1": 4, "5": 3}]

        formats = self.parser.extract_text_formatting(attribute_runs)

        assert len(formats) == 1
        assert formats[0].is_bold is True
        assert formats[0].is_italic is True

    def test_bold_only_still_reads_bold(self):
        attribute_runs = [{"1": 4, "5": 1}]

        formats = self.parser.extract_text_formatting(attribute_runs)

        assert len(formats) == 1
        assert formats[0].is_bold is True
        assert formats[0].is_italic is False

    def test_italic_only_still_reads_italic(self):
        attribute_runs = [{"1": 4, "5": 2}]

        formats = self.parser.extract_text_formatting(attribute_runs)

        assert len(formats) == 1
        assert formats[0].is_bold is False
        assert formats[0].is_italic is True


class TestEncryptedZdata:
    """Locked notes (issue #126 / NTG-022).

    A password-protected note's ZICNOTEDATA.ZDATA is not gzip at all — real
    data shows a `bplist00` blob (with ZCRYPTOTAG set). Before this fix that
    surfaced as ProtobufParseError("Not a gzipped file (b'bp')") and every
    `except EncryptedNoteError` branch in the codebase was dead code.
    """

    def test_bplist_zdata_raises_encrypted_note_error(self):
        from apple_notes.exceptions import EncryptedNoteError

        locked_zdata = b"bplist00" + b"\x00" * 40
        with pytest.raises(EncryptedNoteError, match="password"):
            NoteProtobufParser().parse(locked_zdata)

    def test_encrypted_is_not_reported_as_parse_error(self):
        """The two must stay distinct: callers special-case EncryptedNoteError."""
        from apple_notes.exceptions import EncryptedNoteError, ProtobufParseError

        with pytest.raises(EncryptedNoteError) as exc_info:
            NoteProtobufParser().parse(b"bplist00" + b"\x00" * 40)
        assert not isinstance(exc_info.value, ProtobufParseError)

    def test_other_non_gzip_garbage_is_still_a_parse_error(self):
        from apple_notes.exceptions import ProtobufParseError

        with pytest.raises(ProtobufParseError, match="decompress"):
            NoteProtobufParser().parse(b"\x00\x01\x02 definitely not gzip")

    def test_valid_gzip_still_parses(self):
        text, runs = NoteProtobufParser().parse(_encode_note("hello", []))
        assert text == "hello"


@pytest.mark.parametrize("text", ["Empty", "A heading", "Head", "Empty\nA heading"])
def test_short_note_text_decodes_as_text(text):
    """Short ZDATA texts must not be guessed as nested messages (#184, R12)."""
    zdata = _encode_note(text, [{"1": _utf16_len(text)}])
    note_text, runs = NoteProtobufParser().parse(zdata)
    assert note_text == text
    assert sum(r["1"] for r in runs) == len(text)


def test_invalid_utf8_note_text_decodes_with_replacement():
    """A string pin raises on invalid UTF-8; the parser must fall back to bytes
    and decode with errors="replace", as before the pin (#184 final review)."""
    raw = b"ab\xffcd"
    typedef = {
        "2": {"type": "message", "name": "", "message_typedef": {
            "3": {"type": "message", "name": "", "message_typedef": {
                "2": {"type": "bytes", "name": ""},
                "5": {"type": "message", "name": "", "message_typedef": {
                    "1": {"type": "int", "name": ""}}},
            }},
        }},
    }  # fmt: skip
    message = {"2": {"3": {"2": raw, "5": [{"1": 5}]}}}
    zdata = gzip.compress(blackboxprotobuf.encode_message(message, typedef))
    note_text, runs = NoteProtobufParser().parse(zdata)
    assert note_text == "ab�cd"
    assert runs[0]["1"] == 5
