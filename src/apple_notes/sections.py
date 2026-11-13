"""Paragraph snapshots, outside-range diffs and section fingerprints (issue #184).

Pure data layer for keyboard-driven section editing: read a note's ZDATA into
paragraphs (raw text split on newlines, never markdown offsets), compare the
paragraphs outside an edited range for byte-level identity, and fingerprint a
range by content only.  Must not import ``ui_scripting`` (it imports this).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from .link_tokens import note_link_uuid
from .protobuf_parser import NoteProtobufParser
from .richtext.models import ParagraphType
from .richtext.operations import LINK_UTI
from .richtext.parser import NotesRichtextParser
from .table_parser import parse_table_crdt

OBJECT_REPLACEMENT = "￼"
TABLE_UTI = "com.apple.notes.table"

_P = NoteProtobufParser
_KIND_BY_TYPE = {
    ParagraphType.DEFAULT: "body",
    ParagraphType.HEADING: "heading",
    ParagraphType.SUBHEADING: "subheading",
    ParagraphType.MONOSPACE: "mono",
    ParagraphType.BULLET_LIST: "bullet",
    ParagraphType.DASHED_LIST: "dashed",
    ParagraphType.NUMBERED_LIST: "numbered",
    ParagraphType.CHECKLIST: "checklist",
    ParagraphType.BLOCK_QUOTE: "quote",
}
# Field 2 -> 4 is Apple's indent amount (field 2 -> 2 is alignment).
_FIELD_INDENT = "4"


class SectionError(ValueError):
    """A caller-facing refusal; the message is shown verbatim."""


class MediaCapExceeded(SectionError):
    """The note holds too much media for a whole-note edit."""


class NoteBusyError(SectionError):
    """The note changed or isn't saved yet; nothing was written, a retry is safe."""


@dataclass(frozen=True)
class AttachmentRef:
    id: str
    uti: str
    target: str | None
    alt: str | None
    # Tables: hash of shape + sorted cells. Block attachments: file size.
    content: str | None


@dataclass(frozen=True)
class Paragraph:
    index: int
    start: int
    text: str  # includes the trailing "\n" if any
    kind: str
    indent: int
    checked: bool | None
    atts: tuple[AttachmentRef, ...]
    run_sigs: tuple[str, ...]


@dataclass(frozen=True)
class NoteSnapshot:
    pk: int
    paragraphs: tuple[Paragraph, ...]
    # Rendered markdown line(s) per paragraph; filled in by a renderer, else None.
    rendered: tuple[str | None, ...]


def _s(value) -> str:
    return (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )


def _int(value) -> int:
    if isinstance(value, bytes):
        return int.from_bytes(value, "little")
    return int(value or 0)


def _run_sig(run: dict, clipped_len: int) -> str:
    """Length + every run field except length and the paragraph uuid (2 -> 9).

    A paragraph style that is empty (once 2 -> 9 is dropped) counts as
    absent: Notes adds and drops an explicit empty style on paste and undo
    (spike Q3; seen live as ``"2": {}`` vanishing after a correct Cmd+Z), and
    the two mean the same thing."""
    body = {k: v for k, v in run.items() if k != _P.FIELD_RUN_LENGTH}
    style = body.get(_P.FIELD_PARAGRAPH_STYLE)
    if isinstance(style, dict):
        style = {k: v for k, v in style.items() if k != _P.FIELD_RUN_LINK}
        if style:
            body[_P.FIELD_PARAGRAPH_STYLE] = style
        else:
            del body[_P.FIELD_PARAGRAPH_STYLE]
    return f"{clipped_len}:{json.dumps(body, sort_keys=True, default=_hex)}"


def _hex(value):
    return value.hex() if isinstance(value, bytes) else str(value)


def _table_content(data: bytes) -> str:
    """Hash of a table's shape and sorted cells. A paste rebuilds the table
    object, so raw bytes can't be compared. Cells are still compared sorted:
    reads place them by crRows/crColumns since #235, but an in-order compare
    hasn't been proven across live edits yet. Raw bytes only when the table
    can't be parsed."""
    grid = parse_table_crdt(data)
    if grid is None:
        return hashlib.sha256(data).hexdigest()[:12]
    shape = [len(grid), max((len(row) for row in grid), default=0)]
    cells = sorted(cell for row in grid for cell in row)
    return hashlib.sha256(json.dumps([shape, cells]).encode("utf-8")).hexdigest()[:12]


def _style_of(run: dict | None) -> tuple[str, int, bool | None]:
    style = run.get(_P.FIELD_PARAGRAPH_STYLE) if run else None
    if not isinstance(style, dict):
        return "body", 0, None
    if _int(style.get(_P.FIELD_BLOCK_QUOTE)) == 1:
        ptype = ParagraphType.BLOCK_QUOTE
    else:
        ptype = NotesRichtextParser.PARAGRAPH_TYPE_MAP.get(
            _int(style.get(_P.FIELD_PARAGRAPH_TYPE)), ParagraphType.DEFAULT
        )
    kind = _KIND_BY_TYPE[ptype]
    checked = None
    if ptype == ParagraphType.CHECKLIST:
        info = style.get(_P.FIELD_CHECKLIST)
        checked = bool(
            _int(info.get(_P.FIELD_CHECKLIST_CHECKED)) if isinstance(info, dict) else 0
        )
    return kind, _int(style.get(_FIELD_INDENT)), checked


def load_snapshot(
    db, pk: int, render: Callable[[int], list[str | None]] | None = None
) -> NoteSnapshot:
    """Read note ``pk`` into paragraphs.  ``render(pk)`` yields one rendered
    string (or None) per paragraph; omitted, ``rendered`` is all None."""
    zdata = db.get_note_data(pk)
    if not zdata:
        raise SectionError("Note has no content to read")
    text, runs = NoteProtobufParser().parse(zdata)
    rows = db.get_attachments_for_note(pk)
    block_sizes = db.get_block_attachment_sizes(pk)

    spans, pos = [], 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        length = _int(run.get(_P.FIELD_RUN_LENGTH))
        spans.append((pos, pos + length, run))
        pos += length

    paragraphs: list[Paragraph] = []
    lines = text.split("\n")
    start = 0
    for i, line in enumerate(lines):
        piece = line + ("\n" if i < len(lines) - 1 else "")
        end = start + len(piece)
        sigs: list[str] = []
        atts: list[AttachmentRef] = []
        first_run = None
        for s, e, run in spans:
            lo, hi = max(s, start), min(e, end)
            if lo >= hi:
                continue
            if first_run is None:
                first_run = run
            sigs.append(_run_sig(run, hi - lo))
            info = run.get(_P.FIELD_RUN_ATTACHMENT_INFO)
            if isinstance(info, dict):
                ident = _s(info.get(_P.FIELD_ATTACHMENT_IDENTIFIER, b""))
                row = rows.get(ident) or {}
                uti = row.get("type_uti") or _s(
                    info.get(_P.FIELD_ATTACHMENT_TYPE_UTI, b"")
                )
                content = None
                if uti == TABLE_UTI:
                    data = db.get_table_data(ident)
                    if data:
                        content = _table_content(data)
                elif block_sizes.get(ident) is not None:
                    content = str(block_sizes[ident])
                atts.append(
                    AttachmentRef(
                        ident,
                        uti,
                        row.get("target_url"),
                        row.get("alt_text"),
                        content,
                    )
                )
        kind, indent, checked = _style_of(first_run)
        if i == 0 and first_run is not None and _style_type(first_run) == 0:
            kind = "title"
        paragraphs.append(
            Paragraph(i, start, piece, kind, indent, checked, tuple(atts), tuple(sigs))
        )
        start = end

    rendered = tuple(render(pk)) if render else (None,) * len(paragraphs)
    return NoteSnapshot(pk, tuple(paragraphs), rendered)


def _style_type(run: dict) -> int:
    style = run.get(_P.FIELD_PARAGRAPH_STYLE)
    return _int(style.get(_P.FIELD_PARAGRAPH_TYPE)) if isinstance(style, dict) else -1


def _short(text: str) -> str:
    text = text.rstrip("\n")
    return text if len(text) <= 40 else text[:40] + "..."


@dataclass(frozen=True)
class ParagraphChange:
    """One difference a diff found. ``index`` is None for a note-level
    difference (paragraph count), whose only field is its own message.
    ``str()`` quotes the paragraph's start for callers; ``redacted()`` has
    the index and field paths only, for logs (#170)."""

    index: int | None
    text: str
    fields: tuple[str, ...]

    def __str__(self) -> str:
        if self.index is None:
            return self.fields[0]
        return (
            f"paragraph {self.index} ({_short(self.text)!r}): {', '.join(self.fields)}"
        )

    def redacted(self) -> str:
        if self.index is None:
            return self.fields[0]
        return f"paragraph {self.index}: {', '.join(self.fields)}"


_ABSENT = object()


def _parse_sig(sig: str) -> tuple[int, dict]:
    length, _, body = sig.partition(":")
    return int(length), json.loads(body)


def _flat_fields(body: dict) -> dict:
    """Run fields keyed by path: a nested message's subfields as "2.3"."""
    out = {}
    for key, value in body.items():
        if isinstance(value, dict):
            for sub, sub_value in value.items():
                out[f"{key}.{sub}"] = sub_value
        else:
            out[key] = value
    return out


def run_field_changes(old_sigs: Sequence[str], new_sigs: Sequence[str]) -> list[str]:
    """The field paths (never values) that differ between two paragraphs'
    run signatures (#224, #225): "13", "2.3", "12.1", plus "run_count a->b"
    when the runs split or merge, or "lengths" when a run boundary moved."""
    old = [_parse_sig(s) for s in old_sigs]
    new = [_parse_sig(s) for s in new_sigs]
    if len(old) != len(new):
        # Split or merged runs no longer correspond one-to-one; comparing
        # them by position would name fields that never changed.
        return [f"run_count {len(old)}->{len(new)}"]
    out: set[str] = set()
    if [n for n, _ in old] != [n for n, _ in new]:
        out.add("lengths")
    for (_, a), (_, b) in zip(old, new):
        fa, fb = _flat_fields(a), _flat_fields(b)
        for key in fa.keys() | fb.keys():
            if fa.get(key, _ABSENT) != fb.get(key, _ABSENT):
                out.add(key)
    return sorted(out)


def _drop_last_char(p: Paragraph) -> Paragraph:
    """``p`` as it reads once its trailing newline is gone."""
    sigs = list(p.run_sigs)
    if sigs:
        length, _, rest = sigs[-1].partition(":")
        if int(length) <= 1:
            sigs.pop()
        else:
            sigs[-1] = f"{int(length) - 1}:{rest}"
    return Paragraph(
        p.index, p.start, p.text[:-1], p.kind, p.indent, p.checked, p.atts, tuple(sigs)
    )


def without_final_empty_paragraph(snap: NoteSnapshot) -> NoteSnapshot:
    """``snap`` as it reads without its empty final paragraph (and so without
    the newline before it)."""
    kept = list(snap.paragraphs[:-1])
    if kept and kept[-1].text.endswith("\n"):
        kept[-1] = _drop_last_char(kept[-1])
    return NoteSnapshot(snap.pk, tuple(kept), snap.rendered[: len(kept)])


# A whole-note paste re-copies all of a note's media (issue #240, measured: a
# 13.7 MB note grew 13.7 MB per edit until purge).
MAX_NOTE_MEDIA_BYTES = 25_000_000


def refuse_heavy_media(db, pk: int, name: str) -> None:
    """Raise MediaCapExceeded when note ``pk`` holds more media than the cap."""
    size = db.note_media_bytes(pk)
    if size > MAX_NOTE_MEDIA_BYTES:
        raise MediaCapExceeded(
            f"'{name}' holds {size / 1e6:.0f} MB of images and files; a section edit "
            f"re-copies them all on every write, so edits are limited to "
            f"{MAX_NOTE_MEDIA_BYTES // 1_000_000} MB. Use update_note_content with "
            "operation 'append' to add to the end of the note."
        )


# --- Content-level comparison (issues #240, #237) -----------------------------

# Run fields left out of the formatting comparison: 13 edit timestamp, 12
# attachment id (attachments are compared on their own), 2.1 paragraph type
# (compared as kind), 2.3 a flag Notes rewrites on display, 2.5 checklist data
# (compared as checked; its item id is reissued by a paste), 2.9 paragraph uuid.
_NOT_CONTENT = frozenset({"13", "12", "2.1", "2.3", "2.5", "2.9"})


def _content_fields(body: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in body.items():
        path = f"{prefix}{key}"
        if path in _NOT_CONTENT:
            continue
        if isinstance(value, dict):
            value = _content_fields(value, f"{path}.")
            if not value:
                continue
        out[key] = value
    return out


def _merged_formatting(runs) -> list[str]:
    """(length, run fields) pairs as signatures without non-content fields,
    adjacent equal runs merged: a run split or merge (#237) is not a change."""
    merged: list[list] = []
    for length, body in runs:
        fields = json.dumps(_content_fields(body), sort_keys=True, default=_hex)
        if merged and merged[-1][1] == fields:
            merged[-1][0] += length
        else:
            merged.append([length, fields])
    return [f"{length}:{fields}" for length, fields in merged]


def _formatting(p: Paragraph) -> list[str]:
    return _merged_formatting(_parse_sig(sig) for sig in p.run_sigs)


def note_formatting(snap: NoteSnapshot) -> list[str]:
    """A whole note's formatting, comparable with ``richtext_formatting``."""
    return _merged_formatting(
        _parse_sig(sig) for p in snap.paragraphs for sig in p.run_sigs
    )


def richtext_formatting(richtext) -> list[str]:
    """The same view of a copied note's whole richtext."""
    return _merged_formatting(
        (run.length, {k: v for k, v in (run.raw_data or {}).items() if k != "1"})
        for run in richtext.runs
    )


def _attachment_content(att: AttachmentRef) -> tuple:
    if att.uti == LINK_UTI:
        # The target, not the alt text: a renamed target isn't damage.
        return (att.uti, link_uuid(att) or att.alt)
    if att.uti and "inlinetextattachment" in att.uti:
        return (att.uti, att.alt)
    return (att.uti, att.content)


def _content_kind(p: Paragraph) -> str:
    """A whole paste gives an unstyled first line the title style."""
    return "title" if p.index == 0 and p.kind == "body" else p.kind


def _content_differs(a: Paragraph, b: Paragraph) -> list[str]:
    fields = []
    if a.text != b.text:
        fields.append("text")
    if _content_kind(a) != _content_kind(b):
        fields.append("kind")
    if a.indent != b.indent:
        fields.append("indent")
    if a.checked != b.checked:
        fields.append("checked")
    old, new = _formatting(a), _formatting(b)
    if old != new:
        fields.append(f"formatting [{', '.join(run_field_changes(old, new))}]")
    if [_attachment_content(x) for x in a.atts] != [_attachment_content(x) for x in b.atts]:
        fields.append("atts")
    return fields


def content_changes(
    before: NoteSnapshot,
    after: NoteSnapshot,
    a: int,
    b: int,
    *,
    prev_newline_removed: bool = False,
) -> list[ParagraphChange]:
    """Content differences outside the edited range [a, b) of ``before``:
    the first ``a`` paragraphs positionally, the last ``N - b`` from the end.
    ``prev_newline_removed`` tolerates paragraph ``a - 1`` losing its trailing
    newline (a section that ended the note was deleted)."""
    suffix = len(before.paragraphs) - b
    if len(after.paragraphs) < a + suffix:
        shrank = (
            f"paragraph count shrank below the untouched ends "
            f"({len(after.paragraphs)} < {a + suffix})"
        )
        return [ParagraphChange(None, "", (shrank,))]
    pairs = [(before.paragraphs[i], after.paragraphs[i]) for i in range(a)]
    for k in range(1, suffix + 1):
        pairs.append((before.paragraphs[-k], after.paragraphs[-k]))
    out: list[ParagraphChange] = []
    for old, new in pairs:
        if prev_newline_removed and old.index == a - 1 and old.text.endswith("\n"):
            old = _drop_last_char(old)
        fields = _content_differs(old, new)
        if fields:
            out.append(ParagraphChange(old.index, old.text, tuple(fields)))
    return out


def whole_note_content_changes(
    before: NoteSnapshot, after: NoteSnapshot
) -> list[ParagraphChange]:
    """Every content difference between two states of a note. Same paragraph
    count: by position. Otherwise aligned by text first, so one added or removed
    paragraph is reported as that, not as a shift of every later paragraph."""

    def paired(pairs) -> list[ParagraphChange]:
        out = []
        for old, new in pairs:
            fields = _content_differs(old, new)
            if fields:
                out.append(ParagraphChange(old.index, old.text, tuple(fields)))
        return out

    old_p, new_p = before.paragraphs, after.paragraphs
    if len(old_p) == len(new_p):
        return paired(zip(old_p, new_p))
    count = f"paragraph count {len(old_p)} -> {len(new_p)}"
    out = [ParagraphChange(None, "", (count,))]
    matcher = difflib.SequenceMatcher(
        None, [p.text for p in old_p], [p.text for p in new_p], autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        out += paired(zip(old_p[i1:i2], new_p[j1:j2]))
        if tag in ("delete", "replace"):
            out += [ParagraphChange(p.index, p.text, ("removed",)) for p in old_p[i1 + (j2 - j1):i2]]
        if tag in ("insert", "replace"):
            out += [ParagraphChange(p.index, p.text, ("added",)) for p in new_p[j1 + (i2 - i1):j2]]
    return out


def run_style(run: dict | None) -> tuple[str, int, bool | None]:
    """(kind, indent, checked) a raw run's paragraph style gives, read the
    way ``load_snapshot`` reads it; ("body", 0, None) when it has none."""
    return _style_of(run)


def link_uuid(att: AttachmentRef) -> str | None:
    """Lower-case target uuid of a note-link attachment, else None."""
    target = note_link_uuid(att.target)
    return target.lower() if target else None


def _raw_content(p: Paragraph) -> str:
    text = p.text
    for att in p.atts:
        text = text.replace(OBJECT_REPLACEMENT, att.alt or att.target or "", 1)
    return text


def section_fingerprint(snap: NoteSnapshot, a: int, b: int) -> str:
    """SHA-256 over content only: rendered lines, kind/indent/checked, link
    target uuids and table hashes.  Ignores attachment ids, timestamps and
    paragraph uuids, so it survives a re-stamp of identical content."""
    digest = hashlib.sha256()
    for i in range(a, b):
        p = snap.paragraphs[i]
        rendered = snap.rendered[i]
        record = [
            rendered if rendered is not None else _raw_content(p),
            p.kind,
            p.indent,
            p.checked,
            [link_uuid(att) for att in p.atts],
            [att.content if att.uti == TABLE_UTI else None for att in p.atts],
        ]
        digest.update(json.dumps(record).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class Section:
    a: int  # first paragraph of the section (inclusive)
    b: int  # one past the last paragraph
    heading_index: int | None
    fingerprint: str


_HEADING_LEVEL = {"heading": 1, "subheading": 2}
_HEADING_PREFIX_RE = re.compile(r"^#+\s*")


def _heading_title(rendered: str | None) -> str | None:
    if rendered is None:
        return None
    return _HEADING_PREFIX_RE.sub("", rendered).strip()


def resolve_section(
    snap: NoteSnapshot,
    name: str,
    *,
    heading: str | None,
    section_text: str | None,
) -> Section:
    """Resolve a section to paragraphs [a, b) by heading or exact rendered text."""
    if (heading is None) == (section_text is None):
        raise SectionError("Pass exactly one of heading or section_text.")
    paragraphs = snap.paragraphs
    n = len(paragraphs)
    if heading is not None:
        want = heading.strip()
        hits = [
            p.index
            for p in paragraphs
            if p.index >= 1
            and p.kind in _HEADING_LEVEL
            and _heading_title(snap.rendered[p.index]) == want
        ]
        if not hits:
            raise SectionError(f"Heading '{heading}' not found in note '{name}'.")
        if len(hits) > 1:
            raise SectionError(
                f"Heading '{heading}' appears {len(hits)} times in note "
                f"'{name}'; use section_text instead."
            )
        h = hits[0]
        level = _HEADING_LEVEL[paragraphs[h].kind]
        b = n
        for p in paragraphs[h + 1 :]:
            if _HEADING_LEVEL.get(p.kind, 99) <= level:
                b = p.index
                break
        a, heading_index = h + 1, h
    else:
        wanted = section_text.split("\n")
        while wanted and not wanted[-1].strip():
            wanted.pop()
        wanted = [line.rstrip() for line in wanted]
        rendered = [r.rstrip() if r is not None else None for r in snap.rendered]
        hits = (
            [
                i
                for i in range(1, n - len(wanted) + 1)
                if rendered[i : i + len(wanted)] == wanted
            ]
            if wanted
            else []
        )
        if not hits:
            raise SectionError(
                f"section_text not found as whole paragraphs in note '{name}'. "
                "Copy whole lines exactly as read_note_content shows them."
            )
        if len(hits) > 1:
            raise SectionError(
                f"section_text matches {len(hits)} places in note '{name}'; "
                "include more lines to make it unique."
            )
        a, b, heading_index = hits[0], hits[0] + len(wanted), None
    return Section(a, b, heading_index, section_fingerprint(snap, a, b))


# --- Edit ranges -------------------------------------------------------------

Key = tuple[int, str | None]  # (key code, AppleScript modifiers)

_CMD = "command down"

# The only keys the section path may send: select all, copy, paste, undo, and
# Left to collapse a selection.
SECTION_KEY_ALLOWLIST: frozenset[Key] = frozenset(
    {(0, _CMD), (8, _CMD), (9, _CMD), (6, _CMD), (123, None)}
)


@dataclass(frozen=True)
class EditPlan:
    operation: str
    edit_a: int
    edit_b: int
    start: int  # the characters of the note the edit replaces, [start, end)
    end: int
    prev_newline_removed: bool
    keep_first: bool = False


def _heading_label(snap: NoteSnapshot, section: Section, name: str) -> str:
    h = section.heading_index
    if h is None:
        return name
    title = _heading_title(snap.rendered[h])
    return title if title else snap.paragraphs[h].text.strip()


def plan_edit(
    snap: NoteSnapshot, section: Section, operation: str, name: str
) -> EditPlan:
    """The edit range and the characters it replaces.

    When a replace/find_replace/append range would be empty, it is widened one
    paragraph up and ``keep_first`` is set: the fragment builder keeps paragraph
    ``edit_a`` byte-for-byte and replaces the rest (the builders need a non-empty
    slice)."""
    n = len(snap.paragraphs)
    a, b = section.a, section.b
    if operation == "append":
        a = b - 1  # the section's last paragraph (the heading when empty)
    elif a >= b:
        raise SectionError(
            f"Section under '{_heading_label(snap, section, name)}' is empty; "
            "only append is supported."
        )
    delete = operation == "delete"
    to_end = delete and b == n  # takes a-1's newline and runs to the note end
    if to_end:
        s, e = snap.paragraphs[a].start - 1, sum(len(p.text) for p in snap.paragraphs)
    elif delete:
        s, e = snap.paragraphs[a].start, snap.paragraphs[b].start
    else:
        last = snap.paragraphs[b - 1]
        s = snap.paragraphs[a].start
        e = last.start + len(last.text.removesuffix("\n"))
    keep_first = False
    if not delete and s == e:
        if a - 1 <= 0:
            raise SectionError(
                f"Section in '{name}' is blank right under the title; add a "
                "line first or use update_note_content."
            )
        a -= 1
        s = snap.paragraphs[a].start
        keep_first = True

    return EditPlan(operation, a, b, s, e, to_end, keep_first)
