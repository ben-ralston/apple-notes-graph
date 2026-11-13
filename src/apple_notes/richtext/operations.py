"""Edit operations for Apple Notes richtext format.

These operations modify NotesRichtext objects while preserving formatting.
The key challenge is updating run lengths correctly when text changes.
"""

import copy
import json

from ..link_tokens import note_link_uuid, retarget_note_link
from ..models import AttachmentType
from .models import AttributeRun, FontStyle, NotesRichtext, ParagraphType


def adjust_run_lengths_for_replacement(
    runs: list[AttributeRun],
    original_text: str,
    search: str,
    replace: str,
    start: int = 0,
) -> None:
    """Adjust run lengths in-place for a text replacement.

    When text changes length, the run that contains the change must be
    adjusted so that the sum of all run lengths still equals the text length.

    Args:
        runs: List of attribute runs to modify in-place
        original_text: The original text content
        search: Text being replaced
        replace: Replacement text
        start: Position to start searching from (for replace_all)
    """
    if not runs:
        return

    # Find position of the replacement
    pos = original_text.find(search, start)
    if pos == -1:
        return  # Not found, no adjustment needed

    length_diff = len(replace) - len(search)
    if length_diff == 0:
        return  # Same length, no adjustment needed

    # Find the run that contains this position
    current_pos = 0
    for run in runs:
        run_end = current_pos + run.length

        if current_pos <= pos < run_end:
            # This run contains the replacement
            new_length = run.length + length_diff

            # Ensure run length doesn't go negative
            if new_length < 0:
                # Need to adjust subsequent runs too
                remaining_reduction = -new_length
                run.length = 0

                # Find subsequent runs to reduce
                found_this_run = False
                for other_run in runs:
                    if other_run is run:
                        found_this_run = True
                        continue
                    if found_this_run and remaining_reduction > 0:
                        reduction = min(other_run.length, remaining_reduction)
                        other_run.length -= reduction
                        remaining_reduction -= reduction

                        # Update raw_data for modified runs
                        if other_run.raw_data and "1" in other_run.raw_data:
                            other_run.raw_data["1"] = other_run.length

                # Validate that we could accommodate the full reduction
                if remaining_reduction > 0:
                    raise ValueError(
                        f"Cannot reduce run lengths: {remaining_reduction} characters "
                        f"could not be accommodated by subsequent runs"
                    )
            else:
                run.length = new_length

            # Update raw_data if present
            if run.raw_data and "1" in run.raw_data:
                run.raw_data["1"] = run.length

            break

        current_pos = run_end


def _split_runs_at_paragraph_boundaries(richtext: NotesRichtext) -> None:
    """Split attribute runs that span multiple paragraphs.

    In Apple Notes protobuf, each paragraph (ending with \\n) should have
    its own attribute run. After a find_replace that changes text length,
    a single run may cover text with \\n characters in the middle. This
    function splits such runs so each paragraph has its own run.

    Modifies the richtext.runs list in-place.
    """
    new_runs: list[AttributeRun] = []
    current_pos = 0

    for run in richtext.runs:
        run_text = richtext.text[current_pos : current_pos + run.length]
        # Count paragraph breaks within this run (not counting trailing \n)
        # A run covering "foo\nbar\n" should split into "foo\n" and "bar\n"
        parts = run_text.split("\n")

        if len(parts) <= 2 and run_text.endswith("\n"):
            # Single paragraph (text + trailing \n) — no split needed
            new_runs.append(run)
        elif len(parts) == 1:
            # No newlines at all — no split needed
            new_runs.append(run)
        else:
            # Multiple paragraphs in one run — split at \n boundaries
            for i, part in enumerate(parts):
                if i < len(parts) - 1:
                    # Add the \n back
                    sub_text = part + "\n"
                else:
                    # Last part (may be empty if text ends with \n)
                    sub_text = part

                if not sub_text:
                    continue

                sub_run = copy.deepcopy(run)
                sub_run.length = len(sub_text)
                if sub_run.raw_data and "1" in sub_run.raw_data:
                    sub_run.raw_data["1"] = sub_run.length
                new_runs.append(sub_run)

        current_pos += run.length

    richtext.runs = new_runs


def _splice_runs_for_replacement(
    runs: list[AttributeRun],
    search_pos: int,
    search_len: int,
    replacement_runs: list[AttributeRun],
) -> list[AttributeRun]:
    """Replace runs covering [search_pos, search_pos+search_len) with replacement_runs.

    Splits existing runs at the search boundaries and inserts the replacement
    runs in between, preserving all other runs.

    Args:
        runs: Original attribute runs
        search_pos: Start position of search text in the original text
        search_len: Length of search text
        replacement_runs: New runs to insert (from markdown parsing)

    Returns:
        New list of attribute runs with the replacement spliced in
    """
    search_end = search_pos + search_len
    new_runs: list[AttributeRun] = []
    current_pos = 0
    replacement_inserted = False

    for run in runs:
        run_start = current_pos
        run_end = current_pos + run.length

        if run_end <= search_pos:
            # Entirely before search range — keep as-is
            new_runs.append(run)
        elif run_start >= search_end:
            # Entirely after search range — insert replacement first if needed
            if not replacement_inserted:
                new_runs.extend(copy.deepcopy(replacement_runs))
                replacement_inserted = True
            new_runs.append(run)
        else:
            # This run overlaps with the search range

            # Keep the part before the search
            if run_start < search_pos:
                before_len = search_pos - run_start
                before_run = copy.deepcopy(run)
                before_run.length = before_len
                if before_run.raw_data and "1" in before_run.raw_data:
                    before_run.raw_data["1"] = before_len
                new_runs.append(before_run)

            # Insert replacement runs (only once)
            if not replacement_inserted:
                spliced = copy.deepcopy(replacement_runs)
                # If the search starts at a paragraph boundary (no "before"
                # portion), transfer the original run's paragraph style to
                # the first replacement run so headings/lists/quotes survive.
                if run_start == search_pos and run.paragraph and spliced:
                    first = spliced[0]
                    if first.paragraph is None:
                        first.paragraph = copy.deepcopy(run.paragraph)
                        if run.raw_data and "2" in run.raw_data:
                            if first.raw_data is None:
                                first.raw_data = {}
                            first.raw_data["2"] = copy.deepcopy(run.raw_data["2"])
                            if "1" in first.raw_data:
                                first.raw_data["1"] = first.length
                new_runs.extend(spliced)
                replacement_inserted = True

            # Keep the part after the search
            if run_end > search_end:
                after_len = run_end - search_end
                after_run = copy.deepcopy(run)
                after_run.length = after_len
                if after_run.raw_data and "1" in after_run.raw_data:
                    after_run.raw_data["1"] = after_len
                # Paragraph style belongs to the start of the paragraph,
                # not this trailing fragment
                after_run.paragraph = None
                if after_run.raw_data and "2" in after_run.raw_data:
                    del after_run.raw_data["2"]
                new_runs.append(after_run)

        current_pos = run_end

    # If search was at the very end, replacement might not have been inserted
    if not replacement_inserted:
        new_runs.extend(copy.deepcopy(replacement_runs))

    # Remove zero-length runs
    return [r for r in new_runs if r.length > 0]


def _has_formatting(runs: list[AttributeRun]) -> bool:
    """Check if any runs have non-default formatting (bold, italic, etc.)."""
    for run in runs:
        if run.font_style != FontStyle.NORMAL:
            return True
        if run.underline or run.strikethrough:
            return True
        if run.link_url:
            return True
        if run.color:
            return True
        if run.paragraph and run.paragraph.type != ParagraphType.DEFAULT:
            return True
    return False


def find_replace(
    richtext: NotesRichtext,
    search: str,
    replace: str,
    replace_all: bool = False,
    replacement_runs: list[AttributeRun] | None = None,
    search_start: int = 0,
) -> NotesRichtext:
    """Replace text in a NotesRichtext object while preserving formatting.

    Args:
        richtext: The NotesRichtext object to modify
        search: Text to find
        replace: Replacement text (plain text extracted from markdown)
        replace_all: If True, replace all occurrences
        replacement_runs: Optional attribute runs for the replacement text
            (from markdown parsing). If provided and contain formatting,
            these runs are spliced in instead of inheriting the existing
            run's formatting. If None or all-NORMAL, existing formatting
            is inherited (plain text behavior).
        search_start: Character offset to start searching from. Matches
            before this offset are ignored — callers use it to protect the
            note's title line (see the body_only handling in
            ClipboardManager.find_replace_in_notes_richtext_variable).

    Returns:
        New NotesRichtext object with replaced text
    """
    if richtext.text.find(search, search_start) == -1:
        return richtext  # Return unchanged

    # Deep copy to avoid modifying original
    result = copy.deepcopy(richtext)

    # Determine whether to use formatted replacement runs
    use_formatted = replacement_runs is not None and _has_formatting(replacement_runs)

    # One occurrence walk for both paths — they differ only in how the runs are
    # updated: formatted replacements splice the markdown runs in, plain ones
    # stretch the existing run so the surrounding formatting is inherited.
    start = search_start
    while True:
        pos = result.text.find(search, start)
        if pos == -1:
            break

        if use_formatted:
            result.runs = _splice_runs_for_replacement(
                result.runs, pos, len(search), replacement_runs
            )
        else:
            adjust_run_lengths_for_replacement(
                result.runs,
                result.text,
                search,
                replace,
                start=start,
            )

        result.text = result.text[:pos] + replace + result.text[pos + len(search) :]
        start = pos + len(replace)

        if not replace_all:
            break

    # If the replacement contains newlines, ensure each paragraph has its
    # own attribute run. The run-length adjustment may have placed multiple
    # paragraphs under a single run, which prevents Notes from rendering
    # proper paragraph breaks.
    if "\n" in replace:
        _split_runs_at_paragraph_boundaries(result)

    return result


def toggle_checkbox(
    richtext: NotesRichtext,
    item_text: str,
) -> NotesRichtext:
    """Toggle a checkbox item's checked state.

    Finds a checklist item by its text content and toggles its checked state.

    Args:
        richtext: The NotesRichtext object to modify
        item_text: Text content of the checklist item to find

    Returns:
        New NotesRichtext object with toggled checkbox

    Raises:
        ValueError: If the checklist item is not found
    """
    # Deep copy to avoid modifying original
    result = copy.deepcopy(richtext)

    run = _find_checklist_run(result, item_text)
    if run is None:
        raise ValueError(f"Checklist item '{item_text}' not found")

    # Toggle the checked state
    run.paragraph.checklist_checked = not run.paragraph.checklist_checked
    new_val = 1 if run.paragraph.checklist_checked else 0

    # Update raw_data: field 5 is {"1": uuid_bytes, "2": 0|1}
    if run.paragraph.raw_data and "5" in run.paragraph.raw_data:
        checked_data = run.paragraph.raw_data["5"]
        if isinstance(checked_data, dict):
            checked_data["2"] = new_val
        else:
            run.paragraph.raw_data["5"] = new_val

    # Also update the run's raw_data paragraph section
    if run.raw_data and "2" in run.raw_data:
        para_raw = run.raw_data["2"]
        if isinstance(para_raw, dict) and "5" in para_raw:
            checked_data = para_raw["5"]
            if isinstance(checked_data, dict):
                checked_data["2"] = new_val
            else:
                para_raw["5"] = new_val

    return result


def checklist_item_checked(richtext: NotesRichtext, item_text: str) -> bool | None:
    """Checked state of the checklist item toggle_checkbox would flip.

    Same match rule as toggle_checkbox — the first attribute run containing
    item_text whose paragraph is a checklist — so reading this before and
    after a toggle looks at the same item. Returns None when there is no
    such item. Used to verify a toggle landed (issue #177): AppleScript body
    HTML doesn't carry checked state.
    """
    run = _find_checklist_run(richtext, item_text)
    return None if run is None else bool(run.paragraph.checklist_checked)


def _find_checklist_run(richtext: NotesRichtext, item_text: str) -> AttributeRun | None:
    """The first attribute run containing item_text whose paragraph is a
    checklist — the one match rule shared by toggle_checkbox and
    checklist_item_checked."""
    current_pos = 0
    for run in richtext.runs:
        run_end = current_pos + run.length
        if item_text in richtext.text[current_pos:run_end]:
            if run.paragraph and run.paragraph.type == ParagraphType.CHECKLIST:
                return run
        current_pos = run_end
    return None


def insert_text(
    richtext: NotesRichtext,
    position: int,
    text: str,
) -> NotesRichtext:
    """Insert text at a specific position.

    Args:
        richtext: The NotesRichtext object to modify
        position: Character position to insert at
        text: Text to insert

    Returns:
        New NotesRichtext object with inserted text
    """
    if position < 0 or position > len(richtext.text):
        raise IndexError(f"Position {position} out of range")

    # Deep copy
    result = copy.deepcopy(richtext)

    # Insert the text
    result.text = result.text[:position] + text + result.text[position:]

    # Find and adjust the run containing this position
    current_pos = 0
    for run in result.runs:
        run_end = current_pos + run.length

        if current_pos <= position <= run_end:
            # Extend this run by the inserted length
            run.length += len(text)

            # Update raw_data
            if run.raw_data and "1" in run.raw_data:
                run.raw_data["1"] = run.length

            break

        current_pos = run_end

    return result


def delete_text(
    richtext: NotesRichtext,
    start: int,
    end: int,
) -> NotesRichtext:
    """Delete text in a range.

    Args:
        richtext: The NotesRichtext object to modify
        start: Start position (inclusive)
        end: End position (exclusive)

    Returns:
        New NotesRichtext object with deleted text
    """
    if start < 0 or end > len(richtext.text) or start >= end:
        raise IndexError(f"Invalid range [{start}, {end})")

    # Deep copy
    result = copy.deepcopy(richtext)

    # Delete the text
    deleted_length = end - start
    result.text = result.text[:start] + result.text[end:]

    # Adjust run lengths
    current_pos = 0
    remaining_to_delete = deleted_length

    for run in result.runs:
        run_end = current_pos + run.length

        if remaining_to_delete > 0 and run_end > start:
            # This run is affected by the deletion
            if current_pos >= start:
                # Run starts within or after deletion range
                reduction = min(run.length, remaining_to_delete)
                run.length -= reduction
                remaining_to_delete -= reduction
            elif run_end > start:
                # Run spans the start of deletion
                overlap = min(run_end - start, remaining_to_delete)
                run.length -= overlap
                remaining_to_delete -= overlap

            # Update raw_data
            if run.raw_data and "1" in run.raw_data:
                run.raw_data["1"] = run.length

        current_pos = run_end

    return result


LINK_UTI = AttachmentType.LINK.value
_TOKEN_KEY = "tokenContentIdentifierKey"


def _link_token(attachment) -> dict | None:
    """The parsed field-34 JSON of a link attachment, or None if unusable."""
    raw = attachment.raw_data.get("34")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        token = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(token, dict) or not isinstance(token.get(_TOKEN_KEY), str):
        return None
    return token


def _token_uuid(token: dict | None) -> str:
    return (note_link_uuid(token[_TOKEN_KEY]) or "") if token else ""


def link_target_uuids(richtext: NotesRichtext) -> list[str]:
    """Upper-case target uuids of the note-link attachments, in field-6 order.

    A link whose token can't be parsed yields "".
    """
    return [
        _token_uuid(_link_token(a))
        for a in richtext.attachments
        if a.type_uti == LINK_UTI
    ]


def link_targets_from(richtext: NotesRichtext, start: int = 0) -> list[str]:
    """Upper-case target uuids of the note links at text offset >= `start`.

    In text order, pairing each link run's attachment id (run field 12.1) with
    its field-6 entry (field 2), as copied Notes richtext carries them. A run
    with no matching entry is skipped, so an unpaired link never confirms
    anything (issue #212: append checks only the appended region).
    """
    by_id = {
        str(a.raw_data.get("2")): _token_uuid(_link_token(a))
        for a in richtext.attachments
        if a.type_uti == LINK_UTI
    }
    targets: list[str] = []
    pos = 0
    for run in richtext.runs:
        ref = run.raw_data.get("12") if run.raw_data else None
        if (
            pos >= start
            and isinstance(ref, dict)
            and ref.get("2") == LINK_UTI
            and str(ref.get("1")) in by_id
        ):
            targets.append(by_id[str(ref.get("1"))])
        pos += run.length
    return targets


def relink(
    richtext: NotesRichtext,
    old_uuid: str,
    new_uuid: str,
    new_alt: str,
) -> tuple[NotesRichtext, int]:
    """Re-point every note link to old_uuid at new_uuid (issue #183).

    Rewrites the copied richtext's link attachment entries in place: field
    "7" (alt text) becomes new_alt, and only the uuid inside the field-34
    token URL changes (ownerIdentifier and creationDateKey are kept). Notes
    rebuilds the link from these fields on paste. Text and runs are untouched.

    Returns:
        (new NotesRichtext, number of link entries rewritten)
    """
    result = copy.deepcopy(richtext)
    target = old_uuid.upper()
    count = 0
    for attachment in result.attachments:
        if attachment.type_uti != LINK_UTI:
            continue
        token = _link_token(attachment)
        if _token_uuid(token) != target:
            continue
        token[_TOKEN_KEY] = retarget_note_link(token[_TOKEN_KEY], new_uuid)
        attachment.raw_data["34"] = json.dumps(token)
        attachment.raw_data["7"] = new_alt
        count += 1
    return result, count


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each newline-separated paragraph, newline excluded.

    Only "\\n" separates paragraphs, as in ``sections.load_snapshot``; a "\\r"
    or U+2028 soft break stays inside its paragraph.
    """
    spans, pos = [], 0
    for line in text.split("\n"):
        spans.append((pos, pos + len(line)))
        pos += len(line) + 1
    return spans


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _run_attachment_id(run: AttributeRun) -> str | None:
    info = run.raw_data.get("12") if run.raw_data else None
    return _text(info.get("1")) if isinstance(info, dict) else None


def _runs_between(runs: list[AttributeRun], start: int, end: int) -> list[AttributeRun]:
    """Copies of `runs` cut to [start, end). A run cut at an edge keeps its
    paragraph style on both pieces: one run can cover several paragraphs."""
    out, pos = [], 0
    for run in runs:
        lo, hi = max(pos, start), min(pos + run.length, end)
        if lo < hi:
            piece = copy.deepcopy(run)
            piece.length = hi - lo
            if piece.raw_data:
                piece.raw_data["1"] = hi - lo
            out.append(piece)
        pos += run.length
    return out


def _attachments_of(richtext: NotesRichtext, runs: list[AttributeRun]) -> list:
    ids = {_run_attachment_id(run) for run in runs} - {None}
    return [
        copy.deepcopy(a) for a in richtext.attachments if _text(a.raw_data.get("2")) in ids
    ]


def slice_richtext(richtext: NotesRichtext, start: int, end: int) -> NotesRichtext:
    """The characters [start, end) (code points) as their own richtext, with
    only the attachment entries its runs refer to."""
    runs = _runs_between(richtext.runs, start, end)
    return NotesRichtext(
        text=richtext.text[start:end],
        runs=runs,
        attachments=_attachments_of(richtext, runs),
        _archive_template=copy.deepcopy(richtext._archive_template),
        _protobuf_typedef=copy.deepcopy(richtext._protobuf_typedef),
    )


def _merge_typedef(base: dict | None, extra: dict | None) -> dict:
    """The union of two protobuf typedefs; `base` wins where both define a type."""
    merged = copy.deepcopy(base) if base else {}
    for field, definition in (extra or {}).items():
        if field not in merged:
            merged[field] = copy.deepcopy(definition)
        elif "message_typedef" in definition and "message_typedef" in merged[field]:
            merged[field]["message_typedef"] = _merge_typedef(
                merged[field]["message_typedef"], definition["message_typedef"]
            )
    return merged


def replace_range(
    richtext: NotesRichtext, start: int, end: int, fragment: NotesRichtext
) -> NotesRichtext:
    """`richtext` with the characters [start, end) replaced by `fragment`.

    The typedef is the union of both: a parsed fragment's only covers the
    fields that fragment uses, and the note's other attachments need theirs
    (issue #240).
    """
    before = _runs_between(richtext.runs, 0, start)
    after = _runs_between(richtext.runs, end, len(richtext.text))
    result = NotesRichtext(
        text=richtext.text[:start] + fragment.text + richtext.text[end:],
        runs=before + copy.deepcopy(fragment.runs) + after,
        attachments=_attachments_of(richtext, before + after)
        + copy.deepcopy(fragment.attachments),
        _archive_template=copy.deepcopy(richtext._archive_template),
        _protobuf_typedef=_merge_typedef(
            richtext._protobuf_typedef, fragment._protobuf_typedef
        ),
    )
    result.validate()
    return result
