"""Synthesized note links, and paste fragments for section edits (#184, #212).

``[[links]]`` are built directly as link attachments instead of through the
Cmd+F / Cmd+K picker flow (spike, design doc Q5): the placeholder becomes
U+FFFC, its run gets ``12 = {id, LINK_UTI}``, and a field-6 entry carries the
alt text and the ``applenotes:note/<uuid>?ownerIdentifier=<owner>`` token.
Every write path uses this (issue #212): ``markdown_to_richtext_linked`` for
add_note / append / replace, ``synthesize_pending_links`` for find_replace.

A section edit builds its new content here and splices it into the copied note
(``operations.replace_range``); new ``{#tags}`` and tables still need native
typing there, so they are refused.
"""

from __future__ import annotations

import copy
import json
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass

from .markdown_converter import (
    MarkdownConversionError,
    PendingKeyboardAction,
    RichtextConversionResult,
    _ensure_complete_typedef,
    _load_default_template,
    build_richtext,
    markdown_to_richtext,
    parse_markdown,
)
from .models import Attachment, AttributeRun, NotesRichtext
from .operations import LINK_UTI, delete_text, find_replace
from .parser import NotesRichtextParser
from .serializer import NotesRichtextSerializer

_OBJECT_REPLACEMENT = "￼"
_TAGS_TABLES_REFUSAL = (
    "New {#tags} and tables can't be added by edit_note_section yet; "
    "use update_note_content for those."
)


@dataclass(frozen=True)
class LinkTarget:
    uuid: str
    title: str
    owner: str


class FragmentError(ValueError):
    """Caller-facing: the requested fragment can't be built."""


def link_resolver_for(links: Mapping[str, LinkTarget]):
    """The converter's name -> uuid resolver for `links` (keyed by lower-case title)."""
    return lambda name: links[name.lower()].uuid if name.lower() in links else None


def _complete_typedef(richtext: NotesRichtext) -> None:
    """Make the typedef cover the link fields a synthesized link adds."""
    typedef = richtext._protobuf_typedef
    if typedef is None:
        return
    _ensure_complete_typedef(typedef)
    field_6 = typedef["6"]["message_typedef"]
    for subfield in ("7", "34"):
        field_6.setdefault(subfield, {"type": "string", "name": ""})


def _check_pending(pending: list[PendingKeyboardAction]) -> None:
    if any(p.action_type != "wiki_link" for p in pending):
        raise FragmentError(_TAGS_TABLES_REFUSAL)


def _piece(run: AttributeRun, length: int, first: bool) -> AttributeRun:
    """A copy of `run` resized to `length`; only a first piece keeps the paragraph style."""
    out = copy.deepcopy(run)
    out.length = length
    if out.raw_data:
        out.raw_data["1"] = length
    if not first:
        out.paragraph = None
        if out.raw_data:
            out.raw_data.pop("2", None)
    return out


def synthesize_link(
    richtext: NotesRichtext, placeholder: str, target: LinkTarget
) -> NotesRichtext:
    """Replace `placeholder` with a real note link to `target` (returns a copy)."""
    if not target.owner:
        raise FragmentError(
            f"Can't link to '{target.title}': no ownerIdentifier could be determined."
        )
    result = copy.deepcopy(richtext)
    start = result.text.find(placeholder)
    if start == -1:
        raise FragmentError(f"Link placeholder {placeholder} not found in the fragment")
    end = start + len(placeholder)

    # The placeholder may cross runs: a plain-text find_replace replacement
    # inherits the replaced text's run boundaries (#212 review). The link run
    # takes the style of the run the placeholder starts in; the rest of the
    # covered runs is dropped, and the run it ends in keeps its tail.
    new_runs: list[AttributeRun] = []
    attachment_id = str(uuid_module.uuid4()).upper()
    pos = 0
    linked = False
    for run in result.runs:
        run_start, run_end = pos, pos + run.length
        pos = run_end
        if run_end <= start or run_start >= end:
            new_runs.append(run)
            continue
        if run_start < start:
            new_runs.append(_piece(run, start - run_start, True))
        if not linked:
            link_run = _piece(run, 1, run_start >= start)
            link_run.link_url = None
            link_run.raw_data.pop("9", None)
            link_run.raw_data["12"] = {"1": attachment_id, "2": LINK_UTI}
            new_runs.append(link_run)
            linked = True
        if run_end > end:
            tail = _piece(run, run_end - end, False)
            tail.raw_data.pop("12", None)
            new_runs.append(tail)

    result.text = result.text[:start] + _OBJECT_REPLACEMENT + result.text[end:]
    result.runs = new_runs
    token = {
        "tokenContentIdentifierKey": (
            f"applenotes:note/{target.uuid.lower()}?ownerIdentifier={target.owner}"
        )
    }
    result.attachments.append(
        Attachment(
            uuid=attachment_id.encode("utf-8"),
            type_uti=LINK_UTI,
            content=None,
            raw_data={
                "2": attachment_id,
                "7": target.title,
                "8": LINK_UTI,
                "25": 13,
                "34": json.dumps(token),
            },
        )
    )
    _complete_typedef(result)
    result.validate()
    return result


def _synthesize_all(
    richtext: NotesRichtext,
    pending: list[PendingKeyboardAction],
    links: Mapping[str, LinkTarget],
) -> NotesRichtext:
    for action in pending:
        richtext = synthesize_link(
            richtext, action.placeholder, links[action.text_to_type.lower()]
        )
    return richtext


def _serialize(richtext: NotesRichtext) -> bytes:
    return NotesRichtextSerializer().serialize(richtext)


def synthesize_pending_links(
    richtext: NotesRichtext,
    pending: list[PendingKeyboardAction],
    links: Mapping[str, LinkTarget],
) -> tuple[NotesRichtext, list[PendingKeyboardAction]]:
    """Turn every wiki-link placeholder into a real link (issue #212).

    Returns the new richtext and the actions still left for the keyboard
    (hashtags and tables). Only names in `links` resolve to a placeholder,
    so every wiki-link action has a target here.
    """
    link_actions = [p for p in pending if p.action_type == "wiki_link"]
    rest = [p for p in pending if p.action_type != "wiki_link"]
    if not link_actions:
        return richtext, rest
    richtext = copy.deepcopy(richtext)
    _complete_typedef(richtext)
    return _synthesize_all(richtext, link_actions, links), rest


def markdown_to_richtext_linked(
    markdown: str, template: bytes, links: Mapping[str, LinkTarget]
) -> RichtextConversionResult:
    """``markdown_to_richtext`` with every resolvable ``[[link]]`` synthesized.

    `links` is keyed by lower-case title; names not in it stay plain text.
    The result's pending actions are hashtags and tables only, never links,
    so no Cmd+K picker runs (issue #212).
    """
    converted = markdown_to_richtext(
        markdown, template, link_resolver=link_resolver_for(links)
    )
    if not any(p.action_type == "wiki_link" for p in converted.pending_actions):
        return converted
    try:
        richtext, rest = synthesize_pending_links(
            NotesRichtextParser().parse(converted.data), converted.pending_actions, links
        )
        return RichtextConversionResult(data=_serialize(richtext), pending_actions=rest)
    except Exception as e:  # same contract as markdown_to_richtext
        raise MarkdownConversionError(f"Link synthesis failed: {e}") from e


def markdown_to_richtext_new_linked(
    markdown: str, links: Mapping[str, LinkTarget]
) -> RichtextConversionResult:
    """``markdown_to_richtext_linked`` on the bundled template (new notes, append)."""
    return markdown_to_richtext_linked(markdown, _load_default_template(), links)


def build_replacement(
    copied: bytes, markdown: str, links: Mapping[str, LinkTarget]
) -> bytes:
    """Richtext for `markdown`, templated on `copied`, with links synthesized."""
    converted = markdown_to_richtext(markdown, copied, link_resolver=link_resolver_for(links))
    _check_pending(converted.pending_actions)
    if not converted.pending_actions:
        return converted.data
    richtext = NotesRichtextParser().parse(converted.data)
    _complete_typedef(richtext)
    return _serialize(_synthesize_all(richtext, converted.pending_actions, links))


def build_append(
    copied: bytes, markdown: str, links: Mapping[str, LinkTarget]
) -> bytes:
    """`copied` (one or more paragraphs), a newline, then `markdown` converted
    against it.

    The copied paragraphs' runs and attachments are kept byte-identical; the
    joining newline gets its own run that carries only the last copied
    paragraph's style.  When `copied` ends with a newline its last paragraph
    is empty and its own style wasn't copied, so the joining newline (which
    ends that blank paragraph) carries no paragraph style.
    """
    base = NotesRichtextParser().parse(copied)
    new = NotesRichtextParser().parse(build_replacement(copied, markdown, links))

    newline_raw: dict = {"1": 1}
    last = base.runs[-1] if base.runs and not base.text.endswith("\n") else None
    if last is not None and last.raw_data and "2" in last.raw_data:
        newline_raw["2"] = copy.deepcopy(last.raw_data["2"])
    newline = AttributeRun(
        length=1,
        paragraph=copy.deepcopy(last.paragraph) if last is not None else None,
        raw_data=newline_raw,
    )

    result = NotesRichtext(
        text=base.text + "\n" + new.text,
        runs=copy.deepcopy(base.runs) + [newline] + copy.deepcopy(new.runs),
        attachments=copy.deepcopy(base.attachments) + copy.deepcopy(new.attachments),
        _archive_template=copy.deepcopy(base._archive_template),
        _protobuf_typedef=copy.deepcopy(base._protobuf_typedef),
    )
    _complete_typedef(result)
    return _serialize(result)


def first_paragraph(copied: bytes) -> bytes:
    """`copied` cut to its first paragraph (no trailing newline).

    A widened blank-section replace keeps only the paragraph above the blank
    line and appends the new content onto it (issue #184, R10).
    """
    richtext = NotesRichtextParser().parse(copied)
    cut = richtext.text.find("\n")
    if cut == -1:
        return copied
    trimmed = delete_text(richtext, cut, len(richtext.text))
    trimmed.runs = [run for run in trimmed.runs if run.length > 0]
    return _serialize(trimmed)


def build_find_replace(
    copied: bytes,
    old_text: str,
    new_text: str,
    links: Mapping[str, LinkTarget],
) -> bytes:
    """Replace the single occurrence of `old_text` in the copied section."""
    if not old_text:
        raise FragmentError("old_text cannot be empty")
    richtext = NotesRichtextParser().parse(copied)
    count = richtext.text.count(old_text)
    if count == 0:
        raise FragmentError("old_text not found in the section")
    if count > 1:
        raise FragmentError(
            f"old_text found {count} times in the section; "
            "include more surrounding text so it matches exactly once"
        )
    replacement, pending = build_richtext(
        parse_markdown(new_text), richtext, link_resolver=link_resolver_for(links)
    )
    _check_pending(pending)
    modified = find_replace(
        richtext,
        old_text,
        replacement.text,
        replacement_runs=replacement.runs,
    )
    _complete_typedef(modified)
    return _serialize(_synthesize_all(modified, pending, links))
