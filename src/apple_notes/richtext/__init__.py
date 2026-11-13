"""Apple Notes richtext format (com.apple.notes.richtext) parsing and serialization.

This module provides tools to parse, modify, and serialize Apple Notes'
proprietary richtext clipboard format, enabling arbitrary edits like
inserting lines, toggling checkboxes, and changing formatting.
"""

from .models import (
    AttributeRun,
    Attachment,
    FontStyle,
    NotesRichtext,
    ParagraphStyle,
    ParagraphType,
)
from .operations import (
    adjust_run_lengths_for_replacement,
    checklist_item_checked,
    delete_text,
    find_replace,
    insert_text,
    link_target_uuids,
    paragraph_spans,
    relink,
    replace_range,
    slice_richtext,
    toggle_checkbox,
)
from .parser import NotesRichtextParser
from .section_fragment import (
    FragmentError,
    LinkTarget,
    build_append,
    build_find_replace,
    build_replacement,
    synthesize_link,
)
from .serializer import NotesRichtextSerializer
from .markdown_converter import (
    LinkResolver,
    MarkdownConversionError,
    MarkdownDocument,
    MarkdownLine,
    PendingKeyboardAction,
    RichtextConversionResult,
    TextSpan,
    build_richtext,
    markdown_to_richtext,
    markdown_to_richtext_new,
    parse_markdown,
)

__all__ = [
    "FragmentError",
    "LinkTarget",
    "build_append",
    "build_find_replace",
    "build_replacement",
    "synthesize_link",
    "AttributeRun",
    "Attachment",
    "FontStyle",
    "LinkResolver",
    "MarkdownConversionError",
    "MarkdownDocument",
    "MarkdownLine",
    "NotesRichtext",
    "NotesRichtextParser",
    "NotesRichtextSerializer",
    "ParagraphStyle",
    "ParagraphType",
    "PendingKeyboardAction",
    "RichtextConversionResult",
    "TextSpan",
    "build_richtext",
    "adjust_run_lengths_for_replacement",
    "delete_text",
    "find_replace",
    "insert_text",
    "markdown_to_richtext",
    "markdown_to_richtext_new",
    "parse_markdown",
    "link_target_uuids",
    "paragraph_spans",
    "replace_range",
    "slice_richtext",
    "relink",
    "checklist_item_checked",
    "toggle_checkbox",
]
