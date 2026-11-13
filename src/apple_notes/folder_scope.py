"""Process-wide folder allowlist for remote mode (issue #167, amendment 4).

When set, NotesDatabase hides every note outside the allowed folders, so all
read tools are confined to them whatever arguments a caller passes. Unset (the
default, and always in stdio mode) it is a no-op.

Matching is deliberately stricter than the config-file folder filters
(``filters.matches_folder_filter`` is a substring match): a pattern allows the
folder whose full path equals it, case-insensitively, plus that folder's
subfolders. ``"Daily"`` allows ``Daily`` and ``Daily/Old`` but not
``Daily Archive`` or ``Other/Daily``.
"""

from typing import Iterable, Optional

_allowlist: Optional[tuple[str, ...]] = None
# Remote mode never serves Recently Deleted, allowlist or not (issue #167).
_remote_mode = False


def set_remote_mode(enabled: bool) -> None:
    """Remote server: hide the trash from every NotesDatabase read path."""
    global _remote_mode
    _remote_mode = bool(enabled)


def reset() -> None:
    """Back to stdio behaviour: no allowlist, no remote-mode trash guard."""
    global _allowlist, _remote_mode
    _allowlist = None
    _remote_mode = False


def set_folder_allowlist(patterns: Optional[Iterable[str]]) -> None:
    """Confine reads to these folders, or lift the confinement with None."""
    global _allowlist
    if patterns is None:
        _allowlist = None
        return
    cleaned = tuple(
        p.strip().strip("/").casefold() for p in patterns if p.strip().strip("/")
    )
    if not cleaned:
        raise ValueError(
            "folder allowlist must name at least one folder (use None for no allowlist)"
        )
    _allowlist = cleaned


def get_folder_allowlist() -> Optional[tuple[str, ...]]:
    return _allowlist


def is_active() -> bool:
    """Whether reads are confined at all (an allowlist, or remote mode)."""
    return _allowlist is not None or _remote_mode


def folder_allowed(folder_path: str) -> bool:
    """True when folder_path is an allowed folder or one of its subfolders."""
    if _allowlist is None:
        return True
    path = folder_path.casefold()
    return any(path == p or path.startswith(p + "/") for p in _allowlist)
