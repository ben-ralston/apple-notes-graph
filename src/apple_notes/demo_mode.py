"""Demo mode: a hard read-only boundary around the Notes app.

``NOTES_GRAPH_DEMO=1`` (set by ``./run demo-ui`` and ``./run demo-mcp``) means
the process is reading a synthetic library, not the user's notes. Everything
that would act on the real Mac (AppleScript, keyboard scripting, the
clipboard, the UI helper daemon, backups, Reminders) calls ``require_live``
first and refuses. The check sits in the lowest shared layers, so a new call
site inherits it; the MCP server and the Electron app also refuse up front
with a clear message.

Unset, or any other value, is normal behaviour.
"""

from __future__ import annotations

import os

from .exceptions import AppleNotesError

ENV_VAR = "NOTES_GRAPH_DEMO"
MESSAGE = (
    "{action} is not available in demo mode: the demo library is read-only "
    "and is never connected to the Notes app."
)


class DemoModeError(AppleNotesError):
    """An action that would touch the real Notes app was asked for in demo mode."""


def is_demo() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def refusal(action: str) -> str:
    return MESSAGE.format(action=action)


def require_live(action: str) -> None:
    """Raise DemoModeError if this process is in demo mode."""
    if is_demo():
        raise DemoModeError(refusal(action))
