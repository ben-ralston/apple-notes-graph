"""Convert markdown text to Apple Notes richtext format.

This module enables AI agents to write/edit Apple Notes using familiar markdown
syntax. The conversion preserves the semantic meaning while mapping to Notes'
native formatting.

Supported markdown elements:
- Checklists: `- [ ] unchecked`, `- [x] checked`
- Bold: `**text**`
- Italic: `*text*`
- Strikethrough: `~~text~~`
- Underline: `<u>text</u>`
- Color: `{color:red}text{/color}` (named colors or hex)
- Headings: `## Heading`, `### Subheading`
- Lists: `- dashed`, `* bullet`, `1. numbered`
- Block quotes: `> quote`
- Monospace: `` `code` ``
- Tables: `| A | B |\\n| --- | --- |\\n| 1 | 2 |`
- External links: `[text](url)` → clickable hyperlinks
- Internal links: `[[Note Name]]` → Note-to-note links (requires link_resolver)
- Tags: `{#tag}` → native Apple Notes tag (e.g. `{#daily-note}`). Plain `#word`
  in prose is written as literal text, not a tag.
- Nested formatting: `**~~bold strikethrough~~**`, `*<u>italic underline</u>*`, etc.

Named highlight colors: purple, pink, orange, green, blue. Hex colors near a
highlight color become that highlight; other hex values are written as a
foreground text color (field 10), which reads back as the same `{color:#RRGGBB}`.

Note: `# Title` (H1) is treated as plain text — Apple Notes automatically
makes the first line the title, so H1 has no distinct formatting.
"""

import copy
import re
import uuid as uuid_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .models import (
    Attachment,
    AttributeRun,
    FontStyle,
    NotesRichtext,
    ParagraphStyle,
    ParagraphType,
)

# Type for resolving note names to UUIDs
LinkResolver = Callable[[str], str | None]

# Regex fragment for matching URLs that may contain one level of balanced parentheses.
# Matches: "https://example.com", "https://en.wikipedia.org/wiki/Barcelona_(city)"
_URL_PATTERN = r"[^()]*(?:\([^()]*\)[^()]*)*"


@dataclass
class PendingKeyboardAction:
    """A keyboard action to perform after pasting richtext.

    Used for hashtags and tables that must be typed natively via keyboard
    automation (Apple Notes only populates the required database fields when
    the user types the trigger sequences). A ``wiki_link`` action is an
    intermediate placeholder only: every write path turns it into a
    synthesized link attachment (``section_fragment.synthesize_pending_links``,
    issue #212) before anything is pasted, and the keyboard refuses it.
    """

    placeholder: str  # e.g., "{__NG_TAG_0__}", "{__NG_TABLE_0__}"
    action_type: str  # "hashtag", "wiki_link", or "table"
    text_to_type: str  # tag name, note name, or empty for tables
    original_markdown: str  # "#todo", "[[My Note]]", or table markdown
    table_cells: list[list[str]] | None = None  # 2D grid for table actions
    table_col_count: int | None = None  # Column count for table actions
    # Upper-case uuid a wiki link resolved to (#185); kept on the wire format.
    target_uuid: str | None = None


@dataclass
class RichtextConversionResult:
    """Result of converting markdown to richtext.

    Contains both the richtext binary data and any pending keyboard
    actions needed for wiki links and hashtags.
    """

    data: bytes
    pending_actions: list[PendingKeyboardAction]


def _make_placeholder(type_code: str, index: int) -> str:
    """Generate a unique placeholder string for keyboard actions."""
    return f"{{__NG_{type_code}_{index}__}}"


class MarkdownConversionError(Exception):
    """Error converting markdown to richtext."""

    pass


@dataclass
class TextSpan:
    """A span of text with formatting."""

    text: str
    font_style: FontStyle = FontStyle.NORMAL
    underline: bool = False
    strikethrough: bool = False
    color: str | None = None
    link_url: str | None = None
    note_link: str | None = None
    hashtag: str | None = None


@dataclass
class TableBlock:
    """A parsed markdown table."""

    cells: list[list[str]]  # 2D grid (rows of cells)
    col_count: int


@dataclass
class MarkdownLine:
    """A parsed markdown line with paragraph style and formatted content."""

    spans: list[TextSpan] = field(default_factory=list)
    paragraph_type: ParagraphType = ParagraphType.DEFAULT
    checklist_checked: bool = False
    table: TableBlock | None = None  # Set when line represents a table attachment

    @property
    def text(self) -> str:
        """Get the plain text content of this line."""
        return "".join(span.text for span in self.spans)

    @property
    def length(self) -> int:
        """Get the total character length."""
        return sum(len(span.text) for span in self.spans)


@dataclass
class MarkdownDocument:
    """A parsed markdown document."""

    lines: list[MarkdownLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Get the full plain text with newlines."""
        return "\n".join(line.text for line in self.lines)


def _combine_font_style(a: FontStyle, b: FontStyle) -> FontStyle:
    """Combine two FontStyle values using bitwise OR (they're a bitmask)."""
    return FontStyle(a.value | b.value)


# Regex for splitting inline formatting tokens.
# Order matters: check [[wiki links]] first (before [] is consumed by markdown links),
# then markdown links, then *** before ** before *.
# Uses .+? (lazy) inside formatting markers to allow nested markers.
# Tags: only the explicit {#tag} syntax fires a tag (issue #100). Plain #word in
# prose is left as literal text. Tag names allow letters/digits/_/- ([\w-]+).
_INLINE_PATTERN = re.compile(
    r"("
    r"\{#[\w-]+\}"  # explicit tag {#tag}
    r"|\[\[[^\]]+\]\]"  # wiki link
    rf"|\[[^\]]+\]\({_URL_PATTERN}\)"  # markdown link (balanced parens in URL)
    r"|\{color:(?:\w+|#[0-9A-Fa-f]{6})\}.+?\{/color\}"  # color tag (named or hex)
    r"|\*\*\*.+?\*\*\*"  # bold italic (***)
    r"|\*\*.+?\*\*"  # bold (**)
    # italic (*) — not adjacent to other *; flanking rule: the opener can't be
    # followed by whitespace and the closer can't be preceded by it, so loose
    # asterisks in prose (`5 * 3 * 2`) stay literal (NTG-007)
    r"|(?<!\*)\*(?![\s*]).+?(?<![\s*])\*(?!\*)"
    r"|~~.+?~~"  # strikethrough
    r"|<u>.+?</u>"  # underline
    r"|`[^`]+`"  # inline code
    r")"
)

_MAX_INLINE_DEPTH = 6

_COLOR_OPEN_PATTERN = re.compile(r"\{color:(?:\w+|#[0-9A-Fa-f]{6})\}")
_COLOR_CLOSE = "{/color}"


def _balanced_color_end(text: str, start: int) -> int | None:
    """Find the end (exclusive) of the balanced {/color} for the opener at
    `start`, counting nested openers. Returns None if unbalanced."""
    open_match = _COLOR_OPEN_PATTERN.match(text, start)
    if not open_match:
        return None
    depth = 1
    pos = open_match.end()
    while depth:
        close_idx = text.find(_COLOR_CLOSE, pos)
        if close_idx == -1:
            return None
        inner_open = _COLOR_OPEN_PATTERN.search(text, pos, close_idx)
        if inner_open:
            depth += 1
            pos = inner_open.end()
        else:
            depth -= 1
            pos = close_idx + len(_COLOR_CLOSE)
    return pos


def _split_inline(text: str) -> list[str]:
    """Split text into alternating literal and formatting-token parts.

    Behaves like ``_INLINE_PATTERN.split`` except that a {color:} token is
    extended from the regex's lazy first-closer extent to its balanced
    {/color}, so nested color tags recurse instead of leaking literal tag
    text and coloring the wrong range (NTG-018). Unbalanced nestings keep
    the lazy extent and degrade to literal inner tags.
    """
    parts: list[str] = []
    pos = 0
    while pos < len(text):
        match = _INLINE_PATTERN.search(text, pos)
        if not match:
            parts.append(text[pos:])
            break
        start, end = match.start(), match.end()
        if match.group(0).startswith("{color:"):
            balanced_end = _balanced_color_end(text, start)
            if balanced_end is not None:
                end = balanced_end
        if start > pos:
            parts.append(text[pos:start])
        parts.append(text[start:end])
        pos = end
    return parts


def _parse_inner(
    text: str,
    font_style: FontStyle = FontStyle.NORMAL,
    underline: bool = False,
    strikethrough: bool = False,
    color: str | None = None,
    depth: int = 0,
) -> list[TextSpan]:
    """Recursively parse inline formatting, inheriting outer formatting flags.

    Args:
        text: Text to parse
        font_style: Inherited font style from outer formatting
        underline: Inherited underline flag
        strikethrough: Inherited strikethrough flag
        color: Inherited color name
        depth: Recursion depth (capped at _MAX_INLINE_DEPTH)

    Returns:
        List of TextSpan with combined formatting from all nesting levels
    """
    if not text:
        return []

    if depth >= _MAX_INLINE_DEPTH:
        return [
            TextSpan(
                text=text,
                font_style=font_style,
                underline=underline,
                strikethrough=strikethrough,
                color=color,
            )
        ]

    spans: list[TextSpan] = []
    parts = _split_inline(text)

    for part in parts:
        if not part:
            continue

        # Explicit tag: {#tag} (plain #word is left as literal text)
        hashtag_match = re.match(r"^\{#([\w-]+)\}$", part)
        if hashtag_match:
            tag_name = hashtag_match.group(1)
            spans.append(
                TextSpan(
                    text=f"#{tag_name}",
                    font_style=font_style,
                    underline=underline,
                    strikethrough=strikethrough,
                    color=color,
                    hashtag=tag_name,
                )
            )
            continue

        # Wiki link: [[Note Name]]
        wiki_match = re.match(r"^\[\[([^\]]+)\]\]$", part)
        if wiki_match:
            note_name = wiki_match.group(1)
            spans.append(
                TextSpan(
                    text=note_name,
                    font_style=font_style,
                    underline=underline,
                    strikethrough=strikethrough,
                    color=color,
                    note_link=note_name,
                )
            )
            continue

        # Markdown link: [text](url)
        link_match = re.match(rf"^\[([^\]]+)\]\(({_URL_PATTERN})\)$", part)
        if link_match:
            spans.append(
                TextSpan(
                    text=link_match.group(1),
                    font_style=font_style,
                    underline=underline,
                    strikethrough=strikethrough,
                    color=color,
                    link_url=link_match.group(2),
                )
            )
            continue

        # Color tag: {color:name}inner{/color} or {color:#RRGGBB}inner{/color}
        color_match = re.match(
            r"^\{color:(\w+|#[0-9A-Fa-f]{6})\}(.+)\{/color\}$", part, re.DOTALL
        )
        if color_match:
            color_name = color_match.group(1)
            inner = color_match.group(2)
            spans.extend(
                _parse_inner(
                    inner, font_style, underline, strikethrough, color_name, depth + 1
                )
            )
            continue

        # Bold italic: ***inner***
        if part.startswith("***") and part.endswith("***") and len(part) > 6:
            inner = part[3:-3]
            combined = _combine_font_style(font_style, FontStyle.BOLD_ITALIC)
            spans.extend(
                _parse_inner(
                    inner, combined, underline, strikethrough, color, depth + 1
                )
            )
            continue

        # Bold: **inner**
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            inner = part[2:-2]
            combined = _combine_font_style(font_style, FontStyle.BOLD)
            spans.extend(
                _parse_inner(
                    inner, combined, underline, strikethrough, color, depth + 1
                )
            )
            continue

        # Italic: *inner*
        if part.startswith("*") and part.endswith("*") and len(part) > 2:
            inner = part[1:-1]
            combined = _combine_font_style(font_style, FontStyle.ITALIC)
            spans.extend(
                _parse_inner(
                    inner, combined, underline, strikethrough, color, depth + 1
                )
            )
            continue

        # Strikethrough: ~~inner~~
        if part.startswith("~~") and part.endswith("~~") and len(part) > 4:
            inner = part[2:-2]
            spans.extend(
                _parse_inner(inner, font_style, underline, True, color, depth + 1)
            )
            continue

        # Underline: <u>inner</u>
        if part.startswith("<u>") and part.endswith("</u>") and len(part) > 7:
            inner = part[3:-4]
            spans.extend(
                _parse_inner(inner, font_style, True, strikethrough, color, depth + 1)
            )
            continue

        # Inline code: `inner`
        if part.startswith("`") and part.endswith("`") and len(part) > 2:
            inner = part[1:-1]
            spans.append(
                TextSpan(
                    text=inner,
                    font_style=font_style,
                    underline=underline,
                    strikethrough=strikethrough,
                    color=color,
                )
            )
            continue

        # Plain text
        spans.append(
            TextSpan(
                text=part,
                font_style=font_style,
                underline=underline,
                strikethrough=strikethrough,
                color=color,
            )
        )

    return spans


def _consolidate_spans(spans: list[TextSpan]) -> list[TextSpan]:
    """Merge adjacent spans with identical formatting."""
    if not spans:
        return [TextSpan(text="")]

    consolidated: list[TextSpan] = []
    current = spans[0]

    for span in spans[1:]:
        # Each tag span becomes its own placeholder and typed tag: merging
        # two adjacent identical tags dropped one (issue #122).
        if (
            span.hashtag is None
            and span.font_style == current.font_style
            and span.underline == current.underline
            and span.strikethrough == current.strikethrough
            and span.color == current.color
            and span.link_url == current.link_url
            and span.note_link == current.note_link
            and span.hashtag == current.hashtag
        ):
            current = TextSpan(
                text=current.text + span.text,
                font_style=current.font_style,
                underline=current.underline,
                strikethrough=current.strikethrough,
                color=current.color,
                link_url=current.link_url,
                note_link=current.note_link,
                hashtag=current.hashtag,
            )
        else:
            consolidated.append(current)
            current = span

    consolidated.append(current)
    return consolidated


def parse_inline_formatting(text: str) -> list[TextSpan]:
    """Parse inline formatting into text spans.

    Handles:
    - [text](url) (external links)
    - **bold**
    - *italic*
    - ***bold italic***
    - ~~strikethrough~~
    - <u>underline</u>
    - `monospace` (inline code)
    - Arbitrary nesting (e.g., **~~bold strikethrough~~**)

    Args:
        text: Text with markdown inline formatting

    Returns:
        List of TextSpan with appropriate font styles
    """
    if not text:
        return [TextSpan(text="")]

    spans = _parse_inner(text)
    return _consolidate_spans(spans)


def parse_markdown_line(line: str) -> MarkdownLine:
    """Parse a single markdown line into a MarkdownLine.

    Detects paragraph-level styles:
    - Checklists: `- [ ] text` or `- [x] text`
    - Headings: `# text`, `## text`, `### text`
    - Lists: `- text`, `* text`, `1. text`
    - Block quotes: `> text`
    - Monospace: line is entirely `code`

    Then parses inline formatting within the content.

    Args:
        line: A single line of markdown text

    Returns:
        MarkdownLine with paragraph type and formatted spans
    """
    result = MarkdownLine()

    # Check for checklist patterns first (most specific)
    checklist_match = re.match(r"^- \[([ xX])\] (.*)$", line)
    if checklist_match:
        checked = checklist_match.group(1).lower() == "x"
        content = checklist_match.group(2)
        result.paragraph_type = ParagraphType.CHECKLIST
        result.checklist_checked = checked
        result.spans = parse_inline_formatting(content)
        return result

    # Check for headings
    heading_match = re.match(r"^(#{1,3}) (.*)$", line)
    if heading_match:
        level = len(heading_match.group(1))
        content = heading_match.group(2)

        if level == 1:
            result.paragraph_type = ParagraphType.DEFAULT  # Title is type 0 (DEFAULT)
            # Actually Notes uses a different internal type for title - let's use DEFAULT
            # for now as notes' first line is auto-titled
        elif level == 2:
            result.paragraph_type = ParagraphType.HEADING
        elif level == 3:
            result.paragraph_type = ParagraphType.SUBHEADING

        result.spans = parse_inline_formatting(content)
        return result

    # Check for dashed list (before generic bullet check)
    dashed_match = re.match(r"^- (.*)$", line)
    if dashed_match:
        content = dashed_match.group(1)
        result.paragraph_type = ParagraphType.DASHED_LIST
        result.spans = parse_inline_formatting(content)
        return result

    # Check for bullet list
    bullet_match = re.match(r"^\* (.*)$", line)
    if bullet_match:
        content = bullet_match.group(1)
        result.paragraph_type = ParagraphType.BULLET_LIST
        result.spans = parse_inline_formatting(content)
        return result

    # Check for numbered list
    numbered_match = re.match(r"^\d+\. (.*)$", line)
    if numbered_match:
        content = numbered_match.group(1)
        result.paragraph_type = ParagraphType.NUMBERED_LIST
        result.spans = parse_inline_formatting(content)
        return result

    # Check for block quote
    quote_match = re.match(r"^> (.*)$", line)
    if quote_match:
        content = quote_match.group(1)
        result.paragraph_type = ParagraphType.BLOCK_QUOTE
        result.spans = parse_inline_formatting(content)
        return result

    # Check for monospace (entire line wrapped in a single pair of backticks;
    # interior backticks mean separate inline-code spans, not one monospace line)
    monospace_match = re.match(r"^`([^`]+)`$", line)
    if monospace_match:
        content = monospace_match.group(1)
        result.paragraph_type = ParagraphType.MONOSPACE
        result.spans = [TextSpan(text=content, font_style=FontStyle.NORMAL)]
        return result

    # Default: plain text with inline formatting
    result.paragraph_type = ParagraphType.DEFAULT
    result.spans = parse_inline_formatting(line)
    return result


def _is_table_separator(line: str) -> bool:
    """Check if a line is a markdown table separator (e.g., |---|---|)."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    # Remove outer pipes and check inner cells are all dashes/colons
    inner = stripped[1:-1]
    cells = inner.split("|")
    return all(re.match(r"^\s*:?-+:?\s*$", cell) for cell in cells)


def _parse_table_block(lines: list[str]) -> TableBlock | None:
    """Parse a block of markdown table lines into a TableBlock.

    Args:
        lines: Lines that form the table (header + separator + data rows)

    Returns:
        TableBlock, or None if parsing fails
    """
    if len(lines) < 2:
        return None

    rows: list[list[str]] = []
    for line in lines:
        # Skip separator lines
        if _is_table_separator(line):
            continue
        stripped = line.strip()
        # Remove leading/trailing pipe
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        cells = [cell.strip() for cell in stripped.split("|")]
        rows.append(cells)

    if not rows:
        return None

    col_count = max(len(row) for row in rows)
    # Pad short rows so the grid is rectangular: the typed table flow tabs
    # cell by cell, and a short row shifts every later cell (issue #122).
    rows = [row + [""] * (col_count - len(row)) for row in rows]
    return TableBlock(cells=rows, col_count=col_count)


def parse_markdown(text: str) -> MarkdownDocument:
    """Parse markdown text into a MarkdownDocument.

    Detects markdown table blocks (consecutive lines starting with |)
    and converts them to single MarkdownLine entries with table data.

    Args:
        text: Full markdown text

    Returns:
        MarkdownDocument with parsed lines
    """
    raw_lines = text.split("\n")
    doc = MarkdownDocument()

    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]

        # Detect start of a markdown table (line starts with |)
        if line.strip().startswith("|"):
            table_lines = []
            while i < len(raw_lines) and raw_lines[i].strip().startswith("|"):
                table_lines.append(raw_lines[i])
                i += 1

            # Need at least a header and separator to be a table
            has_separator = any(_is_table_separator(tl) for tl in table_lines)
            if has_separator and len(table_lines) >= 2:
                table = _parse_table_block(table_lines)
                if table:
                    # Create a line with U+FFFC and attach the table data
                    table_line = MarkdownLine()
                    table_line.spans = [TextSpan(text="\ufffc")]
                    table_line.table = table
                    doc.lines.append(table_line)
                    continue

            # Not a valid table — treat lines normally
            for tl in table_lines:
                parsed = parse_markdown_line(tl)
                doc.lines.append(parsed)
            continue

        parsed = parse_markdown_line(line)
        doc.lines.append(parsed)
        i += 1

    return doc


def _message_td(parent: dict, field: str) -> dict:
    """The ``message_typedef`` of ``parent[field]``, creating the entry (or an
    empty ``message_typedef`` under an existing one) when absent."""
    entry = parent.setdefault(field, {"type": "message", "message_typedef": {}, "name": ""})
    return entry.setdefault("message_typedef", {})


def _merge_fields(td: dict, fields) -> None:
    """Add each (field, type) missing from ``td``; never replaces one."""
    for field, ftype in fields:
        td.setdefault(field, {"type": ftype, "name": ""})


def _ensure_complete_typedef(typedef: dict) -> dict:
    """Ensure the protobuf typedef covers all paragraph style fields.

    Templates may only have a subset of fields (e.g., a note with only headings
    won't have checklist fields). This extends the typedef to cover all known
    paragraph style field combinations so serialization succeeds regardless of
    what formatting types are used.

    Every level MERGES missing subfields into an existing message typedef
    rather than only creating absent ones: a template can carry a partial
    message (a foreground color with subfields 1-3 only, or the parser's
    pinned typedef (PINNED_TYPEDEF, issue #198) naming 5 -> 2 -> 5 with just
    the checklist uuid even in a note with no checklist; issue #184).
    """
    run_td = _message_td(typedef, "5")
    _merge_fields(
        run_td,
        [
            ("1", "int"),  # length
            ("5", "int"),  # font style
            ("6", "int"),  # underline
            ("7", "int"),  # strikethrough
            ("9", "bytes"),  # link URL
            ("14", "int"),  # highlight palette index
        ],
    )
    # Field 10 (color): fixed32 subfields; the hex write path always emits
    # alpha (subfield 4).
    _merge_fields(_message_td(run_td, "10"), [(f, "fixed32") for f in "1234"])

    para_td = _message_td(run_td, "2")  # paragraph style
    _merge_fields(
        para_td,
        [
            ("1", "int"),  # paragraph type
            ("2", "int"),  # indent level
            ("3", "int"),  # list marker
            ("7", "int"),  # numbered list marker
            ("8", "int"),  # block quote marker
            ("9", "bytes"),  # UUID
        ],
    )
    # Field 5: checklist state (uuid + checked)
    _merge_fields(_message_td(para_td, "5"), [("1", "bytes"), ("2", "int")])
    # Field 12: attachment info (identifier, type UTI)
    _merge_fields(_message_td(run_td, "12"), [("1", "string"), ("2", "string")])

    # Field 6: attachments
    _merge_fields(
        _message_td(typedef, "6"),
        [
            ("2", "string"),  # UUID
            ("6", "string"),  # content
            ("8", "string"),  # type UTI
            ("25", "int"),  # column count
        ],
    )

    return typedef


def _generate_uuid_bytes() -> bytes:
    """Generate a UUID as bytes for paragraph style."""
    return uuid_module.uuid4().bytes


def _build_paragraph_raw_data(
    para_type: ParagraphType, checked: bool = False, indent: int = 0
) -> dict:
    """Build raw_data dict for a ParagraphStyle.

    The protobuf encoding in the clipboard format differs from the model enum
    values. This function produces the correct raw protobuf fields based on
    analysis of real Apple Notes clipboard data:

    - Checklist: field 1=103, field 3=1, field 5={1: uuid, 2: 0|1}
    - Bullet list (star): field 1=100, field 3=1
    - Block quote: field 8=1 (no field 1)
    - Dashed list: field 1=101
    - Numbered list: field 1=102, field 3=1, field 7=1
    - Heading: field 1=1
    - Subheading: field 1=2
    - Monospace: field 1=4
    - Default: field 1=0

    Args:
        para_type: The paragraph type
        checked: Whether checklist is checked (only for CHECKLIST type)
        indent: Indent level

    Returns:
        Dictionary matching the protobuf structure
    """
    raw: dict = {}

    if para_type == ParagraphType.CHECKLIST:
        # Checklists use type 103 with field 3=1 and field 5 for checked state
        raw["1"] = 103
        raw["3"] = 1
        raw["5"] = {"1": _generate_uuid_bytes(), "2": 1 if checked else 0}
    elif para_type == ParagraphType.BULLET_LIST:
        # Bullet/star list uses type 100 with field 3=1
        raw["1"] = 100
        raw["3"] = 1
    elif para_type == ParagraphType.BLOCK_QUOTE:
        # Block quote uses field 8=1, no field 1
        raw["8"] = 1
    elif para_type == ParagraphType.DASHED_LIST:
        raw["1"] = 101
    elif para_type == ParagraphType.NUMBERED_LIST:
        raw["1"] = 102
        raw["3"] = 1
    elif para_type == ParagraphType.HEADING:
        raw["1"] = 1
    elif para_type == ParagraphType.SUBHEADING:
        raw["1"] = 2
    elif para_type == ParagraphType.MONOSPACE:
        raw["1"] = 4
    elif para_type == ParagraphType.DEFAULT:
        raw["1"] = 0

    if indent > 0:
        raw["2"] = indent

    # Field 9 is UUID - generate a new one
    raw["9"] = _generate_uuid_bytes()

    return raw


def _build_run_raw_data(
    length: int,
    font_style: FontStyle = FontStyle.NORMAL,
    paragraph_raw: Optional[dict] = None,
    link_url: Optional[str] = None,
    underline: bool = False,
    strikethrough: bool = False,
    color: Optional[str] = None,
) -> dict:
    """Build raw_data dict for an AttributeRun.

    Args:
        length: Character length this run covers
        font_style: Font style (bold, italic, etc.)
        paragraph_raw: Optional paragraph style raw data
        link_url: Optional URL for external hyperlinks (field 9)
        underline: Whether text is underlined (field 6)
        strikethrough: Whether text is struck through (field 7)
        color: Optional highlight color name (field 14). Valid: purple, pink, orange, green, blue

    Returns:
        Dictionary matching the protobuf structure
    """
    raw = {"1": length}

    if font_style != FontStyle.NORMAL:
        raw["5"] = font_style.value

    if underline:
        raw["6"] = 1

    if strikethrough:
        raw["7"] = 1

    if paragraph_raw is not None:
        raw["2"] = paragraph_raw

    if link_url is not None:
        raw["9"] = link_url.encode("utf-8")

    if color is not None:
        from ..colors import (
            VALID_COLOR_NAMES,
            color_name_to_highlight_index,
            hex_to_rgb_floats,
            rgb_floats_to_ints,
            rgb_to_color_name,
        )

        # Resolve color to a highlight palette index
        highlight_idx = color_name_to_highlight_index(color)
        rgb = None
        if highlight_idx is None and color.startswith("#"):
            rgb = hex_to_rgb_floats(color)
            if rgb is None:
                raise MarkdownConversionError(
                    f"Invalid hex color '{color}'. Expected #RRGGBB."
                )
            # Hex close to a named highlight color still becomes a highlight
            name = rgb_to_color_name(*rgb)
            if name:
                highlight_idx = color_name_to_highlight_index(name)

        if highlight_idx is not None:
            raw["14"] = highlight_idx
        elif rgb is not None:
            # Hex with no highlight match: write a field 10 foreground color
            # instead of silently dropping it (NTG-019). The read path renders
            # field 10 back as {color:#RRGGBB}, so round-trips preserve it.
            r_bits, g_bits, b_bits = rgb_floats_to_ints(*rgb)
            alpha_bits = rgb_floats_to_ints(1.0, 1.0, 1.0)[0]
            raw["10"] = {"1": r_bits, "2": g_bits, "3": b_bits, "4": alpha_bits}
        else:
            # Named color that isn't a valid highlight — error early
            valid = ", ".join(sorted(VALID_COLOR_NAMES))
            raise MarkdownConversionError(
                f"Unsupported highlight color '{color}'. "
                f"Valid colors: {valid}. "
                f"Hex colors also work: those near a highlight color become "
                f"that highlight; others are written as foreground color."
            )

    return raw


def _resolve_span_text(
    span: TextSpan,
    link_resolver: Optional[LinkResolver],
    pending_actions: list[PendingKeyboardAction],
    counters: dict[str, int],
) -> str:
    """Determine the actual display text for a span.

    For hashtag spans, generates a placeholder and records a pending
    keyboard action (the hashtag will be typed natively after paste).
    For resolved note_link spans, generates a placeholder and records a
    ``wiki_link`` action, which the caller synthesizes into a link attachment
    (``section_fragment.markdown_to_richtext_linked``, issue #212).
    Unresolved note links and external links are unchanged.

    Returns:
        The display text for this span (may be a placeholder string).
    """
    if span.hashtag:
        placeholder = _make_placeholder("TAG", counters["TAG"])
        counters["TAG"] += 1
        pending_actions.append(
            PendingKeyboardAction(
                placeholder=placeholder,
                action_type="hashtag",
                text_to_type=span.hashtag,
                original_markdown=f"{{#{span.hashtag}}}",
            )
        )
        return placeholder
    if span.note_link and link_resolver:
        resolved_uuid = link_resolver(span.note_link)
        if resolved_uuid:
            placeholder = _make_placeholder("LINK", counters["LINK"])
            counters["LINK"] += 1
            pending_actions.append(
                PendingKeyboardAction(
                    placeholder=placeholder,
                    action_type="wiki_link",
                    text_to_type=span.note_link,
                    original_markdown=f"[[{span.note_link}]]",
                    target_uuid=resolved_uuid.upper(),
                )
            )
            return placeholder
        # Unresolved: fall back to [[Note Name]] plain text
        return f"[[{span.note_link}]]"
    elif span.note_link:
        # No resolver: keep as [[Note Name]] plain text
        return f"[[{span.note_link}]]"
    return span.text


def build_richtext(
    doc: MarkdownDocument,
    template: NotesRichtext,
    link_resolver: Optional[LinkResolver] = None,
) -> tuple[NotesRichtext, list[PendingKeyboardAction]]:
    """Build a NotesRichtext from a parsed markdown document.

    Uses the template to get the archive and typedef structures needed for
    serialization.

    Args:
        doc: Parsed markdown document
        template: Template NotesRichtext to copy archive structure from
        link_resolver: Optional callable that resolves note names to UUIDs.
            When provided, [[Note Name]] spans that resolve successfully
            are converted to placeholder text. Unresolved names are kept
            as plain text.

    Returns:
        Tuple of (NotesRichtext, list of PendingKeyboardAction)
    """
    # Build text and runs together in a single pass
    # (note_link spans change text length, so we can't use doc.text directly)
    text_parts: list[str] = []
    runs: list[AttributeRun] = []
    attachments: list[Attachment] = []
    pending_actions: list[PendingKeyboardAction] = []
    counters: dict[str, int] = {"TAG": 0, "LINK": 0, "TABLE": 0}

    for line_idx, line in enumerate(doc.lines):
        is_last_line = line_idx == len(doc.lines) - 1

        # Handle table lines — insert placeholder text + PendingKeyboardAction
        # (keyboard automation inserts native tables; CRDT attachment path
        # produces empty boxes in Notes.app)
        if line.table is not None:
            table = line.table
            placeholder = _make_placeholder("TABLE", counters["TABLE"])
            counters["TABLE"] += 1

            # Reconstruct markdown for original_markdown field
            table_md_lines = []
            for row in table.cells:
                table_md_lines.append("| " + " | ".join(row) + " |")
                if len(table_md_lines) == 1:
                    table_md_lines.append("| " + " | ".join("---" for _ in row) + " |")
            original_md = "\n".join(table_md_lines)

            pending_actions.append(
                PendingKeyboardAction(
                    placeholder=placeholder,
                    action_type="table",
                    text_to_type="",
                    original_markdown=original_md,
                    table_cells=table.cells,
                    table_col_count=table.col_count,
                )
            )

            # Placeholder text + newline
            length = len(placeholder)
            if not is_last_line:
                length += 1

            run_raw = _build_run_raw_data(length)
            run = AttributeRun(length=length, raw_data=run_raw)
            runs.append(run)
            text_parts.append(placeholder)
            if not is_last_line:
                text_parts.append("\n")
            continue

        # Build paragraph style for this line
        para_raw = None
        paragraph = None

        if line.paragraph_type != ParagraphType.DEFAULT or line.checklist_checked:
            para_raw = _build_paragraph_raw_data(
                line.paragraph_type, line.checklist_checked
            )
            paragraph = ParagraphStyle(
                type=line.paragraph_type,
                checklist_checked=line.checklist_checked,
                raw_data=para_raw,
            )

        # Process spans in the line
        for span_idx, span in enumerate(line.spans):
            if not span.text and span_idx == 0:
                # Empty line - still need a run for the newline
                continue

            is_last_span = span_idx == len(line.spans) - 1

            # Resolve note links and hashtags (may replace text with placeholder)
            display_text = _resolve_span_text(
                span, link_resolver, pending_actions, counters
            )

            # Calculate length: display text + newline if not last line and last span
            length = len(display_text)
            if is_last_span and not is_last_line:
                length += 1  # Include newline in this run

            if length == 0:
                continue

            # Build run raw data
            run_raw = _build_run_raw_data(
                length,
                span.font_style,
                para_raw if span_idx == 0 else None,
                link_url=span.link_url,
                underline=span.underline,
                strikethrough=span.strikethrough,
                color=span.color,
            )

            run = AttributeRun(
                length=length,
                font_style=span.font_style,
                underline=span.underline,
                strikethrough=span.strikethrough,
                link_url=span.link_url,
                paragraph=paragraph if span_idx == 0 else None,
                raw_data=run_raw,
            )
            runs.append(run)

            # Append text
            text_parts.append(display_text)
            if is_last_span and not is_last_line:
                text_parts.append("\n")

        # Handle empty lines (no spans or only empty span)
        if not line.spans or (len(line.spans) == 1 and not line.spans[0].text):
            if not is_last_line:
                run_raw = _build_run_raw_data(1, paragraph_raw=para_raw)
                run = AttributeRun(length=1, paragraph=paragraph, raw_data=run_raw)
                runs.append(run)
                text_parts.append("\n")

    full_text = "".join(text_parts)

    # Extend the typedef to cover all paragraph style fields, since the
    # template may only have a subset (e.g., no checklists or block quotes)
    typedef = copy.deepcopy(template._protobuf_typedef)
    _ensure_complete_typedef(typedef)

    richtext = NotesRichtext(
        text=full_text,
        runs=runs,
        attachments=attachments,
        _archive_template=copy.deepcopy(template._archive_template),
        _protobuf_typedef=typedef,
    )

    return richtext, pending_actions


def markdown_to_richtext(
    markdown: str,
    template: bytes,
    link_resolver: Optional[LinkResolver] = None,
) -> RichtextConversionResult:
    """Convert markdown text to Apple Notes richtext binary format.

    This is the main entry point for markdown conversion.

    Args:
        markdown: Markdown formatted text
        template: Binary data from an existing note to use as archive template
        link_resolver: Optional callable to resolve [[Note Name]] to UUIDs

    Returns:
        RichtextConversionResult with binary data and pending keyboard actions

    Raises:
        MarkdownConversionError: If conversion fails
    """
    from .parser import NotesRichtextParser, NotesRichtextParseError
    from .serializer import NotesRichtextSerializer, NotesRichtextSerializeError

    try:
        # Parse template to get archive structure
        parser = NotesRichtextParser()
        template_richtext = parser.parse(template)

        # Parse markdown
        doc = parse_markdown(markdown)

        # Build richtext from markdown using template
        richtext, pending_actions = build_richtext(
            doc, template_richtext, link_resolver=link_resolver
        )

        # Serialize back to binary
        serializer = NotesRichtextSerializer()
        data = serializer.serialize(richtext)
        return RichtextConversionResult(data=data, pending_actions=pending_actions)

    except NotesRichtextParseError as e:
        raise MarkdownConversionError(f"Failed to parse template: {e}") from e
    except NotesRichtextSerializeError as e:
        raise MarkdownConversionError(f"Failed to serialize richtext: {e}") from e
    except Exception as e:
        raise MarkdownConversionError(f"Markdown conversion failed: {e}") from e


def _load_default_template() -> bytes:
    """Load the bundled default template for new note creation."""
    return (Path(__file__).parent / "default_template.bin").read_bytes()


def markdown_to_richtext_new(
    markdown: str,
    link_resolver: Optional[LinkResolver] = None,
) -> RichtextConversionResult:
    """Convert markdown to richtext using the bundled template (for new notes).

    This is used when creating new notes with markdown formatting, where no
    existing note data is available to use as a template.

    Args:
        markdown: Markdown formatted text
        link_resolver: Optional callable to resolve [[Note Name]] to UUIDs

    Returns:
        RichtextConversionResult with binary data and pending keyboard actions

    Raises:
        MarkdownConversionError: If conversion fails
    """
    return markdown_to_richtext(
        markdown, _load_default_template(), link_resolver=link_resolver
    )
