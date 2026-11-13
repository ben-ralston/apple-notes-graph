"""The one parser for a note-link token (issue #247).

Notes stores a link to another note in one of two forms:

- ``applenotes:note/<uuid>?ownerIdentifier=<owner>``: our tools, older links
- ``applenotes://showNote?identifier=<uuid>``: links made by hand in Notes

Both open the same note and every reader must treat them alike. New writes keep
emitting the first form (``richtext/section_fragment.py``).
"""

import re
from typing import Optional

_NOTE_LINK_TOKEN = re.compile(
    r"^(applenotes:note/|applenotes://showNote\?identifier=)([a-f0-9-]+)", re.IGNORECASE
)


def note_link_uuid(url: Optional[str]) -> Optional[str]:
    """The target uuid of a note-link token, upper-case; None if it isn't one."""
    match = _NOTE_LINK_TOKEN.search(url or "")
    return match.group(2).upper() if match else None


def retarget_note_link(url: str, new_uuid: str) -> str:
    """``url`` pointing at ``new_uuid``, keeping its form and query. The uuid takes
    its form's case: live ``showNote`` tokens are all upper-case, ``note/`` lower."""

    def swap(match: re.Match) -> str:
        form = match.group(1)
        hand_made = "shownote" in form.lower()
        return form + (new_uuid.upper() if hand_made else new_uuid.lower())

    return _NOTE_LINK_TOKEN.sub(swap, url, count=1)
