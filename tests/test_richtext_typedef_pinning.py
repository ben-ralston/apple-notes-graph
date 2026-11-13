"""Regression tests for issue #198: richtext fields must not be type-guessed.

``blackboxprotobuf.decode_message`` with no typedef guesses each
length-delimited field's type from its bytes, and in a repeated field the
FIRST entry fixes the type for every later one. A paragraph UUID (field
5.2.9) is 16 random bytes; about 1.4% of them happen to parse as a valid
protobuf message. When the note's first paragraph UUID is one of those, the
parser got a dict where it expected bytes and ``bytes(dict)`` failed with
"'str' object cannot be interpreted as an integer", so find_replace (and
anything else that parses the copied richtext) refused the note.

Headless (no GUI, no Notes): richtext payloads are built in-memory on top
of a real clipboard fixture's archive template.
"""

from pathlib import Path

import blackboxprotobuf
import pytest

from apple_notes.richtext import NotesRichtextParser, NotesRichtextSerializer

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES = [
    "notes_richtext_sample_with_great_sentence.bin",
    "notes_richtext_sample_with_amazing_phrase.bin",
]

# A 16-byte paragraph UUID that blackboxprotobuf guesses as a nested message.
MESSAGE_LIKE_UUID = bytes.fromhex("d03cc78502a1db251fe0ad4c3ca99c5a")
# An ordinary UUID that guesses as bytes.
PLAIN_UUID = bytes.fromhex("4eeda1defeaa43a4be6cbcd362aad969")

# Typedef used only to BUILD test payloads (so the bytes are written as-is).
_BUILD_TYPEDEF = {
    "2": {"type": "bytes"},
    "5": {
        "type": "message",
        "message_typedef": {
            "1": {"type": "int"},
            "2": {
                "type": "message",
                "message_typedef": {
                    "1": {"type": "int"},
                    "9": {"type": "bytes"},
                    "5": {
                        "type": "message",
                        "message_typedef": {
                            "1": {"type": "bytes"},
                            "2": {"type": "int"},
                        },
                    },
                },
            },
            "12": {
                "type": "message",
                "message_typedef": {
                    "1": {"type": "bytes"},
                    "2": {"type": "bytes"},
                },
            },
        },
    },
    "6": {
        "type": "message",
        "message_typedef": {
            "2": {"type": "bytes"},
            "6": {"type": "bytes"},
            "7": {"type": "bytes"},
            "8": {"type": "bytes"},
            "34": {"type": "bytes"},
        },
    },
}


@pytest.fixture
def parser() -> NotesRichtextParser:
    return NotesRichtextParser()


@pytest.fixture
def template(parser):
    return parser.parse((FIXTURES / SAMPLES[0]).read_bytes())


def _richtext_bytes(template, message: dict) -> bytes:
    """Wrap a protobuf message in the fixture's NSKeyedArchive."""
    import plistlib

    ns_data = blackboxprotobuf.encode_message(message, _BUILD_TYPEDEF)
    archive = NotesRichtextSerializer()._build_archive(
        template._archive_template, ns_data
    )
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def _run(length: int, uuid: bytes, para_type: int = 0, checklist=None) -> dict:
    para = {"1": para_type, "9": uuid}
    if checklist is not None:
        para["5"] = {"1": checklist, "2": 0}
    return {"1": length, "2": para}


def test_uuid_constants_reproduce_the_guess():
    """Precondition: the constants really trigger / avoid the bad guess."""
    guessed, _ = blackboxprotobuf.decode_message(b"\x4a\x10" + MESSAGE_LIKE_UUID)
    assert isinstance(guessed["9"], dict)
    guessed, _ = blackboxprotobuf.decode_message(b"\x4a\x10" + PLAIN_UUID)
    assert isinstance(guessed["9"], bytes)


def test_first_paragraph_uuid_that_looks_like_a_message(parser, template):
    """The #198 failure: first paragraph UUID decodes as a nested message."""
    data = _richtext_bytes(
        template,
        {
            "2": b"Trips\nPlans\n",
            "5": [
                _run(6, MESSAGE_LIKE_UUID),
                _run(6, PLAIN_UUID, para_type=100),
            ],
        },
    )

    rt = parser.parse(data)

    assert rt.text == "Trips\nPlans\n"
    assert rt.runs[0].paragraph.uuid == MESSAGE_LIKE_UUID
    assert rt.runs[1].paragraph.uuid == PLAIN_UUID
    # No alternate-typedef keys ("9-1") leak into the raw paragraph data.
    assert all(set(r.raw_data["2"]) <= {"1", "9"} for r in rt.runs)


def test_message_like_uuid_round_trips_byte_identical(parser, template):
    data = _richtext_bytes(
        template,
        {
            "2": b"Trips\nPlans\n",
            "5": [_run(6, MESSAGE_LIKE_UUID), _run(6, PLAIN_UUID)],
        },
    )

    assert NotesRichtextSerializer().serialize(parser.parse(data)) == data


def test_checklist_uuid_that_looks_like_a_message(parser, template):
    data = _richtext_bytes(
        template,
        {
            "2": b"Todo\nmilk\n",
            "5": [
                _run(5, PLAIN_UUID),
                _run(5, PLAIN_UUID, para_type=103, checklist=MESSAGE_LIKE_UUID),
            ],
        },
    )

    rt = parser.parse(data)

    assert rt.runs[1].raw_data["2"]["5"]["1"] == MESSAGE_LIKE_UUID
    assert NotesRichtextSerializer().serialize(rt) == data


def test_text_that_looks_like_a_message(parser, template):
    """Note text "Hi" is valid protobuf ({9: 105}) and was guessed as one."""
    data = _richtext_bytes(template, {"2": b"Hi", "5": [_run(2, PLAIN_UUID)]})

    assert parser.parse(data).text == "Hi"


def test_link_alt_text_that_looks_like_a_message(parser, template):
    """A link to a note titled "Hi": attachment alt (6.7) stays a string."""
    data = _richtext_bytes(
        template,
        {
            "2": b"See \xef\xbf\xbc\n",
            "5": [_run(6, PLAIN_UUID)],
            "6": [
                {
                    "2": b"6A1F3C1E-0000-4000-8000-000000000001",
                    "7": b"Hi",
                    "8": b"com.apple.notes.inlinetextattachment.link",
                    "34": b'{"x":1}',
                }
            ],
        },
    )

    attachment = parser.parse(data).attachments[0]

    assert attachment.raw_data["7"] == "Hi"
    assert attachment.type_uti == "com.apple.notes.inlinetextattachment.link"


@pytest.mark.parametrize("name", SAMPLES)
def test_real_samples_decode_exactly_as_before(parser, name):
    """Pinning changes nothing for notes the guesser already got right."""
    rt = parser.parse((FIXTURES / name).read_bytes())
    guessed_message, guessed_typedef = blackboxprotobuf.decode_message(
        rt._original_ns_data
    )

    assert rt._protobuf_typedef == guessed_typedef
    assert [r.raw_data for r in rt.runs] == guessed_message["5"]
    assert [a.raw_data for a in rt.attachments] == guessed_message["6"]


def test_bytes_that_break_a_pinned_type_fall_back_to_guessing(parser, template):
    """Non-UTF-8 in a string-pinned field parses as it did before #198.

    This pins parity with main (guessed bytes, decoded with U+FFFD), not a
    claim that the lossy text is desirable.
    """
    data = _richtext_bytes(template, {"2": b"\xff\xfe\n", "5": [_run(3, PLAIN_UUID)]})

    rt = parser.parse(data)

    assert rt.text == "��\n"
    assert rt.runs[0].paragraph.uuid == PLAIN_UUID


def test_replace_writes_a_checklist_into_a_note_without_one(parser, template):
    """`replace` converts markdown against the note's OWN typedef.

    The pinned typedef names checklist field 5.2.5 even when the note has
    no checklist, so the converter must still complete that definition
    (checked-state subfield 2) instead of skipping it as already present.
    """
    from apple_notes.richtext import markdown_to_richtext

    note = _richtext_bytes(
        template, {"2": b"Plain\nhello\n", "5": [_run(12, PLAIN_UUID, para_type=100)]}
    )

    result = markdown_to_richtext("- [ ] milk\n- [x] eggs", note)

    rt = parser.parse(result.data)
    assert rt.text.startswith("milk\neggs")
    assert [r.paragraph.checklist_checked for r in rt.runs if r.paragraph][:2] == [
        False,
        True,
    ]


def test_non_utf8_text_keeps_the_uuid_pins(parser, template):
    """The fallback must not reopen #198: string pins relax, bytes pins stay."""
    data = _richtext_bytes(
        template,
        {"2": b"\xff\xfe\n", "5": [_run(3, MESSAGE_LIKE_UUID)]},
    )

    rt = parser.parse(data)

    assert rt.runs[0].paragraph.uuid == MESSAGE_LIKE_UUID
    assert set(rt.runs[0].raw_data["2"]) == {"1", "9"}


def test_fallback_is_reported_to_the_unified_logger(parser, template, monkeypatch):
    from apple_notes import unified_log

    calls = []
    monkeypatch.setattr(
        unified_log, "log_parse_error", lambda *a, **k: calls.append((a, k))
    )
    data = _richtext_bytes(template, {"2": b"\xff\xfe\n", "5": [_run(3, PLAIN_UUID)]})

    parser.parse(data)

    assert len(calls) == 1
    site, _note, exc = calls[0][0]
    assert "richtext" in site
    assert isinstance(exc, Exception)


def test_pinned_decode_does_not_report(parser, monkeypatch):
    from apple_notes import unified_log

    calls = []
    monkeypatch.setattr(
        unified_log, "log_parse_error", lambda *a, **k: calls.append(a)
    )

    parser.parse((FIXTURES / SAMPLES[0]).read_bytes())

    assert calls == []


def test_table_content_and_run_attachment_info_stay_strings(parser, template):
    """Table cell text (6.6) and run attachment info (5.12) are pinned too."""
    run = _run(2, PLAIN_UUID)
    run["12"] = {"1": b"Hi", "2": b"com.apple.notes.table"}
    data = _richtext_bytes(
        template,
        {
            "2": b"\xef\xbf\xbc\n",
            "5": [run],
            "6": [
                {
                    "2": b"Hi",
                    "6": b"Hi",
                    "8": b"com.apple.notes.table",
                }
            ],
        },
    )

    rt = parser.parse(data)

    assert rt.runs[0].raw_data["12"]["1"] == "Hi"
    assert rt.attachments[0].content == b"Hi"
    assert rt.attachments[0].raw_data["6"] == "Hi"
