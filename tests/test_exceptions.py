"""Contract tests for the focus-guard / write-verification exceptions.

Both must subclass UIScriptingError so the existing `except UIScriptingError`
handlers keep catching them, while remaining distinct types so the MCP server
can surface their messages verbatim (caught *before* the generic wrapper).
"""

from apple_notes.exceptions import (
    NotesNotFrontmostError,
    NotesNotRespondingError,
    SectionEditDamagedError,
    SectionEditRolledBackError,
    UIScriptingError,
    WrongNoteOpenError,
    WriteVerificationError,
)
from apple_notes.ui_helper_client import _ERROR_TYPE_MAP


def test_notes_not_frontmost_is_ui_scripting_error():
    assert issubclass(NotesNotFrontmostError, UIScriptingError)


def test_daemon_client_maps_the_ui_scripting_error_types():
    """The remote (SSH) path re-raises by type NAME (issue #141).

    A type missing from the map is demoted to the bare AppleNotesError
    fallback, which no MCP tool handler catches — so the round-trip test in
    tests/test_ui_helper.py has this CI-visible companion (that module is
    macos_gui-marked and never runs in CI).
    """
    for exc_type in (
        NotesNotFrontmostError,
        NotesNotRespondingError,
        WriteVerificationError,
        WrongNoteOpenError,
        SectionEditRolledBackError,
        SectionEditDamagedError,
    ):
        assert _ERROR_TYPE_MAP.get(exc_type.__name__) is exc_type


def test_write_verification_is_ui_scripting_error():
    assert issubclass(WriteVerificationError, UIScriptingError)


def test_partial_write_error_hierarchy():
    """#200: a partial write is still a verification failure for every
    existing ``except WriteVerificationError`` handler, but a distinct type the
    remote queue maps to applied_unverified (never retried)."""
    from apple_notes.exceptions import LinksNotConfirmedError, PartialWriteError

    assert issubclass(PartialWriteError, WriteVerificationError)
    assert issubclass(LinksNotConfirmedError, PartialWriteError)


def test_error_map_has_partial_types():
    """Both must survive the UI helper daemon round-trip by name, or a remote
    partial write demotes to AppleNotesError and reads as a plain failure."""
    from apple_notes.exceptions import LinksNotConfirmedError, PartialWriteError

    assert _ERROR_TYPE_MAP["PartialWriteError"] is PartialWriteError
    assert _ERROR_TYPE_MAP["LinksNotConfirmedError"] is LinksNotConfirmedError


def test_section_edit_exception_hierarchy():
    assert issubclass(WrongNoteOpenError, NotesNotFrontmostError)
    assert issubclass(SectionEditRolledBackError, WriteVerificationError)
    assert issubclass(SectionEditDamagedError, WriteVerificationError)


def test_modal_open_is_a_not_frontmost_error():
    """A dialog in Notes means keystrokes would go to the dialog, not the
    note (issue #199): every "keystrokes refused" branch must catch it."""
    from apple_notes import NotesModalOpenError

    assert issubclass(NotesModalOpenError, NotesNotFrontmostError)
    assert _ERROR_TYPE_MAP.get("NotesModalOpenError") is NotesModalOpenError
