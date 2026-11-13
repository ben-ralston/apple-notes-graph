"""Custom exceptions for the apple_notes library."""

# macOS 27 shows the Accessibility pane under this name (TCC's service is unchanged).
ACCESSIBILITY_SETTINGS = (
    "System Settings > Privacy & Security > Device Control and Data Access "
    "(called Accessibility on older macOS)"
)


class AppleNotesError(Exception):
    """Base exception for apple_notes library."""

    pass


class DatabaseNotFoundError(AppleNotesError):
    """Raised when the Apple Notes database cannot be found."""

    pass


class NoteNotFoundError(AppleNotesError):
    """Raised when a requested note doesn't exist."""

    pass


class EncryptedNoteError(AppleNotesError):
    """Raised when attempting to read an encrypted note."""

    pass


class ProtobufParseError(AppleNotesError):
    """Raised when protobuf parsing fails."""

    pass


class AppleScriptError(AppleNotesError):
    """Raised when an AppleScript execution fails."""

    pass


class UIScriptingError(AppleScriptError):
    """Raised when UI scripting operations fail."""

    pass


class AccessibilityPermissionError(UIScriptingError):
    """Raised when accessibility permissions are not granted for UI scripting."""

    pass


class SessionLockedError(UIScriptingError):
    """Raised when the macOS GUI session is locked and UI scripting would
    silently no-op. Unlock the Mac and retry."""

    pass


class NotesNotFrontmostError(UIScriptingError):
    """Raised when UI scripting is aborted because Apple Notes is not the
    frontmost app. Sending keystrokes would type into whatever app *is*
    frontmost (e.g. a terminal), so we refuse. The message names the actual
    frontmost app. Bring Notes to the front and retry."""

    pass


class WrongNoteOpenError(NotesNotFrontmostError):
    """The note selected in Apple Notes is not the one being edited (issue
    #184). Raised by the per-key identity check before keystrokes are sent.
    Subclasses NotesNotFrontmostError so existing retry/verbatim handling
    applies."""

    pass


class NotesModalOpenError(NotesNotFrontmostError):
    """Raised when a sheet or dialog is open in Apple Notes (issue #199).

    Notes is the frontmost *process*, so the plain frontmost check passes,
    but keystrokes would go to the dialog instead of the note body: the
    select-all + copy came back without Notes richtext and the write failed
    with a misleading "note may be empty or inaccessible" error. A subclass
    of NotesNotFrontmostError because it is the same refusal (keystrokes
    would land somewhere other than the note), so every existing
    "keystrokes refused" branch handles it. Close the dialog and retry."""

    pass


class NotesNotRespondingError(UIScriptingError):
    """Apple Notes / System Events did not answer an osascript call in time.

    Distinct from NotesNotFrontmostError: the app was confirmed *slow to
    answer*, not confirmed *behind another app* (issue #141 — the old path
    reported a minute-long stall as a focus problem)."""

    pass


class WriteVerificationError(UIScriptingError):
    """Raised when a UI-scripting write completed without an error but the
    read-back shows it did not take effect (note not created, or content
    unchanged). Prevents reporting a false success."""

    pass


class PartialWriteError(WriteVerificationError):
    """The note changed, but part of the requested content isn't confirmed
    in it (issue #200): a missing link, missing text, a checkbox state not
    seen, or a read-back that never came. Don't retry the whole write — that
    would duplicate the part that landed; check the note. The remote queue
    reports it as ``applied_unverified``."""

    pass


class PlaceholderNotFoundError(PartialWriteError):
    """Cmd+F didn't select a tag or table placeholder (issue #122).

    Raised before the Delete that would replace the selection, so no note
    text is erased. The paste already landed, so the note holds the literal
    placeholder (and any tags or tables typed before it): don't retry the
    whole write, which would duplicate it; fix the note by hand."""

    pass


class LinksNotConfirmedError(PartialWriteError):
    """A pasted note link couldn't be confirmed after the write (#185, #212).

    Raised by add_note's link copy-back once the new note exists, so the rest
    of the write landed: a retried add_note would duplicate the note. The
    other write paths report a missing link as a plain PartialWriteError
    from their content verify."""

    pass


class SectionEditRolledBackError(WriteVerificationError):
    """A section edit failed verification and was undone; the note is back in
    its original state (issue #184)."""

    pass


class SectionEditDamagedError(WriteVerificationError):
    """A section edit failed verification and could not be undone; the note
    may be damaged (issue #184)."""

    pass


class ClipboardError(AppleNotesError):
    """Raised when clipboard operations fail."""

    pass


class FolderNotFoundError(AppleNotesError):
    """Raised when a specified folder doesn't exist in Apple Notes."""

    pass


class AmbiguousNoteTitleError(AppleNotesError):
    """More than one note carries the title a delete/move addressed (#229).

    ``matches`` holds every candidate (note_target.TitleMatch) so callers can
    show each one's folder and modified date.
    """

    def __init__(self, message: str, matches=()):
        super().__init__(message)
        self.matches = tuple(matches)


class NoteInTrashError(AppleNotesError):
    """The addressed note is already in Recently Deleted (#229, #230)."""

    pass


class AmbiguousFolderError(AppleNotesError):
    """More than one folder matches the name a move addressed (#230)."""

    def __init__(self, message: str, matches=()):
        super().__init__(message)
        self.matches = tuple(matches)


class BackupError(AppleNotesError):
    """Raised when backup operations fail."""

    pass


class InvalidGraphParameterError(AppleNotesError):
    """Raised when graph traversal parameters are invalid."""

    pass


class UIHelperNotAvailableError(AppleNotesError):
    """Raised when the UI helper daemon is not available for remote operations."""

    pass


class UIHelperTimeoutError(UIHelperNotAvailableError):
    """The daemon accepted the command but didn't answer in time; the command
    may still complete (issue #184). A subclass so existing handlers behave as before."""

    pass


class EditLockTimeoutError(UIScriptingError):
    """Raised when the cross-process edit lock can't be acquired within the
    timeout — another UI-scripting edit is taking too long or is stuck. Fails
    loudly instead of hanging the caller indefinitely (issue #96).

    A UIScriptingError (and thus AppleNotesError) so the daemon's broad
    `except AppleNotesError` returns a clean error response AND server.py's
    write tools, which catch UIScriptingError specifically, surface the
    actionable message instead of letting it propagate uncaught."""

    pass
