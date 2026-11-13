"""Pure comparisons for content-aware write verification (issues #200, #196).

After a write, ``ui_operations`` copies the note back out of the editor and
asks these helpers whether what we pasted is actually there. Each
``unconfirmed_*`` returns a list of short human-readable items: empty means
the content is confirmed. Whether the note *changed at all* is a separate
signal (``differs`` plus body HTML), so a write whose text was already in the
note (bolding a word) isn't mistaken for a no-op, and one that never landed
isn't mistaken for success.

The ``{__NG_<TYPE>_<n>__}`` placeholders the markdown converter leaves for
tags and tables become native attachments only after the pending keyboard
actions run, so they compare as wildcards here, and a placeholder still
literally in the note is reported. Links are pasted as real attachments
(issue #212): their U+FFFC compares as text, and each link is confirmed by
its target uuid.

No UI, no I/O: unit-tested in CI.
"""

import re
from collections import Counter

from .richtext.models import NotesRichtext, ParagraphType
from .richtext.operations import link_target_uuids, link_targets_from
from .title_scope import body_start_offset, body_text

_PLACEHOLDER = re.compile(r"\{__NG_[A-Z]+_\d+__\}")
_NBSP = "\u00a0"


def placeholder_count(text: str | None) -> int:
    """Literal tag/table placeholders in ``text`` (issue #122). Exact in body
    HTML too: a placeholder has no character HTML escapes."""
    return len(_PLACEHOLDER.findall(text or ""))


def normalize(text: str) -> str:
    """Undo what Notes rewrites on paste: NBSP → space, trailing whitespace."""
    return text.replace(_NBSP, " ").rstrip()


def checklist_states(richtext: NotesRichtext) -> list[bool]:
    """Checked state of each checklist paragraph, in note order."""
    states: dict[int, bool] = {}
    pos = para = 0  # running paragraph index: linear in the note's length
    for run in richtext.runs:
        if run.paragraph and run.paragraph.type == ParagraphType.CHECKLIST:
            states.setdefault(para, bool(run.paragraph.checklist_checked))
        para += richtext.text.count("\n", pos, pos + run.length)
        pos += run.length
    return [states[k] for k in sorted(states)]


def _segments(expected: str) -> list[str]:
    """``expected``'s literal text between placeholders (k placeholders → k+1).

    A native table is its own block in the editor, so the newlines around a
    TABLE placeholder needn't match the payload's: they're dropped from the
    neighbouring literals and left to the wildcard.
    """
    segments: list[str] = []
    pos = 0
    text = normalize(expected)
    for m in _PLACEHOLDER.finditer(text):
        literal = text[pos : m.start()]
        table = m.group().startswith("{__NG_TABLE_")
        segments.append(literal.rstrip("\n") if table else literal)
        pos = m.end()
        if table:
            while pos < len(text) and text[pos] == "\n":
                pos += 1
    segments.append(text[pos:])
    return segments


def matches_template(actual: str, expected: str, *, suffix: bool = False) -> bool:
    """Does ``actual`` match ``expected``, placeholders as wildcards?

    Full match by default; ``suffix=True`` means ``expected`` must match the
    end of ``actual`` (an append lands at the end of the note).

    Glob matching over literal segments with ``str.find``, leftmost first —
    exact for wildcards between literals, and linear (no backtracking regex:
    a note with many links must not blow up the verify step).
    """
    segments = _segments(expected)
    actual = normalize(actual)
    if suffix:
        segments = ["", *segments]  # a leading wildcard
    first, last = segments[0], segments[-1]
    if len(segments) == 1:
        return actual == first
    if not actual.startswith(first) or not actual.endswith(last):
        return False
    pos, end = len(first), len(actual) - len(last)
    if pos > end:
        return False
    for segment in segments[1:-1]:
        found = actual.find(segment, pos, end)
        if found == -1:
            return False
        pos = found + len(segment)
    return True


def leftover_placeholders(actual: str, expected: str) -> list[str]:
    """Placeholders from ``expected`` still literally present in ``actual``."""
    return [p for p in _PLACEHOLDER.findall(expected) if p in actual]


def differs(before: NotesRichtext | None, after: NotesRichtext) -> bool:
    """Did the text or a checked state change? ``before=None`` is no evidence."""
    if before is None:
        return False
    return normalize(before.text) != normalize(after.text) or checklist_states(
        before
    ) != checklist_states(after)


def _checkbox_items(actual: list[bool], expected: list[bool]) -> list[str]:
    if len(actual) != len(expected):
        return [f"expected {len(expected)} checklist item(s), found {len(actual)}"]
    for i, (got, want) in enumerate(zip(actual, expected), start=1):
        if got != want:
            state = lambda b: "checked" if b else "unchecked"  # noqa: E731
            return [f"checkbox {i} is {state(got)}, expected {state(want)}"]
    return []


def _placeholder_items(actual: str, expected: str) -> list[str]:
    return [f"{p} still in the note" for p in leftover_placeholders(actual, expected)]


def unconfirmed_links(
    actual: NotesRichtext, expected: NotesRichtext, *, actual_from: int = 0
) -> list[str]:
    """Every link target in ``expected`` must be a link in ``actual`` (#212).

    Counted per uuid, so a link to the wrong note (or a bare U+FFFC left
    where a link didn't land) is reported. Only ``actual``'s links at text
    offset >= ``actual_from`` count, so an append's check can't be satisfied
    by a link already earlier in the note. Never names the note.
    """
    want = Counter(link_target_uuids(expected))
    if not want:
        return []
    have = Counter(link_targets_from(actual, actual_from))
    missing = sum((want - have).values())
    if not missing:
        return []
    return [f"{missing} of {sum(want.values())} note link(s) not confirmed"]


def _tail_lines(text: str, count: int) -> str:
    lines = normalize(text).split("\n")
    return "\n".join(lines[-count:])


def _suffix_start(actual: str, expected: str) -> int:
    """Where the latest suffix match of ``expected`` starts in ``actual``.

    Matches the literal segments right to left (``rfind``), so the start is
    the appended region itself, however many lines a table added in between.
    A TABLE wildcard first consumes the table's own U+FFFC, so that object
    character is never taken for a link in the literal before it. 0 when
    there is no match (the text check reports that on its own).
    """
    segments = _segments(expected)
    kinds = [m.group() for m in _PLACEHOLDER.finditer(normalize(expected))]
    text = normalize(actual)
    if not text.endswith(segments[-1]):
        return 0
    end = len(text) - len(segments[-1])
    for kind, segment in zip(reversed(kinds), reversed(segments[:-1])):
        if kind.startswith("{__NG_TABLE_"):
            end = text.rfind("\ufffc", 0, end)
            if end == -1:
                return 0
        found = text.rfind(segment, 0, end)
        if found == -1:
            return 0
        end = found
    return end


def _tail_offset(text: str, count: int) -> int:
    """Offset in ``text`` where its last ``count`` lines start (see _tail_lines)."""
    stripped = normalize(text)
    cut = len(stripped)
    for _ in range(count):
        cut = stripped.rfind("\n", 0, cut)
        if cut == -1:
            return 0
    return cut + 1


def unconfirmed_append(after: NotesRichtext, payload: NotesRichtext) -> list[str]:
    """The appended text must end the note.

    A payload of k lines (no table) must match the note's last k lines in
    full, so text that was already earlier in the note can't satisfy it, not
    even around a trailing placeholder. A table adds lines in the editor, so
    a payload with one falls back to a suffix match.
    """
    items = _placeholder_items(after.text, payload.text)
    expected = normalize(payload.text)
    if "{__NG_TABLE_" in expected:
        ok = matches_template(after.text, expected, suffix=True)
    else:
        ok = matches_template(_tail_lines(after.text, expected.count("\n") + 1), expected)
    if not ok:
        items.append("the appended text isn't at the end of the note")
    if "{__NG_TABLE_" in expected:
        tail = _suffix_start(after.text, expected)
    else:
        tail = _tail_offset(after.text, expected.count("\n") + 1)
    return items + unconfirmed_links(after, payload, actual_from=tail)


def unconfirmed_replace(after: NotesRichtext, payload: NotesRichtext) -> list[str]:
    """The note body must equal what was pasted, checked states included."""
    body = body_text(after.text)
    items = _placeholder_items(body, payload.text)
    if not matches_template(body, payload.text):
        items.append("the note body doesn't match the new text")
    items += _checkbox_items(checklist_states(after), checklist_states(payload))
    return items + unconfirmed_links(
        after, payload, actual_from=body_start_offset(after.text)
    )


def unconfirmed_find_replace(
    after: NotesRichtext, expected: NotesRichtext, old_text: str
) -> list[str]:
    """The note must equal the richtext we pasted (``expected``).

    The ``old_text`` count in the body is reported on its own: it must equal
    the count in ``expected`` — the replacement count, net of any occurrences
    the replacement itself brings back.
    """
    items = _placeholder_items(after.text, expected.text)
    old = old_text.replace(_NBSP, " ")
    want = body_text(expected.text).replace(_NBSP, " ").count(old)
    got = body_text(after.text).replace(_NBSP, " ").count(old)
    if old and got != want:
        items.append(f"old_text appears {got} time(s), expected {want}")
    elif not matches_template(after.text, expected.text):
        items.append("the note does not match the replaced text")
    items += _checkbox_items(checklist_states(after), checklist_states(expected))
    return items + unconfirmed_links(after, expected)
