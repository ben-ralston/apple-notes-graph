"""Detect whether the macOS GUI session can receive UI-scripting events.

Writing a note via UI scripting requires the GUI session to be on-console,
past the login screen, and not locked. If any of those is false, CGEvent
keystrokes are silently dropped by loginwindow and the write appears to
succeed while doing nothing. This module exposes a single preflight that
raises loudly instead.
"""

from __future__ import annotations

from Quartz import CGSessionCopyCurrentDictionary

from .exceptions import SessionLockedError


def ui_session_available() -> bool:
    """True when UI scripting is expected to work (never raises).

    Strict, fail-closed: missing session dict, not on-console, login not
    complete, or screen locked all count as unavailable. The remote write
    queue polls this to decide when queued writes may apply (issue #169).
    """
    info = CGSessionCopyCurrentDictionary()
    return not (
        info is None
        or not info.get("kCGSSessionOnConsoleKey", False)
        or not info.get("kCGSessionLoginDoneKey", False)
        or info.get("CGSSessionScreenIsLocked", False)
    )


def require_ui_scripting_session() -> None:
    """Raise SessionLockedError unless UI scripting is expected to work.

    If we can't prove the session is scriptable, we refuse to try — the
    alternative (silent keystroke drop) is the bug this plan fixes.
    """
    if not ui_session_available():
        raise SessionLockedError(
            "macOS GUI session is unavailable for UI scripting (locked, "
            "screensaver, non-console, or pre-login). Unlock the Mac at "
            "the console and retry. Reads still work over SSH. See "
            "docs/keep-gui-unlocked-for-remote-writes.md for how to keep "
            "the session available for remote writes."
        )
