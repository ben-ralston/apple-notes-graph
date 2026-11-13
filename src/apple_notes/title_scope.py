"""Title-line scoping for find/replace (issue #120).

Copied Apple Notes content — whether read as `com.apple.notes.richtext`, as RTF,
or as resolved text from the database — starts with the note's **title** line.
A `find_replace` must never rewrite it: the server's exactly-once guard counts
body matches only, and the write engines have to agree with that guard or a note
silently gets renamed (audit findings NTG-003 / NTG-004).

Every layer that needs "where does the body start" goes through this module so
the guard and the engines can't drift apart.
"""

import re

# A \par control word ends at the first non-letter, so a bare substring search
# would also match \pard, \paperw, \partightenfactor … in the RTF prologue.
_RTF_PARAGRAPH_BREAK = re.compile(r"\\par(?![a-zA-Z])")


def body_start_offset(text: str) -> int:
    """Character offset where the note body starts (just past the title line).

    A note with no newline at all is title-only: the offset is the end of the
    text, so no match can ever be found in the "body".
    """
    newline = text.find("\n")
    return len(text) if newline == -1 else newline + 1


def body_text(text: str) -> str:
    """The note text with its title line removed."""
    return text[body_start_offset(text) :]


def rtf_body_start_offset(rtf_str: str) -> int:
    """Offset just past the title paragraph in an RTF document.

    Returns ``len(rtf_str)`` when the document has no paragraph break, matching
    :func:`body_start_offset`: with no body there is nothing to replace, which
    is the safe answer for a title-protection guard.
    """
    match = _RTF_PARAGRAPH_BREAK.search(rtf_str)
    return len(rtf_str) if match is None else match.end()
