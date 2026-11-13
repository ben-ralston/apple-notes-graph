#!/usr/bin/env python3
"""Integration tests for the richtext module.

Collected by pytest (issue #120 / NTG-025). This file used to be named
`verify_richtext_integration.py`, which pytest's default `python_files`
pattern never picked up, so the round-trip regression suite never ran.

Verifies all ISC items from issue #31 are working:
1. Parse any com.apple.notes.richtext data into structured representation
2. Serialize back to valid binary that Notes accepts
3. Variable-length replacements work
4. Toggle checkbox state works
5. All formatting preserved through parse/edit/serialize cycle
"""

from pathlib import Path

# Test fixtures
FIXTURES = Path(__file__).parent / "fixtures"
GREAT_SENTENCE = FIXTURES / "notes_richtext_sample_with_great_sentence.bin"
AMAZING_PHRASE = FIXTURES / "notes_richtext_sample_with_amazing_phrase.bin"


def test_1_parse_richtext():
    """ISC #1: Can parse any com.apple.notes.richtext data into structured representation."""
    from apple_notes.richtext import NotesRichtextParser

    parser = NotesRichtextParser()
    data = GREAT_SENTENCE.read_bytes()
    richtext = parser.parse(data)

    # Verify text extraction
    assert "Test note format" in richtext.text, f"Expected 'Test note format' in text, got: {richtext.text[:100]}"
    assert "great sentence" in richtext.text, f"Expected 'great sentence' in text"

    # Verify runs extraction
    assert len(richtext.runs) == 46, f"Expected 46 runs, got {len(richtext.runs)}"

    # Verify run lengths sum to text length (critical invariant)
    total_length = sum(r.length for r in richtext.runs)
    assert total_length == len(richtext.text), f"Run lengths {total_length} != text length {len(richtext.text)}"

    # Verify formatting detection
    has_bold = any(r.font_style.value & 1 for r in richtext.runs if r.font_style)
    assert has_bold, "Expected to find bold text"

    print("✅ ISC #1 PASS: Parse richtext into structured representation")


def test_2_serialize_roundtrip():
    """ISC #2: Can serialize back to valid binary that Notes accepts."""
    import plistlib
    from apple_notes.richtext import NotesRichtextParser, NotesRichtextSerializer

    parser = NotesRichtextParser()
    serializer = NotesRichtextSerializer()

    original_data = GREAT_SENTENCE.read_bytes()
    parsed = parser.parse(original_data)
    serialized = serializer.serialize(parsed)

    # Verify it's valid plist
    archive = plistlib.loads(serialized)
    assert archive["$archiver"] == "NSKeyedArchiver"

    # Verify NS.data payload is byte-identical (the critical test)
    orig_archive = plistlib.loads(original_data)
    orig_ns_data = bytes(orig_archive["$objects"][2]["NS.data"])
    new_ns_data = bytes(archive["$objects"][2]["NS.data"])

    assert new_ns_data == orig_ns_data, f"NS.data mismatch: {len(orig_ns_data)} vs {len(new_ns_data)} bytes"

    # Verify can be reparsed
    reparsed = parser.parse(serialized)
    assert reparsed.text == parsed.text
    assert len(reparsed.runs) == len(parsed.runs)

    print("✅ ISC #2 PASS: Serialize back to valid binary (byte-identical NS.data)")


def test_3_variable_length_replacement():
    """ISC #3: Variable-length replacements work."""
    from apple_notes.richtext import NotesRichtextParser, NotesRichtextSerializer, find_replace

    parser = NotesRichtextParser()
    serializer = NotesRichtextSerializer()

    data = GREAT_SENTENCE.read_bytes()
    richtext = parser.parse(data)

    # Replace "great sentence" (14 chars) with "magnificent" (11 chars) - different length
    modified = find_replace(richtext, "great sentence", "magnificent")

    # Verify text updated
    assert "magnificent" in modified.text
    assert "great sentence" not in modified.text

    # Verify run lengths still sum to text length
    total_length = sum(r.length for r in modified.runs)
    assert total_length == len(modified.text), f"Run lengths {total_length} != text length {len(modified.text)}"

    # Verify can serialize
    serialized = serializer.serialize(modified)
    reparsed = parser.parse(serialized)
    assert "magnificent" in reparsed.text

    print("✅ ISC #3 PASS: Variable-length replacements work")


def test_4_toggle_checkbox():
    """ISC #4: Toggle checkbox state works."""
    from apple_notes.richtext import NotesRichtextParser, ParagraphType, toggle_checkbox

    parser = NotesRichtextParser()
    data = GREAT_SENTENCE.read_bytes()
    richtext = parser.parse(data)

    # Find current state of "This needs to happen" (a checklist item)
    item_text = "This needs to happen"
    original_checked = None
    for i, run in enumerate(richtext.runs):
        text_slice = richtext.text_at_run(i)
        if item_text in text_slice and run.paragraph:
            if run.paragraph.type == ParagraphType.CHECKLIST:
                original_checked = run.paragraph.checklist_checked
                break

    assert original_checked is not None, f"Could not find checklist item '{item_text}'"

    # Toggle it
    toggled = toggle_checkbox(richtext, item_text)

    # Find new state
    new_checked = None
    for i, run in enumerate(toggled.runs):
        text_slice = toggled.text_at_run(i)
        if item_text in text_slice and run.paragraph:
            if run.paragraph.type == ParagraphType.CHECKLIST:
                new_checked = run.paragraph.checklist_checked
                break

    assert new_checked != original_checked, f"Checkbox state did not change: {original_checked} -> {new_checked}"

    print("✅ ISC #4 PASS: Toggle checkbox state works")


def test_5_formatting_preserved():
    """ISC #5: All formatting preserved through parse/edit/serialize cycle."""
    from apple_notes.richtext import NotesRichtextParser, NotesRichtextSerializer, find_replace

    parser = NotesRichtextParser()
    serializer = NotesRichtextSerializer()

    data = GREAT_SENTENCE.read_bytes()
    original = parser.parse(data)

    # Edit with same-length replacement (should preserve everything perfectly)
    modified = find_replace(original, "great", "super")

    # Verify run count preserved
    assert len(modified.runs) == len(original.runs), f"Run count changed: {len(original.runs)} -> {len(modified.runs)}"

    # Verify attachment count preserved
    assert len(modified.attachments) == len(original.attachments)

    # Serialize and reparse
    serialized = serializer.serialize(modified)
    reparsed = parser.parse(serialized)

    # Verify still intact after full roundtrip
    assert len(reparsed.runs) == len(original.runs)
    assert len(reparsed.attachments) == len(original.attachments)
    assert "super sentence" in reparsed.text

    print("✅ ISC #5 PASS: All formatting preserved through parse/edit/serialize cycle")


def test_6_clipboard_integration():
    """ISC #6: ClipboardManager.find_replace_in_notes_richtext_variable works."""
    from unittest.mock import MagicMock, patch

    from apple_notes.clipboard import ClipboardManager

    with patch("apple_notes.clipboard.NSPasteboard") as mock_pb_class:
        mock_pb_class.generalPasteboard.return_value = MagicMock()
        cm = ClipboardManager()

    data = GREAT_SENTENCE.read_bytes()

    # Same-length replacement (uses byte-level method)
    result_same, count_same, _ = cm.find_replace_in_notes_richtext_variable(
        data, "great", "super", replace_all=True
    )
    assert count_same >= 1, "Expected at least 1 replacement"

    # Different-length replacement (uses parser/serializer)
    result_diff, count_diff, _ = cm.find_replace_in_notes_richtext_variable(
        data, "great sentence", "magnificent", replace_all=True
    )
    assert count_diff >= 1, "Expected at least 1 replacement"

    # Verify results can be reparsed
    from apple_notes.richtext import NotesRichtextParser

    parser = NotesRichtextParser()

    reparsed_same = parser.parse(result_same)
    assert "super" in reparsed_same.text

    reparsed_diff = parser.parse(result_diff)
    assert "magnificent" in reparsed_diff.text

    # The title line (first line) is never touched by a find_replace
    assert reparsed_same.text.split("\n")[0] == "Test note format FOR UPDATING"
    assert reparsed_diff.text.split("\n")[0] == "Test note format FOR UPDATING"
