"""UI scripting operations for Apple Notes with RTF clipboard support."""

import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence, Tuple


from . import profiling
from .applescript import (
    AppleScriptExecutor,
    is_note_not_found_output,
    is_timeout_output,
)
from .clipboard import ClipboardManager
from .exceptions import (
    ACCESSIBILITY_SETTINGS,
    UIScriptingError,
    AccessibilityPermissionError,
    EncryptedNoteError,
    NoteNotFoundError,
    NotesModalOpenError,
    NotesNotFrontmostError,
    NotesNotRespondingError,
    PlaceholderNotFoundError,
    WrongNoteOpenError,
    AppleScriptError,
)
from .sections import SECTION_KEY_ALLOWLIST, Key
from .richtext.markdown_converter import PendingKeyboardAction
from .write_indicator import show_pending

# Raised from inside `_note_body_html`'s script when the note is locked —
# Notes returns an empty body for a password-protected note with no error of
# its own, so the script has to signal it explicitly (issue #126).
_PASSWORD_PROTECTED_SENTINEL = "NOTE_PASSWORD_PROTECTED"


# --- AppleScript failure classification (issue #133) ---------------------
#
# A genuine Accessibility/TCC denial and a `first process whose ...` filter
# that matches nothing BOTH surface as AppleScript error -1719
# (`errAEIllegalIndex`). The numeric code therefore cannot distinguish a
# permanent permissions problem from a transient focus race — only the
# message text can, so classification keys on text, not on the code.
#
# These are the literal denial strings System Events emits, read out of
# /System/Library/CoreServices/System Events.app/Contents/MacOS/System Events:
#     "%@ is not allowed assistive access."
#     "%@ is not allowed to send keystrokes."
#     "Access for assistive devices is disabled."
#     "Sending keystrokes is disabled."
#
# Matching the phrase rather than the code is also what makes this robust:
# the denial has been reported under -1719, -1728 and -25211
# (kAXErrorAPIDisabled) while the wording stayed constant. And a bare
# "not allowed" substring would false-positive on -1713 errAENoUserInteraction
# ("User interaction is not allowed."), which is reachable on the headless
# daemon path and is not a permissions problem at all.
_ACCESSIBILITY_DENIAL_MARKERS = (
    "not allowed assistive access",
    "not allowed to send keystrokes",
    "assistive devices is disabled",
    "sending keystrokes is disabled",
)

# `set frontApp to name of first process whose frontmost is true` fails with
# "Can't get process 1 whose frontmost = true. Invalid index. (-1719)" when no
# process reports frontmost — a transient race during app switching, not a
# permissions problem. Retryable.
#
# BOTH markers are required. "invalid index" alone is far too broad: any
# index-based AppleScript reference that goes out of range (a menu item, row or
# window N in some future `action_body`, or a failing `prelude`) renders the
# same errAEIllegalIndex text, and reporting that as a focus race would tell
# the user to retry a deterministic failure forever. Pairing it with the
# frontmost-lookup phrase scopes the match to the guard's own query. If Apple
# ever changes this wording the match simply stops firing and the output falls
# through to the generic UIScriptingError — a worse message, but never a wrong
# instruction, which is the safe direction to fail in.
_NO_FRONTMOST_MARKERS = ("invalid index", "frontmost = true")

# Classification keys on English message text, and a genuine denial does not
# always carry a denial phrase — under a non-English locale, or when System
# Events renders only the bare errAEIllegalIndex description, a real
# permissions failure lands in the transient-race branch. Retrying then never
# succeeds. Rather than re-adding the numeric-code check that caused issue #133
# (it produced the inverse false positive), the retry guidance carries a
# secondary hint: retry first, and if the condition persists it is not a race.
# This keeps the common case pointed at the right remedy without ever
# confidently sending the user to System Settings for a race.
_PERSISTENT_HINT = (
    "If it keeps failing the same way, this is not a transient race: check "
    f"that Accessibility access is granted in {ACCESSIBILITY_SETTINGS}."
)


def _live_note_id(note_name: str) -> str | None:
    """AppleScript id of the live note a write to ``note_name`` must address
    when copies in Recently Deleted share its title, else None (#83).

    By-title lookups can bind the trashed copy, so the scripts below switch to
    `note id "…"` then. A failure to read NoteStore refuses the write rather
    than falling back to the title: the title guards now allow a live note
    beside trashed copies, so the title alone is no longer known to be safe.
    """
    from . import database
    from .note_target import live_note_id_beside_trashed_twins

    try:
        with database.NotesDatabase() as db:
            return live_note_id_beside_trashed_twins(db, note_name)
    except UIScriptingError:
        raise
    except Exception as e:
        raise UIScriptingError(
            f"Couldn't read NoteStore to tell '{note_name}' from a copy in Recently "
            f"Deleted, so it wasn't addressed by title ({type(e).__name__}); retry."
        ) from e


def _note_spec(note_name: str, by_title: str) -> str:
    """The AppleScript reference a script uses for ``note_name``: the live
    note's id beside a trashed twin (#83), else ``by_title``."""
    note_id = _live_note_id(note_name)
    return f'note id "{note_id}"' if note_id else by_title


def _is_accessibility_denial(output: str) -> bool:
    """True only for a genuine Accessibility/TCC denial from System Events.

    Deliberately narrow: a bare "-1719" or a generic "not allowed" substring
    is NOT enough (issue #133), because both appear in transient failures that
    have nothing to do with permissions.
    """
    lowered = output.lower()
    return any(marker in lowered for marker in _ACCESSIBILITY_DENIAL_MARKERS)


def _is_no_frontmost_match(output: str) -> bool:
    """True when the frontmost-process lookup matched nothing (retryable).

    The `not _is_accessibility_denial` conjunct is load-bearing: System Events
    can emit a denial phrase and the invalid-index text in the same output, and
    a denial must never be reported as the retryable case — that would tell the
    user to retry forever on a permanent permissions problem.
    """
    lowered = output.lower()
    return all(
        marker in lowered for marker in _NO_FRONTMOST_MARKERS
    ) and not _is_accessibility_denial(output)


# Open dialog in Notes (issue #199). A sheet on a Notes window, or a Notes
# window whose subrole is a dialog (an alert), passes the frontmost-PROCESS
# check, but keystrokes then go to the dialog: the select-all + copy came back
# without Notes richtext and every write failed with a misleading "note may be
# empty or inaccessible". Detection is a pure read (no keystrokes); dismissal
# is deliberately not attempted: Escape on an unknown dialog could cancel
# something the user started and Return could confirm it.
MODAL_SENTINEL = "NOTES_MODAL_OPEN"
MODAL_TITLE_MAX = 80
_DIALOG_SUBROLES = ("AXDialog", "AXSystemDialog")
# osascript appends the (negative) error number to an `error` message:
# "... (-2700)". Only that is stripped, never a title's own "(2)".
_ERROR_NUMBER_SUFFIX = re.compile(r"\s*\(-\d+\)\s*$")


def _modal_scan_script(process_name: str) -> str:
    """AppleScript that sets `modalTitle` to the open dialog's title.

    `missing value` when no dialog is open; "" when one is open but has no
    readable title. Only the front window (`window 1`, which takes the keys)
    is inspected: a sheet left on a background note window doesn't receive
    keystrokes and must not block writes. Emitted at the `tell application "System Events"` level
    (never inside a `tell process` block) so it can sit between batched keys
    next to the frontmost re-check. Any read error is swallowed (fail open):
    the frontmost check before it already caught a permissions denial, and a
    window that can't be inspected is not proof of a dialog.
    """
    subroles = ", ".join(f'"{s}"' for s in _DIALOG_SUBROLES)
    return (
        "    set modalTitle to missing value\n"
        "    set modalTarget to missing value\n"
        "    try\n"
        f'        set modalWin to window 1 of process "{process_name}"\n'
        "        if exists sheet 1 of modalWin then\n"
        "            set modalTarget to sheet 1 of modalWin\n"
        f"        else if (subrole of modalWin) is in {{{subroles}}} then\n"
        "            set modalTarget to modalWin\n"
        "        end if\n"
        "    end try\n"
        "    if modalTarget is not missing value then\n"
        '        set modalTitle to ""\n'
        "        try\n"
        "            set modalTitle to name of modalTarget\n"
        "        end try\n"
        '        if modalTitle is missing value then set modalTitle to ""\n'
        '        if modalTitle is "" then\n'
        "            try\n"
        "                set modalTitle to value of static text 1 of modalTarget\n"
        "            end try\n"
        '            if modalTitle is missing value then set modalTitle to ""\n'
        "        end if\n"
        "    end if\n"
    )


def _clean_modal_title(raw: str, *, error_output: bool = False) -> str:
    title = _ERROR_NUMBER_SUFFIX.sub("", raw) if error_output else raw
    title = title.strip()
    if len(title) > MODAL_TITLE_MAX:
        title = title[: MODAL_TITLE_MAX - 1] + "…"
    return title


class UIScriptingExecutor:
    """Execute UI scripting operations for Apple Notes."""

    # Delays for UI operations (seconds)
    # These need to be generous to account for UI responsiveness
    NOTE_LOAD_DELAY = 1.0  # Time for note to fully load and focus
    KEYSTROKE_DELAY = 0.3  # Time between keyboard operations
    SECTION_KEY_DELAY = 0.12  # Between section-edit keys (issue #184)
    PASTE_DELAY = 0.5  # Time after paste for content to render

    # Delays for keyboard automation (Find+Replace workflow)
    FIND_BAR_DELAY = 0.5  # Time for Find bar to appear
    HASHTAG_CONFIRM_DELAY = 0.3  # Time for hashtag to confirm after Space

    # macOS key codes for UI scripting
    RETURN_KEY_CODE = 36
    TAB_KEY_CODE = 48
    DELETE_KEY_CODE = 51
    ESCAPE_KEY_CODE = 53
    T_KEY_CODE = 17
    RIGHT_ARROW_KEY_CODE = 124
    DOWN_ARROW_KEY_CODE = 125
    LEFT_ARROW_KEY_CODE = 123

    # Delay for table insertion UI
    TABLE_INSERT_DELAY = 0.5
    # Re-reads of the selection after the first, before calling a Find a
    # miss (issue #122): a slow Find highlight gets ~0.6s more.
    SELECTION_RETRIES = 2

    # Focus guard (issue #90): the System Events process name Apple Notes runs
    # as, and how long to wait for it to become frontmost after `activate`
    # before refusing to send keystrokes.
    NOTES_PROCESS_NAME = "Notes"
    FRONTMOST_POLL_TIMEOUT = 5.0  # Max wall-clock wait for Notes to front (#141)
    FRONTMOST_POLL_INTERVAL = 0.1  # Delay between frontmost polls

    # Budget for a SINGLE System Events event (frontmost probe or one
    # keystroke). These normally answer in <1s; a slow answer is the issue-#141
    # stall, and 10s bounds it instead of the executor's 30s default. Batched
    # scripts scale this by their event count (see `_guarded_system_events`)
    # so a slow-but-answering System Events can never kill a script between a
    # select-all and its paired key.
    SYSTEM_EVENTS_TIMEOUT = 10.0

    # Budget for scripts whose duration scales with caller-supplied text
    # (`_type_text`, `_paste_text`) rather than with a fixed key count — the
    # executor's pre-#141 default, kept for exactly that reason.
    TEXT_ENTRY_TIMEOUT = 30.0

    # Debug output directory
    DEBUG_DIR = Path.home() / "Library/Application Support/notes-graph-debug"

    def __init__(self, debug: bool = False):
        self._debug = debug
        self._applescript = AppleScriptExecutor()
        self._clipboard = ClipboardManager()
        # Set while keys go into a dialog this write opened itself (the
        # #tag suggestion popup): the re-check
        # then skips the dialog scan.
        self._dialog_expected = False

    def _save_debug_rtf(self, rtf_data: bytes, label: str) -> Path:
        """
        Save RTF data to debug directory for troubleshooting.

        Args:
            rtf_data: The RTF content to save
            label: A label like "before" or "after"

        Returns:
            Path to the saved file
        """
        self.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"rtf_{label}_{timestamp}.rtf"
        filepath = self.DEBUG_DIR / filename
        filepath.write_bytes(rtf_data)
        return filepath

    def check_accessibility_permissions(self) -> bool:
        """
        Check if accessibility permissions are granted for UI scripting.

        Returns:
            True if permissions are granted, False otherwise
        """
        script = """
tell application "System Events"
    try
        -- Try a benign operation that requires accessibility
        get name of first process
        return true
    on error
        return false
    end try
end tell
"""
        success, output = self._applescript.execute(script)
        return success and output.lower() == "true"

    def _activate_notes(self) -> None:
        """
        Activate the Notes application.

        Raises:
            AppleScriptError: If activation fails
        """
        script = """
tell application "Notes"
    activate
end tell
"""
        # Warn before the app switch: the write-guard overlay + lead countdown
        # run while the user's own app is still in front.
        show_pending()
        # Shares the `open` span with open_note — Profile.record accumulates,
        # so a write that both activates and shows a note reports one figure
        # for "getting Notes in front of us" (issue #141).
        with profiling.span("open"):
            success, error = self._applescript.execute(script)
            if not success:
                raise AppleScriptError(f"Failed to activate Notes: {error}")
            # Focus guard: `activate` is async — confirm Notes actually fronted
            # before any caller sends keystrokes (issue #90).
            self._ensure_notes_frontmost()

    def _frontmost_process_name(self) -> str:
        """
        Return the name of the current frontmost application process.

        Used by the focus guard to confirm Apple Notes is actually frontmost
        before sending keystrokes, since CGEvents always go to the frontmost
        app regardless of `tell process "Notes"`.

        Returns:
            The frontmost process name, or "" when *no* process currently
            reports as frontmost. That is a transient race during app
            switching (the `first process whose ...` filter matches nothing
            and fails with errAEIllegalIndex, -1719), not a permissions
            problem — returning "" lets the settle-poll ride it out instead
            of aborting with a bogus accessibility error (issue #133).

        Raises:
            AccessibilityPermissionError: If accessibility not granted
            NotesNotRespondingError: If System Events didn't answer in time
            UIScriptingError: If the query fails
        """
        script = """
tell application "System Events"
    return name of first process whose frontmost is true
end tell
"""
        success, output = self._applescript.execute(
            script, timeout=self.SYSTEM_EVENTS_TIMEOUT
        )
        if not success:
            if _is_accessibility_denial(output):
                raise AccessibilityPermissionError(
                    "Accessibility permissions required. "
                    f"Grant access in {ACCESSIBILITY_SETTINGS}"
                )
            if _is_no_frontmost_match(output):
                return ""
            if is_timeout_output(output):
                # "No keystrokes were sent" holds for every current call site
                # (this probe only ever runs pre-keystroke) but is a property
                # of the caller, not of this method — a future mid-write
                # caller of this probe would need to reword it.
                raise NotesNotRespondingError(
                    f"Apple Notes didn't respond in "
                    f"{self.SYSTEM_EVENTS_TIMEOUT:g}s while trying to read "
                    f"the frontmost app. Notes or System Events is not "
                    f"answering — no keystrokes were sent. Retry once it is "
                    f"responsive."
                )
            raise UIScriptingError(f"Failed to read frontmost app: {output}")
        return output.strip()

    def _ensure_notes_frontmost(self, timeout: float | None = None) -> None:
        """
        Poll until Apple Notes is the frontmost app, or abort (issue #90).

        `activate` is asynchronous: it requests focus but returns before Notes
        is necessarily frontmost. Right after unlocking the Mac the previous
        app (often a terminal) is still frontmost, so keystrokes sent
        immediately land there. This is the primary safety guard — every
        UI-scripting operation calls it after activating, and it raises rather
        than letting keystrokes reach another app.

        Args:
            timeout: Max seconds of wall clock to wait for Notes to come
                frontmost (defaults to FRONTMOST_POLL_TIMEOUT). This is a
                deadline, not an attempt count: however slow the individual
                probes are, the guard gives up once the budget is spent — but
                it always probes at least once. Note the deadline is checked
                only between probes, not inside one: a probe already in
                flight when the deadline passes still runs to its own budget
                (SYSTEM_EVENTS_TIMEOUT, 10s), so worst-case wall clock before
                the distinct not-responding/not-frontmost error surfaces is
                ~timeout + SYSTEM_EVENTS_TIMEOUT (~15s at the defaults), not
                the bare 5s deadline.

        Raises:
            NotesNotFrontmostError: If Notes is not frontmost within the timeout
            AccessibilityPermissionError: If accessibility not granted
            NotesNotRespondingError: If a frontmost probe times out
            UIScriptingError: If the frontmost query fails
        """
        if timeout is None:
            timeout = self.FRONTMOST_POLL_TIMEOUT
        interval = self.FRONTMOST_POLL_INTERVAL

        # Wall-clock deadline, not attempt count (issue #141): each probe is
        # its own osascript subprocess whose latency varies 6x within a single
        # healthy run — 20 fixed attempts at ~3.3s/probe meant the advertised
        # "2 second" guard actually blocked ~67s before failing misleadingly.
        start = time.monotonic()
        deadline = start + timeout

        # Remember the last app that actually reported as frontmost. A single
        # "" on the final poll must not erase the diagnostic: if some app held
        # focus for the whole timeout, naming it is the one piece of
        # information that identifies what is stealing focus, and the failure
        # is persistent rather than the transient race "" implies.
        last_named = ""
        while True:
            front = self._frontmost_process_name()
            if front == self.NOTES_PROCESS_NAME:
                return
            if front:
                last_named = front
            # Checked after the probe, so a probe already in flight when the
            # deadline passes still counts — and a zero budget still looks once.
            now = time.monotonic()
            if now >= deadline:
                break
            time.sleep(min(interval, deadline - now))

        elapsed = time.monotonic() - start
        if not last_named:
            raise NotesNotFrontmostError(
                f"Aborted UI scripting: no application reported as frontmost "
                f"after {elapsed:.1f}s, so Apple Notes could not be confirmed "
                f"in front. No keystrokes were sent by this step. This is "
                f"usually a transient focus race — retry with hands off the "
                f"keyboard and without switching apps. " + _PERSISTENT_HINT
            )
        raise NotesNotFrontmostError(
            f"Aborted UI scripting: Apple Notes is not frontmost after "
            f"{elapsed:.1f}s (frontmost app: {last_named!r}). Refusing to "
            f"send keystrokes to another app — bring Apple Notes to the "
            f"front and retry."
        )

    def _note_exists(self, note_name: str) -> bool:
        """
        Check if a note exists by name.

        Args:
            note_name: The note title to check

        Returns:
            True if note exists, False otherwise
        """
        escaped_name = AppleScriptExecutor._escape_applescript_string(note_name)
        spec = _note_spec(
            note_name, f'first note whose name = "{escaped_name}"'
        )
        script = f"""
tell application "Notes"
    try
        set theNote to {spec}
        return true
    on error
        return false
    end try
end tell
"""
        success, output = self._applescript.execute(script)
        return success and output.lower() == "true"

    def _note_body_html(self, note_name: str) -> str:
        """
        Return the HTML body of a note (for verify-after-write).

        Uses the native Notes `body` property (read-only HTML), so it reflects
        Notes.app state immediately and works the same locally and in the
        daemon's GUI session. HTML (not `plaintext`) is the change signal so a
        formatting-only edit — e.g. bolding a word — is detected as a real
        change rather than mistaken for a silent no-op.

        Args:
            note_name: The exact note title

        Returns:
            The note's body as HTML

        Notes returns an EMPTY body for a password-protected note with no
        error at all, so the script asks `password protected` itself; a locked
        note surfaces as EncryptedNoteError rather than poisoning the
        verify-after-write baseline with "" (issue #126).

        Raises:
            NoteNotFoundError: If the note doesn't exist
            EncryptedNoteError: If the note is password-protected (locked)
            AppleScriptError: If the read fails for another reason
        """
        escaped_name = AppleScriptExecutor._escape_applescript_string(note_name)
        spec = _note_spec(
            note_name, f'first note whose name = "{escaped_name}"'
        )
        script = f'''
tell application "Notes"
    set theNote to {spec}
    if password protected of theNote then
        error "{_PASSWORD_PROTECTED_SENTINEL}"
    end if
    return body of theNote
end tell
'''
        success, output = self._applescript.execute(script)
        if not success:
            if _PASSWORD_PROTECTED_SENTINEL in output:
                raise EncryptedNoteError(
                    f"Note '{note_name}' is password-protected (locked) and "
                    "cannot be read or edited"
                )
            if is_note_not_found_output(output):
                raise NoteNotFoundError(f"Note '{note_name}' not found")
            raise AppleScriptError(f"Failed to read note body: {output}")
        return output

    def delete_note(self, note_name: str) -> None:
        """
        Delete a note by name (moves to Recently Deleted).

        Uses pure AppleScript — no clipboard or UI scripting needed.

        Args:
            note_name: The exact title of the note to delete

        Raises:
            NoteNotFoundError: If note doesn't exist
            AppleScriptError: If deletion fails
        """
        escaped_name = AppleScriptExecutor._escape_applescript_string(note_name)
        script = f'''
tell application "Notes"
    set matchingNotes to every note whose name = "{escaped_name}"
    set foundNote to missing value
    repeat with n in matchingNotes
        try
            set c to container of n
            set folderName to name of c
        on error
            -- Notes in Recently Deleted have no valid container
            set folderName to "Recently Deleted"
        end try
        if folderName is not "Recently Deleted" then
            set foundNote to n
            exit repeat
        end if
    end repeat
    if foundNote is missing value then
        error "Note not found"
    end if
    delete foundNote
end tell
'''
        success, output = self._applescript.execute(script)
        if not success:
            if is_note_not_found_output(output):
                raise NoteNotFoundError(f"Note '{note_name}' not found")
            raise AppleScriptError(f"Failed to delete note: {output}")

    def open_note(self, note_name: str) -> None:
        """
        Open a note by name in Apple Notes and bring it to focus.

        Args:
            note_name: The exact title of the note to open

        Raises:
            NoteNotFoundError: If note doesn't exist
            AppleScriptError: If opening fails
        """
        escaped_name = AppleScriptExecutor._escape_applescript_string(note_name)
        spec = _note_spec(note_name, f'note "{escaped_name}"')

        script = f"""
tell application "Notes"
    show {spec}
    activate
end tell
"""
        # Warn before the app switch (see _activate_notes).
        show_pending()
        with profiling.span("open"):
            success, output = self._applescript.execute(script)

            if not success:
                if is_note_not_found_output(output):
                    raise NoteNotFoundError(f"Note '{note_name}' not found")
                raise AppleScriptError(f"Failed to open note: {output}")
            # Focus guard: `activate` is async — confirm Notes actually fronted
            # before any caller sends keystrokes (issue #90).
            self._ensure_notes_frontmost()

    def _guarded_system_events(
        self, action_body: str, prelude: str = "", timeout: float | None = None
    ) -> Tuple[bool, str]:
        """
        Run a System Events action, but only while Apple Notes is frontmost.

        Layer B of the focus guard (issue #90): the frontmost check is embedded
        in the same osascript as the action, so the keystroke fires atomically
        only when Notes is frontmost — no extra subprocess round-trip, and
        mid-sequence focus theft is caught. If another app is frontmost the
        script raises an error carrying the NOTES_NOT_FRONTMOST sentinel and
        the offending app name instead of typing into it.

        Args:
            action_body: AppleScript run inside the `tell application "System
                Events"` block once Notes is confirmed frontmost (e.g.
                `tell process "Notes" to keystroke "v" using command down`).
            prelude: Optional AppleScript run *before* the System Events block
                (e.g. `set the clipboard to "..."`), kept in the same osascript
                so clipboard-set + paste stay race-free. It runs inside that
                same osascript process, so it shares this call's budget with
                the action body — don't route a heavyweight/data-assembly
                prelude through it without widening `timeout`.
            timeout: Wall-clock budget for the whole osascript. Defaults to
                `SYSTEM_EVENTS_TIMEOUT`, which is the budget for ONE System
                Events event. Batched scripts (several keystrokes + their
                frontmost re-checks and inline delays) must pass a budget
                scaled to their event count: the pre-batch code gave each key
                its own 30s, so a flat per-event cap would newly be able to
                kill a script *between* a select-all and its paired key —
                exactly the state the guard exists to prevent.

        Returns:
            (success, output) from the AppleScript executor.
        """
        # Write-guard overlay: normally already shown by open_note /
        # _activate_notes before Notes was brought to the front; this
        # idempotent call covers any keystroke path that skips them.
        show_pending()
        # The header is the same frontmost assertion the batched scripts
        # re-emit between keys — built from `_frontmost_recheck()` so the
        # NOTES_NOT_FRONTMOST sentinel lives in exactly one place.
        script = f"""{prelude}
tell application "System Events"
{self._frontmost_recheck()}    {action_body}
end tell
"""
        if timeout is None:
            timeout = self.SYSTEM_EVENTS_TIMEOUT
        return self._applescript.execute(script, timeout=timeout)

    def _frontmost_recheck(self) -> str:
        """AppleScript that re-asserts Notes is frontmost mid-script.

        Batched keystroke scripts (issue #141) send several keys in one
        osascript with inline delays between them, but `_guarded_system_events`
        only checks before the first key. Without a re-check per key the #90
        focus guard would lose its per-keystroke granularity: focus stolen
        during a delay would send the remaining keys — including a Cmd+V
        carrying the note's content — to whatever app took the front.

        Emitted at the same indentation as the action body, and raising the
        same NOTES_NOT_FRONTMOST sentinel, so `_raise_keystroke_failure`
        classifies a mid-script abort exactly like a pre-script one. Costs no
        extra subprocess — it is another statement in the script already
        being run.

        After the frontmost assertion it also refuses while a sheet or dialog
        is open in Notes (issue #199, NOTES_MODAL_OPEN sentinel): Notes is
        then the frontmost process, but the keys would go to the dialog. That
        scan is left out inside `_expecting_dialog()`, for keys meant for a
        dialog the write opened itself.
        """
        frontmost = (
            "    set frontApp to name of first process whose frontmost is true\n"
            f'    if frontApp is not "{self.NOTES_PROCESS_NAME}" then\n'
            '        error "NOTES_NOT_FRONTMOST: " & frontApp\n'
            "    end if\n"
        )
        if getattr(self, "_dialog_expected", False):
            return frontmost
        return (
            frontmost
            + _modal_scan_script(self.NOTES_PROCESS_NAME)
            + "    if modalTitle is not missing value then\n"
            f'        error "{MODAL_SENTINEL}: " & modalTitle\n'
            "    end if\n"
        )

    @contextmanager
    def _expecting_dialog(self):
        """Allow keys into a dialog this write opened itself (issue #199).

        Only the dialog scan is dropped from the re-check; the frontmost
        assertion stays. The scope must cover only keys sent while the
        write's own dialog is up, and it always ends, even on error.
        """
        previous = self._dialog_expected
        self._dialog_expected = True
        try:
            yield
        finally:
            self._dialog_expected = previous

    def _raise_keystroke_failure(
        self, output: str, what: str, timeout: float | None = None
    ) -> None:
        """
        Map a failed guarded-keystroke result to the right exception.

        Distinguishes the focus-guard abort (NotesNotFrontmostError) from
        missing accessibility permissions from a generic UI scripting failure.

        `timeout` is the budget the failed script actually ran under, so the
        timeout message names the real number: batched scripts run on a
        multiple of `SYSTEM_EVENTS_TIMEOUT` (see `_guarded_system_events`) and
        reporting the per-event cap there would contradict the executor's own
        "timed out after Ns" output.

        Classification keys on the AppleScript *message text*, never on a bare
        error code: -1719 is emitted both by a genuine assistive-access denial
        and by the guard's own `first process whose frontmost is true` lookup
        when nothing matches. Treating the latter as a permissions problem
        sent users to System Settings for a race that a plain retry fixes
        (issue #133).
        """
        if "NOTE_CHANGED" in output:
            _, _, tail = output.partition("NOTE_CHANGED:")
            selected = tail.strip().split(" (")[0].strip() or "unknown"
            raise WrongNoteOpenError(
                f"Aborted UI scripting ({what}): a different note is open in "
                f"Apple Notes (selected: {selected}), so keystrokes were "
                f"refused from that point. Keys earlier in this step may "
                f"already have been sent; check the note's contents before "
                f"retrying."
            )
        if MODAL_SENTINEL in output:
            # Checked first: the dialog check runs only after the frontmost
            # assertion passed, so its sentinel is the more specific answer.
            # Like the no-frontmost race below, it may be a mid-script
            # re-check after earlier keys of this step already fired, so the
            # message must not promise the note is untouched.
            _, _, tail = output.partition(f"{MODAL_SENTINEL}:")
            title = _clean_modal_title(tail, error_output=True)
            named = f" ({title!r})" if title else ""
            raise NotesModalOpenError(
                f"Aborted UI scripting ({what}): a dialog is open in Notes"
                f"{named}; close it and retry. Keystrokes would have gone to "
                f"the dialog, not the note. This step stopped at a dialog "
                f"check — keys earlier in this step, and earlier steps in the "
                f"operation, may already have modified the note; check its "
                f"contents before retrying."
            )
        if "NOTES_NOT_FRONTMOST" in output:
            # `partition` rather than `split(...)[1]`: the branch is entered on
            # the bare sentinel name, so an output carrying it without the
            # trailing colon (a truncated error, or a future edit to the
            # sentinel emitted in `_guarded_system_events`) indexed past the
            # end of the split and raised IndexError out of the safety branch.
            # Degrading to the existing "another app" fallback keeps the
            # refused-keystrokes classification, which is the part that
            # matters; the app name is a diagnostic nicety.
            _, _, tail = output.partition("NOTES_NOT_FRONTMOST:")
            app = tail.strip().split(" (")[0].strip() or "another app"
            raise NotesNotFrontmostError(
                f"Aborted UI scripting: Apple Notes is not frontmost "
                f"(frontmost app: {app!r}). Refusing to send keystrokes to "
                f"another app — bring Apple Notes to the front and retry."
            )
        if _is_accessibility_denial(output):
            raise AccessibilityPermissionError(
                "Accessibility permissions required. "
                f"Grant access in {ACCESSIBILITY_SETTINGS}"
            )
        if _is_no_frontmost_match(output):
            # A frontmost assertion matched nothing: no app reported frontmost
            # at that instant, so the script aborted there.
            #
            # The message must not promise anything about what was already
            # typed. Two reasons: `_raise_keystroke_failure` is reached from
            # steps deep inside multi-step sequences (`_replace_with_hashtag`
            # sends DELETE, then types, then DELETE again), AND — since
            # keystrokes batch (issue #141) — the failing assertion may be a
            # mid-script `_frontmost_recheck` that ran *after* earlier keys in
            # this very script already fired. A user who retries on a
            # "nothing was typed" promise re-runs the chain against mutated
            # text and ends up with duplicated or garbled notes. Callers own
            # recovery; this message must not imply the note is untouched.
            raise NotesNotFrontmostError(
                f"Aborted UI scripting ({what}): no application reported as "
                f"frontmost, so Apple Notes could not be confirmed in front. "
                f"This step was aborted at a frontmost check — keys earlier in "
                f"this same step, and earlier steps in the operation, may "
                f"already have modified the note; check its contents before "
                f"retrying. This is usually a transient focus race. "
                f"{_PERSISTENT_HINT}"
            )
        if is_timeout_output(output):
            if timeout is None:
                timeout = self.SYSTEM_EVENTS_TIMEOUT
            raise NotesNotRespondingError(
                f"Apple Notes didn't respond in "
                f"{timeout:g}s while trying to {what}. "
                f"Notes or System Events is not answering. Earlier steps in "
                f"the operation may already have modified the note — check "
                f"its contents before retrying."
            )
        raise UIScriptingError(f"Failed to {what}: {output}")

    def _note_identity_check(self, pk: int) -> str:
        """AppleScript asserting the selected note is Apple Notes pk `pk`.

        Emitted at the `tell application "System Events"` block level (never
        inside a `tell process` block), like `_frontmost_recheck`. Raises the
        NOTE_CHANGED sentinel that `_raise_keystroke_failure` maps to
        WrongNoteOpenError (issue #184).
        """
        return (
            "    try\n"
            '        tell application "Notes" to set selId to id of item 1 of '
            "(get selection)\n"
            "    on error\n"
            '        error "NOTE_CHANGED: no selection"\n'
            "    end try\n"
            f'    if selId does not end with "/ICNote/p{int(pk)}" then\n'
            '        error "NOTE_CHANGED: " & selId\n'
            "    end if\n"
        )

    def send_section_keys(
        self, keys: Sequence[Key], expect_pk: int, *, what: str
    ) -> None:
        """Send allowlisted edit keys in one script, guarded before every key.

        The only keystroke path section editing uses (issues #184, #240). Refuses
        any (key_code, modifiers) outside SECTION_KEY_ALLOWLIST before running
        anything. Before each key the script checks the selected note is
        `expect_pk` and (after the first key) re-asserts Notes is frontmost.

        On any raised failure, earlier keys may already have been sent; callers
        must treat the note as possibly changed.
        """
        keys = tuple(keys)
        for key in keys:
            if key not in SECTION_KEY_ALLOWLIST:
                raise ValueError(f"Key {key!r} is not allowed in section editing")
        proc = self.NOTES_PROCESS_NAME
        budget = max(self.SYSTEM_EVENTS_TIMEOUT, 0.75 * len(keys))
        with profiling.span("navigate"):
            parts = []
            for i, (code, mods) in enumerate(keys):
                if i:
                    parts.append(self._frontmost_recheck())
                parts.append(self._note_identity_check(expect_pk))
                using = f" using {mods}" if mods else ""
                parts.append(f'    tell process "{proc}" to key code {code}{using}\n')
                if i < len(keys) - 1:
                    parts.append(f"    delay {self.SECTION_KEY_DELAY}\n")
            # The header (first frontmost check) is emitted by
            # _guarded_system_events; strip the leading indent it adds.
            body = "".join(parts).lstrip()
            success, output = self._guarded_system_events(
                body.rstrip("\n"), timeout=budget
            )
            if not success:
                self._raise_keystroke_failure(output, what, timeout=budget)

    def _send_keystroke(self, key: str, modifier: str = "command down") -> None:
        """
        Send a keystroke to Apple Notes using System Events.

        Args:
            key: The key to press (e.g., "a", "c", "v")
            modifier: The modifier key (default: "command down")

        Raises:
            NotesNotFrontmostError: If Apple Notes is not frontmost
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If keystroke fails
        """
        success, output = self._guarded_system_events(
            f'tell process "{self.NOTES_PROCESS_NAME}" '
            f'to keystroke "{key}" using {modifier}'
        )
        if not success:
            self._raise_keystroke_failure(output, "send keystroke")

    def _focus_note_content(self) -> None:
        """
        Ensure focus is in the note content area, not the notes list sidebar.

        Uses a deterministic approach:
        1. Option+Command+F opens search field (known focus point)
        2. Tab moves to next field
        3. Tab moves into note content area

        This works regardless of where focus started, avoiding accidental
        content modification (e.g., Tab indenting text if already in note body).

        All three keys ride a single guarded osascript (issue #141) — the
        inter-key delays run inside the AppleScript instead of costing two
        extra subprocess round-trips. Each Tab is preceded by its own
        frontmost re-check, so batching keeps the per-keystroke guard
        granularity of the pre-batch version. The script's timeout budget is
        scaled to match (three keys + three checks), so a slow-but-answering
        System Events can't kill it half-focused.

        Raises:
            NotesNotFrontmostError: If Notes loses the front mid-sequence
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If focus fails
        """
        with profiling.span("focus"):
            recheck = self._frontmost_recheck()
            budget = 3 * self.SYSTEM_EVENTS_TIMEOUT
            success, output = self._guarded_system_events(
                f'tell process "{self.NOTES_PROCESS_NAME}" to keystroke "f" '
                f"using {{option down, command down}}\n"
                f"    delay {self.KEYSTROKE_DELAY}\n"
                f"{recheck}"
                f'    tell process "{self.NOTES_PROCESS_NAME}" to keystroke tab\n'
                f"    delay {self.KEYSTROKE_DELAY}\n"
                f"{recheck}"
                f'    tell process "{self.NOTES_PROCESS_NAME}" to keystroke tab',
                timeout=budget,
            )
            if not success:
                self._raise_keystroke_failure(
                    output, "focus note content", timeout=budget
                )

            time.sleep(self.KEYSTROKE_DELAY)

    def select_body_content(self) -> None:
        """
        Select note body content, excluding the title line.

        In Apple Notes, the first line is the title and is handled specially.
        This method selects from the second line to the end of the note.

        Raises:
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If selection fails
        """
        # First ensure focus is in the note content area
        self._focus_note_content()

        # Move cursor to start of note (Cmd+Up)
        self._send_key_code(126, "command down")  # 126 = Up arrow
        time.sleep(self.KEYSTROKE_DELAY)

        # Move down one line to skip the title (Down arrow)
        self._send_key_code(125)  # 125 = Down arrow
        time.sleep(self.KEYSTROKE_DELAY)

        # Select from cursor to end of note (Cmd+Shift+Down)
        self._send_key_code(125, "{command down, shift down}")  # 125 = Down arrow
        time.sleep(self.KEYSTROKE_DELAY)

    def paste_from_clipboard(self) -> None:
        """
        Paste clipboard content (Cmd+V).

        Shares the `paste` span with `select_all_and_paste` (issue #141). This
        is the paste the append path and the pending-action/table legs use, so
        without it a slow append left its keystroke time unattributed;
        `Profile.record` accumulates repeated pastes into one figure.

        Raises:
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If paste fails
        """
        with profiling.span("paste"):
            self._send_keystroke("v")
            time.sleep(self.PASTE_DELAY)

    def _select_all_then(
        self, key: str, *, span: str, what: str, settle: float
    ) -> None:
        """Cmd+A then Cmd+`key` in one guarded script (issue #141).

        The shared body behind `select_all_and_copy`/`select_all_and_paste`:
        one osascript round-trip, the inter-key delay inline in AppleScript, a
        frontmost re-check before the second key, and a timeout budget scaled
        to the two events + the re-check so the script can never be killed
        between the select-all and its paired key.

        Args:
            key: The second key to send with Command ("c" or "v").
            span: Profiling span to attribute the round-trip to.
            what: Action name for error messages.
            settle: Post-script sleep — how long the UI needs to catch up.
        """
        budget = 2 * self.SYSTEM_EVENTS_TIMEOUT
        with profiling.span(span):
            success, output = self._guarded_system_events(
                f'tell process "{self.NOTES_PROCESS_NAME}" to keystroke "a" '
                f"using command down\n"
                f"    delay {self.KEYSTROKE_DELAY}\n"
                f"{self._frontmost_recheck()}"
                f'    tell process "{self.NOTES_PROCESS_NAME}" to keystroke "{key}" '
                f"using command down",
                timeout=budget,
            )
            if not success:
                self._raise_keystroke_failure(output, what, timeout=budget)

            time.sleep(settle)

    def select_all_and_copy(self) -> None:
        """Select all content and copy it in one guarded script (issue #141).

        Cmd+A and Cmd+C ride one osascript round-trip; the inter-key delay
        runs inside AppleScript, with a frontmost re-check before the copy.
        Caller is responsible for focus being in the note content area.

        Raises:
            NotesNotFrontmostError: If Notes loses the front mid-sequence
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If the select/copy fails
        """
        self._select_all_then(
            "c",
            span="copy",
            what="select all and copy",
            settle=self.KEYSTROKE_DELAY,
        )

    def select_all_and_paste(self) -> None:
        """Select all content and paste over it in one guarded script (#141).

        Cmd+A and Cmd+V ride one osascript round-trip; the inter-key delay
        runs inside AppleScript. Caller is responsible for focus being in the
        note content area and for the clipboard already holding the payload.

        The frontmost re-check before the paste is the highest-stakes one in
        the codebase: without it, focus stolen during the delay would paste
        the note's entire content into another app.

        Raises:
            NotesNotFrontmostError: If Notes loses the front mid-sequence
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If the select/paste fails
        """
        self._select_all_then(
            "v",
            span="paste",
            what="select all and paste",
            settle=self.PASTE_DELAY,
        )

    def read_note_as_rtf(self, note_name: str) -> bytes:
        """
        Read a note's content as RTF via clipboard.

        Opens the note, selects all content, and copies to clipboard as RTF.

        Args:
            note_name: The exact title of the note to read

        Returns:
            RTF data as bytes

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If clipboard operations fail
        """
        # Open the note
        self.open_note(note_name)
        time.sleep(self.NOTE_LOAD_DELAY)

        # Focus the note body, then select all and copy — two batched scripts
        # instead of separate per-key round-trips for the select-all and the
        # copy (issue #141), mirroring read_note_as_notes_richtext.
        self._focus_note_content()
        self.select_all_and_copy()

        # Read RTF from clipboard
        rtf_data = self._clipboard.read_rtf()
        if rtf_data is None:
            raise UIScriptingError(
                f"Failed to read note content as RTF. "
                f"The note '{note_name}' may be empty or inaccessible."
            )

        return rtf_data

    def read_note_as_notes_richtext(self, note_name: str) -> bytes:
        """
        Read a note's content as Apple Notes richtext format via clipboard.

        This format (com.apple.notes.richtext) preserves ALL Notes formatting
        including checklists, block quotes, and other Notes-specific elements
        that are lost when using standard RTF.

        Opens the note, selects all content, and copies to clipboard.

        Args:
            note_name: The exact title of the note to read

        Returns:
            Notes richtext data as bytes

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If clipboard operations fail
        """
        # Open the note
        self.open_note(note_name)
        time.sleep(self.NOTE_LOAD_DELAY)

        # Focus the note body, then select all and copy — two batched scripts
        # instead of five per-key round-trips (issue #141).
        self._focus_note_content()
        self.select_all_and_copy()

        # Read Notes richtext from clipboard (span recorded inside
        # ClipboardManager, which owns the fresh-thread read choke point)
        data = self._clipboard.read_notes_richtext()
        if data is None:
            raise UIScriptingError(
                f"Failed to read note content as Notes richtext. "
                f"The note '{note_name}' may be empty or inaccessible."
            )

        return data

    def write_note_from_rtf(self, note_name: str, rtf_data: bytes) -> None:
        """
        Write RTF content to a note via clipboard, preserving the title.

        Opens the note, writes RTF to clipboard, selects body (not title), and pastes.

        Args:
            note_name: The exact title of the note to update
            rtf_data: RTF content as bytes

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If clipboard operations fail
        """
        # Verify note exists first
        if not self._note_exists(note_name):
            raise NoteNotFoundError(f"Note '{note_name}' not found")

        # Open the note
        self.open_note(note_name)
        time.sleep(self.NOTE_LOAD_DELAY)

        # Write RTF to clipboard
        self._clipboard.write_rtf(rtf_data)

        # Select body content (excluding title) and paste
        self.select_body_content()
        self.paste_from_clipboard()

    def write_note_from_notes_richtext(self, note_name: str, data: bytes) -> None:
        """
        Write Apple Notes richtext content to a note via clipboard, preserving the title.

        This format (com.apple.notes.richtext) preserves ALL Notes formatting
        including checklists, block quotes, and other Notes-specific elements.

        Args:
            note_name: The exact title of the note to update
            data: Notes richtext content as bytes

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If clipboard operations fail
        """
        # Verify note exists first
        if not self._note_exists(note_name):
            raise NoteNotFoundError(f"Note '{note_name}' not found")

        # Open the note
        self.open_note(note_name)
        time.sleep(self.NOTE_LOAD_DELAY)

        # Write Notes richtext to clipboard (no RTF fallback needed for body replacement)
        self._clipboard.write_notes_richtext(data)
        time.sleep(self.KEYSTROKE_DELAY)

        # Select body content (excluding title) and paste
        self.select_body_content()
        self.paste_from_clipboard()

    def replace_note_richtext(
        self, note_name: str, transform: Callable[[bytes], bytes]
    ) -> None:
        """
        Read a note's richtext, transform it, and write it back in one focus cycle.

        Avoids the overhead of opening/focusing the note twice by combining
        the read and write into a single UI scripting session.

        Args:
            note_name: The exact title of the note to update
            transform: Function that takes template richtext bytes and returns
                      new richtext bytes to write

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If clipboard operations fail
        """
        # Open the note and focus content area once
        self.open_note(note_name)
        time.sleep(self.NOTE_LOAD_DELAY)
        self._focus_note_content()

        # Read: select all and copy to get template (one batched script, #141)
        self.select_all_and_copy()

        template_data = self._clipboard.read_notes_richtext()
        if template_data is None:
            raise UIScriptingError(
                f"Failed to read note content as Notes richtext. "
                f"The note '{note_name}' may be empty or inaccessible."
            )

        # Transform the data (e.g., markdown conversion)
        result_data = transform(template_data)

        # Write: put result on clipboard, select body (skip title), paste
        # Focus is already in the content area from above
        self._clipboard.write_notes_richtext(result_data)
        time.sleep(self.KEYSTROKE_DELAY)

        self._send_key_code(126, "command down")  # Cmd+Up: start of note
        time.sleep(self.KEYSTROKE_DELAY)
        self._send_key_code(125)  # Down arrow: skip title
        time.sleep(self.KEYSTROKE_DELAY)
        self._send_key_code(
            125, "{command down, shift down}"
        )  # Cmd+Shift+Down: select to end
        time.sleep(self.KEYSTROKE_DELAY)

        self._send_keystroke("v")  # Paste
        time.sleep(self.PASTE_DELAY)

    def transform_full_note_richtext(
        self, note_name: str, transform: Callable[[bytes], bytes]
    ) -> None:
        """
        Read a note's full richtext, transform it, and write it back.

        Unlike replace_note_richtext (which pastes into body only),
        this replaces the entire note including the title. Use when the
        transform operates on the full richtext structure (e.g., toggling
        checkboxes) rather than replacing body text from markdown.

        Args:
            note_name: The exact title of the note to update
            transform: Function that takes full richtext bytes and returns
                      new richtext bytes to write

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If clipboard operations fail
        """
        # Open the note and focus content area once
        self.open_note(note_name)
        time.sleep(self.NOTE_LOAD_DELAY)
        self._focus_note_content()

        # Read: select all and copy to get full richtext
        self._send_keystroke("a")
        time.sleep(self.KEYSTROKE_DELAY)
        self._send_keystroke("c")
        time.sleep(self.KEYSTROKE_DELAY)

        template_data = self._clipboard.read_notes_richtext()
        if template_data is None:
            raise UIScriptingError(
                f"Failed to read note content as Notes richtext. "
                f"The note '{note_name}' may be empty or inaccessible."
            )

        # Transform the data
        result_data = transform(template_data)

        # Write: select all and paste (replaces entire note including title)
        self._clipboard.write_notes_richtext(result_data)
        time.sleep(self.KEYSTROKE_DELAY)

        # One batched script instead of two per-key round-trips (issue #141).
        self.select_all_and_paste()

    def update_note_content(self, note_name: str, new_body: str) -> None:
        """
        Update a note's content with plain text, preserving note structure.

        Converts plain text to RTF and pastes via UI scripting.

        IMPORTANT: This operation:
        - Requires Apple Notes app to be open (will be launched if needed)
        - Requires accessibility permissions for the terminal/app
        - Will briefly focus the note during the operation
        - Replaces all existing content in the note

        Args:
            note_name: The exact title of the note to update
            new_body: The new content as plain text

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If the update operation fails
        """
        # Convert plain text to RTF
        rtf_data = self._clipboard.text_to_rtf(new_body)

        # Write RTF to the note
        self.write_note_from_rtf(note_name, rtf_data)

    def _send_key_code(self, key_code: int, modifiers: str | None = None) -> None:
        """
        Send a key code to Apple Notes using System Events.

        Key codes are used for non-character keys like arrows.

        Args:
            key_code: The key code (e.g., 125 for Down arrow)
            modifiers: Optional modifiers (e.g., "command down", "{shift down, command down}")

        Raises:
            NotesNotFrontmostError: If Apple Notes is not frontmost
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If keystroke fails
        """
        if modifiers:
            action = (
                f'tell process "{self.NOTES_PROCESS_NAME}" '
                f"to key code {key_code} using {modifiers}"
            )
        else:
            action = f'tell process "{self.NOTES_PROCESS_NAME}" to key code {key_code}'

        success, output = self._guarded_system_events(action)
        if not success:
            self._raise_keystroke_failure(output, "send key code")

    def move_cursor_to_end(self) -> None:
        """
        Move cursor to end of note (Cmd+Down).

        Raises:
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If cursor movement fails
        """
        # Key code 125 = Down arrow
        self._send_key_code(125, "command down")
        time.sleep(self.KEYSTROKE_DELAY)

    def move_cursor_to_start(self) -> None:
        """
        Move cursor to start of note (Cmd+Up).

        Raises:
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If cursor movement fails
        """
        # Key code 126 = Up arrow
        self._send_key_code(126, "command down")
        time.sleep(self.KEYSTROKE_DELAY)

    def append_to_note(self, note_name: str, text_to_append: str) -> None:
        """
        Append text to end of note, preserving all existing content and formatting.

        This is the safest way to add content to a note without affecting
        existing formatting, internal links, or other special content.

        IMPORTANT: This operation:
        - Requires Apple Notes app to be open (will be launched if needed)
        - Requires accessibility permissions for the terminal/app
        - Will briefly focus the note during the operation
        - Adds content at the very end, after existing content

        NOTE: Emojis are not supported - RTF encoding does not handle them
        properly and they will appear as placeholder characters.

        Args:
            note_name: The exact title of the note to append to
            text_to_append: The text to add at the end (no emojis)

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If the append operation fails
        """
        # Verify note exists first
        if not self._note_exists(note_name):
            raise NoteNotFoundError(f"Note '{note_name}' not found")

        # Open the note
        self.open_note(note_name)
        time.sleep(self.NOTE_LOAD_DELAY)

        # Ensure focus is in the note content area
        self._focus_note_content()

        # Move cursor to end of note (Cmd+Down)
        self.move_cursor_to_end()

        # Convert text to RTF and write to clipboard
        # Add newline prefix to separate from existing content
        rtf_data = self._clipboard.text_to_rtf("\n" + text_to_append)
        self._clipboard.write_rtf(rtf_data)

        # Brief delay to ensure clipboard is ready
        time.sleep(self.KEYSTROKE_DELAY)

        # Paste at cursor position (end of note)
        self.paste_from_clipboard()

    def append_richtext_to_note(self, note_name: str, data: bytes) -> None:
        """
        Append Notes richtext content to end of note, preserving all existing content.

        Similar to append_to_note but writes Notes richtext format instead of RTF,
        preserving full formatting including checklists and block quotes.

        Args:
            note_name: The exact title of the note to append to
            data: Notes richtext content as bytes to append

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If the append operation fails
        """
        # Verify note exists first
        if not self._note_exists(note_name):
            raise NoteNotFoundError(f"Note '{note_name}' not found")

        # Open the note
        self.open_note(note_name)
        time.sleep(self.NOTE_LOAD_DELAY)

        # Ensure focus is in the note content area
        self._focus_note_content()

        # Move cursor to end of note (Cmd+Down)
        self.move_cursor_to_end()

        # Insert newline to separate from existing content
        self._send_key_code(self.RETURN_KEY_CODE)
        time.sleep(self.KEYSTROKE_DELAY)

        # Write Notes richtext to clipboard and paste
        self._clipboard.write_notes_richtext(data)
        time.sleep(self.KEYSTROKE_DELAY)
        self.paste_from_clipboard()

    def create_note_with_content(
        self, name: str, body: str, folder: str = "Notes"
    ) -> None:
        """
        Create a new note entirely via UI scripting.

        Uses Cmd+N to create note, pastes title and body via clipboard.
        Note: Always creates in default "Notes" folder.

        IMPORTANT: This operation uses UI scripting and requires:
        - Apple Notes app to be open (will be launched if needed)
        - Accessibility permissions for the terminal/app
        - The note will be briefly focused during the operation

        NOTE: Emojis are not supported - RTF encoding does not handle them
        properly and they will appear as placeholder characters.

        Args:
            name: The note title (max 255 chars, no newlines)
            body: The body content (plain text, converted to RTF)
            folder: The folder to create the note in (only "Notes" supported)

        Raises:
            ValueError: If folder is not "Notes"
            AppleScriptError: If activation fails
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If UI scripting fails
        """
        # UI scripting can only create notes in the default "Notes" folder
        if folder != "Notes":
            raise ValueError(
                f"UI scripting only supports 'Notes' folder, got '{folder}'"
            )

        # Step 1: Activate Notes app
        self._activate_notes()
        time.sleep(self.NOTE_LOAD_DELAY)

        # Step 2: Create new note via Cmd+N
        self._send_keystroke("n")  # Cmd+N
        time.sleep(self.NOTE_LOAD_DELAY)

        # Step 3: Title field is focused and selected - paste title
        # Use plain text for title (not RTF - title shouldn't have formatting)
        self._clipboard.write_plain_text(name)
        self.paste_from_clipboard()
        time.sleep(self.KEYSTROKE_DELAY)

        # Step 4: Move to body field (Return key creates new line = body)
        self._send_key_code(self.RETURN_KEY_CODE)
        time.sleep(self.KEYSTROKE_DELAY)

        # Step 5: Paste body content as RTF (if non-empty)
        if body and body.strip():
            rtf_data = self._clipboard.text_to_rtf(body)
            self._clipboard.write_rtf(rtf_data)
            time.sleep(self.KEYSTROKE_DELAY)
            self.paste_from_clipboard()

    def create_note_with_richtext(
        self, name: str, richtext_data: bytes, folder: str = "Notes"
    ) -> None:
        """
        Create a new note and set its body to Notes richtext in one UI session.

        Creates the note via Cmd+N, pastes the title, moves to the body, and
        pastes the richtext data — all without re-opening or re-focusing the note.

        Args:
            name: The note title (max 255 chars, no newlines)
            richtext_data: Body content as Notes richtext bytes
            folder: The folder to create the note in (only "Notes" supported)

        Raises:
            ValueError: If folder is not "Notes"
            AppleScriptError: If activation fails
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If UI scripting fails
        """
        if folder != "Notes":
            raise ValueError(
                f"UI scripting only supports 'Notes' folder, got '{folder}'"
            )

        # Step 1: Activate Notes app
        self._activate_notes()
        time.sleep(self.NOTE_LOAD_DELAY)

        # Step 2: Create new note via Cmd+N
        self._send_keystroke("n")  # Cmd+N
        time.sleep(self.NOTE_LOAD_DELAY)

        # Step 3: Title field is focused and selected - paste title
        self._clipboard.write_plain_text(name)
        self.paste_from_clipboard()
        time.sleep(self.KEYSTROKE_DELAY)

        # Step 4: Move to body field (Return key)
        self._send_key_code(self.RETURN_KEY_CODE)
        time.sleep(self.KEYSTROKE_DELAY)

        # Step 5: Paste richtext body — cursor is already in the body
        self._clipboard.write_notes_richtext(richtext_data)
        time.sleep(self.KEYSTROKE_DELAY)
        self.paste_from_clipboard()

    def _save_debug_notes_richtext(self, data: bytes, label: str) -> Path:
        """
        Save Notes richtext data to debug directory for troubleshooting.

        Args:
            data: The Notes richtext content to save
            label: A label like "before" or "after"

        Returns:
            Path to the saved file
        """
        self.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"notes_richtext_{label}_{timestamp}.bin"
        filepath = self.DEBUG_DIR / filename
        filepath.write_bytes(data)
        return filepath

    def find_replace_in_note(
        self, note_name: str, search_text: str, replace_text: str
    ) -> int:
        """
        Find and replace text in a note while preserving ALL formatting.

        For same-length replacements (len(search_text) == len(replace_text)),
        uses Apple Notes' native richtext format which preserves ALL formatting
        including checklists, block quotes, and other Notes-specific elements.

        For different-length replacements, falls back to RTF-based replacement
        which preserves most formatting but may affect checklists and block quotes.

        The operation:
        1. Opens the note and copies content
        2. For same-length: modifies Notes richtext directly
           For different-length: modifies RTF bytes
        3. Writes modified content back to clipboard
        4. Selects all and pastes to update the note

        IMPORTANT: This operation:
        - Requires Apple Notes app to be open (will be launched if needed)
        - Requires accessibility permissions for the terminal/app
        - Will briefly focus the note during the operation
        - Replaces ALL occurrences of search_text in the note BODY; the first
          line (the note's title) is never modified

        Args:
            note_name: The exact title of the note to modify
            search_text: Text to find (case-sensitive)
            replace_text: Text to replace with

        Returns:
            Number of replacements made

        Raises:
            NoteNotFoundError: If note doesn't exist
            AccessibilityPermissionError: If accessibility not granted
            UIScriptingError: If the operation fails
            ClipboardError: If clipboard operations fail
        """
        # Verify note exists first
        if not self._note_exists(note_name):
            raise NoteNotFoundError(f"Note '{note_name}' not found")

        # Always use Notes richtext format - preserves ALL formatting
        # Variable-length replacements are supported via the richtext module
        return self._find_replace_via_notes_richtext(
            note_name, search_text, replace_text
        )

    def _find_replace_via_notes_richtext(
        self,
        note_name: str,
        search_text: str,
        replace_text: str,
        links=None,
        capture: dict | None = None,
    ) -> int:
        """
        Find/replace using Apple Notes richtext format.

        This preserves ALL formatting including checklists and block quotes.
        Supports both same-length and variable-length replacements.
        Replacement text is parsed as markdown — bold, italic, etc. are applied.

        Args:
            note_name: Name of the note
            search_text: Text to find
            replace_text: Replacement text (supports markdown formatting)
            links: Optional lower-case title -> LinkTarget mapping; those
                [[links]] are pasted as synthesized attachments (issue #212)
            capture: If given, filled with ``"before"`` (the note as copied)
                and ``"expected"`` (the modified richtext pasted back), so the
                caller can verify the result in the editor (issue #200)
        """
        # Read note content as Notes richtext (this focuses the note content)
        data = self.read_note_as_notes_richtext(note_name)
        if capture is not None:
            capture["before"] = data

        # Save debug: original data
        if self._debug:
            before_path = self._save_debug_notes_richtext(data, "before")
            print(f"[DEBUG] Saved original Notes richtext to: {before_path}")

        # Perform find/replace on Notes richtext data (supports variable-length
        # and markdown formatting in the replacement)
        with profiling.span("fr_compute"):
            modified_data, replacement_count, pending_actions = (
                self._clipboard.find_replace_in_notes_richtext_variable(
                    data,
                    search_text,
                    replace_text,
                    links=links,
                )
            )

        # Save debug: modified data
        if self._debug:
            after_path = self._save_debug_notes_richtext(modified_data, "after")
            print(f"[DEBUG] Saved modified Notes richtext to: {after_path}")
            print(f"[DEBUG] Replacements made: {replacement_count}")

        if replacement_count == 0:
            # Deselect content to avoid leaving UI in bad state
            # (Cmd+A from read operation left everything selected)
            self._send_key_code(self.ESCAPE_KEY_CODE)
            time.sleep(self.KEYSTROKE_DELAY)
            return 0

        # Read RTF only now that we know a replacement will happen — Notes
        # may need this flavor for pasting, but a zero-match return must not
        # pay a second promised-type pasteboard read (review finding, #141)
        rtf_data = self._clipboard.read_rtf()

        # Also do find/replace on RTF so both formats match
        modified_rtf = None
        if rtf_data is not None:
            try:
                modified_rtf, _ = self._clipboard.find_replace_in_raw_rtf(
                    rtf_data, search_text, replace_text, body_only=True
                )
            except Exception:
                # If RTF find/replace fails, continue without it
                modified_rtf = rtf_data

        if capture is not None:
            capture["expected"] = modified_data

        # Write modified Notes richtext to clipboard (with RTF fallback)
        with profiling.span("clip_write"):
            self._clipboard.write_notes_richtext(modified_data, modified_rtf)
        time.sleep(self.KEYSTROKE_DELAY)

        # Focus is already in note content from the read operation above,
        # so we can directly select all and paste without re-focusing
        self.select_all_and_paste()

        # Apply pending keyboard actions (tables, hashtags)
        if pending_actions:
            with profiling.span("pending"):
                self.apply_pending_keyboard_actions(pending_actions)

        return replacement_count

    def _find_replace_via_rtf(
        self, note_name: str, search_text: str, replace_text: str
    ) -> int:
        """
        Find/replace using RTF format (supports different-length replacements).

        Note: This may affect checklists and block quote formatting.
        """
        # Read note content as RTF (this focuses the note content)
        rtf_data = self.read_note_as_rtf(note_name)

        # Save debug: original RTF
        if self._debug:
            before_path = self._save_debug_rtf(rtf_data, "before")
            print(f"[DEBUG] Saved original RTF to: {before_path}")

        # Perform find/replace on RTF bytes
        modified_rtf, replacement_count = self._clipboard.find_replace_in_raw_rtf(
            rtf_data, search_text, replace_text
        )

        # Save debug: modified RTF
        if self._debug:
            after_path = self._save_debug_rtf(modified_rtf, "after")
            print(f"[DEBUG] Saved modified RTF to: {after_path}")
            print(f"[DEBUG] Replacements made: {replacement_count}")

        if replacement_count == 0:
            return 0

        # Write modified RTF to clipboard
        self._clipboard.write_rtf(modified_rtf)
        time.sleep(self.KEYSTROKE_DELAY)

        # Focus is already in note content from the read operation above,
        # so we can directly select all and paste without re-focusing
        self._send_keystroke("a")  # Cmd+A to select all
        time.sleep(self.KEYSTROKE_DELAY)
        self.paste_from_clipboard()

        return replacement_count

    # --- Keyboard automation for hashtags and tables ---

    def _type_text(self, text: str) -> None:
        """Type text without modifier keys.

        For ASCII-only text, uses AppleScript keystroke for character-by-character
        input (needed for triggers like # that Notes recognizes from typing).
        For text containing non-ASCII characters (e.g., ★), uses clipboard paste
        since AppleScript keystroke can't handle Unicode.

        Args:
            text: The text to type

        Raises:
            UIScriptingError: If typing fails
        """
        if not text.isascii():
            # Non-ASCII: use clipboard paste
            self._clipboard.write_plain_text(text)
            time.sleep(self.KEYSTROKE_DELAY)
            self._send_keystroke("v")  # Cmd+V to paste
            time.sleep(self.KEYSTROKE_DELAY)
            return

        escaped = AppleScriptExecutor._escape_applescript_string(text)
        # Typing time scales with the caller's text, not with a fixed key
        # count — keep the pre-#141 30s budget rather than the per-event cap.
        success, output = self._guarded_system_events(
            f'tell process "{self.NOTES_PROCESS_NAME}" to keystroke "{escaped}"',
            timeout=self.TEXT_ENTRY_TIMEOUT,
        )
        if not success:
            self._raise_keystroke_failure(
                output, "type text", timeout=self.TEXT_ENTRY_TIMEOUT
            )

    def _paste_text(self, text: str) -> None:
        """Paste text via clipboard into the currently focused field.

        Uses a single AppleScript to set the clipboard and paste in one call,
        ensuring no race condition. Handles Unicode characters that AppleScript
        keystroke cannot type (e.g., ★).

        Args:
            text: The text to paste

        Raises:
            UIScriptingError: If pasting fails
        """
        escaped = AppleScriptExecutor._escape_applescript_string(text)
        # The clipboard prelude carries the caller's text, so this script's
        # duration scales with it — keep the pre-#141 30s budget.
        success, output = self._guarded_system_events(
            f'tell process "{self.NOTES_PROCESS_NAME}" '
            f'to keystroke "v" using command down',
            prelude=f'set the clipboard to "{escaped}"',
            timeout=self.TEXT_ENTRY_TIMEOUT,
        )
        if not success:
            self._raise_keystroke_failure(
                output, "paste text", timeout=self.TEXT_ENTRY_TIMEOUT
            )

    def _find_and_select_text(self, search_text: str) -> None:
        """Find placeholder text and select it using Cmd+F.

        Opens the Find bar, types the search text, presses Return to
        jump to the first match, then Escape to close the Find bar
        (leaving the found text selected).

        Args:
            search_text: The placeholder text to find

        Raises:
            UIScriptingError: If find/select fails
        """
        # Cmd+F to open Find bar
        self._send_keystroke("f")
        time.sleep(self.FIND_BAR_DELAY)

        # Type search text into Find field
        self._type_text(search_text)
        time.sleep(self.KEYSTROKE_DELAY)

        # Return to jump to first match
        self._send_key_code(self.RETURN_KEY_CODE)
        time.sleep(self.KEYSTROKE_DELAY)

        # Escape to close Find bar (found text stays selected)
        self._send_key_code(self.ESCAPE_KEY_CODE)
        time.sleep(self.KEYSTROKE_DELAY)

        # The tag and table flows start with Delete. On a miss Notes keeps
        # the previous selection or a bare caret (seen live 2026-11-09), so
        # that Delete would erase real note text (issue #122). Find also
        # ignores case, so only an exact match counts.
        with profiling.span("find_check"):
            for attempt in range(1 + self.SELECTION_RETRIES):
                if attempt:
                    time.sleep(self.KEYSTROKE_DELAY)
                selected = self._selected_text()
                if selected == search_text:
                    return
            # The leftover selection is the user's own text: collapse it, or
            # their next key replaces it. Left only moves the caret.
            try:
                self._send_key_code(self.LEFT_ARROW_KEY_CODE)
            except UIScriptingError:
                pass
        raise PlaceholderNotFoundError(
            f"Find didn't select {search_text}: "
            + (
                "nothing was selected."
                if not selected
                else f"it selected other text ({len(selected)} characters)."
            )
        )

    def _selected_text(self) -> str | None:
        """The text selected in the note, by copying it (issue #122).

        The pasteboard is cleared first, so a bare caret (Cmd+C copies
        nothing) reads as ``None``. Callers run inside a write's clipboard
        save/restore. Not Accessibility: AXFocusedUIElement on Notes took
        ~30s per query under load (measured 2026-11-09), where a copy took
        ~0.5s. The read goes through ClipboardManager's fresh-thread choke
        point (#141).
        """
        self._clipboard.clear()
        self._send_keystroke("c")
        time.sleep(self.KEYSTROKE_DELAY)
        return self._clipboard.read_plain_text()

    def _replace_with_hashtag(self, tag_name: str) -> None:
        """Replace selected text with a native hashtag.

        Assumes the placeholder text is already selected. Deletes it,
        types the hashtag trigger (#tagname), presses Space to confirm
        (which converts to native tag), then Backspace to remove the
        trailing space.

        Args:
            tag_name: The tag name without the # prefix

        Raises:
            UIScriptingError: If replacement fails
        """
        # Delete selected placeholder
        self._send_key_code(self.DELETE_KEY_CODE)
        time.sleep(self.KEYSTROKE_DELAY)

        # Type #tagname
        self._type_text(f"#{tag_name}")
        time.sleep(self.KEYSTROKE_DELAY)

        # Typing "#tagname" opens Notes' tag-suggestion popup (a separate
        # AXSystemDialog window): the keys below go to it on purpose, so they
        # skip the open-dialog check (the frontmost check stays; issue #199).
        with self._expecting_dialog():
            # Space to confirm hashtag (Notes converts to native attachment)
            self._type_text(" ")
            time.sleep(self.HASHTAG_CONFIRM_DELAY)

            # Backspace to remove trailing space
            self._send_key_code(self.DELETE_KEY_CODE)
            time.sleep(self.KEYSTROKE_DELAY)

    def _replace_with_table(self, cells: list[list[str]], col_count: int) -> None:
        """Replace selected placeholder with a native Apple Notes table.

        Assumes the placeholder text is already selected. Deletes it,
        inserts a native 2×2 table via Option+Cmd+T, then fills each cell;
        Tab past the last cell creates each further row.

        Only 2-column tables can be typed: Notes has no add-column shortcut,
        so ``refuse_untypeable_actions`` refuses any other width before the
        paste (issues #122, #260).

        Args:
            cells: 2D grid of cell text (rows × columns)
            col_count: Number of columns (always 2 here)

        Raises:
            UIScriptingError: If table insertion fails
        """
        row_count = len(cells)

        # Delete selected placeholder
        self._send_key_code(self.DELETE_KEY_CODE)
        time.sleep(self.KEYSTROKE_DELAY)

        # Option+Cmd+T to insert native 2×2 table. Don't add rows up front:
        # Tab at the last cell auto-creates a new row, which avoids cursor
        # position issues from Option+Cmd+Down.
        self._send_key_code_with_modifiers(self.T_KEY_CODE, ["option", "command"])
        time.sleep(self.TABLE_INSERT_DELAY)

        # Fill cells row-by-row using Tab to navigate. A short row is padded
        # with empty cells so every row tabs across all columns; otherwise
        # later rows shift into the wrong cells (issue #122).
        for r_idx, row in enumerate(cells):
            padded = list(row[:col_count]) + [""] * (col_count - len(row))
            for c_idx, cell_text in enumerate(padded):
                if cell_text:
                    self._paste_text(cell_text)
                    time.sleep(self.KEYSTROKE_DELAY)

                # Tab to next cell (skip after last cell)
                is_last_cell = r_idx == row_count - 1 and c_idx == col_count - 1
                if not is_last_cell:
                    self._send_key_code(self.TAB_KEY_CODE)
                    time.sleep(self.KEYSTROKE_DELAY)

        # Escape to exit table
        self._send_key_code(self.ESCAPE_KEY_CODE)
        time.sleep(self.KEYSTROKE_DELAY)

    def _send_key_code_with_modifiers(
        self, key_code: int, modifiers: list[str]
    ) -> None:
        """Send a key code with modifier keys via AppleScript.

        Args:
            key_code: macOS key code
            modifiers: List of modifier names ("command", "option", "shift", "control")

        Raises:
            UIScriptingError: If keystroke fails
        """
        modifier_str = ", ".join(f"{m} down" for m in modifiers)
        success, output = self._guarded_system_events(
            f'tell process "{self.NOTES_PROCESS_NAME}" '
            f"to key code {key_code} using {{{modifier_str}}}"
        )
        if not success:
            self._raise_keystroke_failure(
                output, f"send key code {key_code} with modifiers"
            )

    def apply_pending_keyboard_actions(
        self, actions: list[PendingKeyboardAction]
    ) -> int:
        """Apply pending keyboard actions (hashtags, tables) after pasting richtext.

        Iterates through actions, using Cmd+F to find each placeholder, then
        typing the native trigger sequence (``#tag`` + Space for tags, the
        table flow for tables).

        Assumes Notes is already active and the note with placeholders
        is focused.

        ``[[links]]`` are never typed: every write path synthesizes them as
        link attachments in the pasted richtext (issue #212), because the
        Cmd+K picker dropped links, picked fuzzy matches and left its sheet
        open. A ``wiki_link`` action here is a caller bug and is refused
        before any keystroke.

        Args:
            actions: List of PendingKeyboardAction to apply

        Returns:
            Count of actions successfully applied

        Raises:
            UIScriptingError: If keyboard automation fails, or an action is a
                wiki link
        """
        refuse_untypeable_actions(actions)

        applied = 0
        typed_table = False
        for action in actions:
            try:
                self._find_and_select_text(action.placeholder)
            except PlaceholderNotFoundError as e:
                raise PlaceholderNotFoundError(
                    f"{e} Typed {applied} of {len(actions)} tags/tables; "
                    f"nothing was deleted, and the note still holds the "
                    f"literal {action.placeholder} (and any later "
                    "placeholders). Don't retry the whole write; fix the "
                    "note by hand."
                ) from e

            if action.action_type == "hashtag":
                self._replace_with_hashtag(action.text_to_type)
            else:
                self._replace_with_table(action.table_cells, action.table_col_count)
                typed_table = True

            applied += 1
        if typed_table:
            # The table flow's closing Escape leaves keyboard focus inside the
            # table, so the verify copy-back's Cmd+A would copy one cell and
            # report a landed write as partial (#222, #223). Put focus back in
            # the note body once, so the next Cmd+A covers the whole note.
            # The table has landed by now: a failed refocus must not surface
            # as a write error a caller would retry (that duplicates the
            # write); the copy-back reports what it can or can't read.
            try:
                self._focus_note_content()
            except UIScriptingError:
                pass
        return applied


def refuse_untypeable_actions(actions: Sequence[PendingKeyboardAction]) -> None:
    """Raise UIScriptingError unless every action is a hashtag or a table.

    Note links are synthesized into the pasted richtext (issue #212); a
    ``wiki_link`` action means a caller on pre-#212 code. Write paths call this
    before their paste, so a refused payload never lands half-written.
    """
    for action in actions:
        if action.action_type == "wiki_link":
            raise UIScriptingError(
                "Can't type a note link: links must be synthesized into the "
                "pasted richtext (issue #212); restart the MCP server and UI "
                "helper daemon so both run current code. Nothing was written."
            )
        if action.action_type not in ("hashtag", "table"):
            raise UIScriptingError(f"Unknown action type: {action.action_type}")
        # The typed flow inserts Notes' default 2x2 table and adds rows with
        # Tab, but it can't change the column count: a one-column table
        # landed scrambled (issue #122), and Notes has no add-column shortcut
        # (Option+Cmd+Right did nothing), so a wider one landed as loose cell
        # text beside an empty 2x2 (issue #260).
        if action.action_type == "table":
            cols = action.table_col_count or 0
            if cols < 2:
                raise UIScriptingError(
                    "Can't type a one-column table: Apple Notes tables need at "
                    "least 2 columns. Add a column or write it as a list. "
                    "Nothing was written."
                )
            if cols > 2:
                raise UIScriptingError(
                    f"Can't type a table with {cols} columns: only tables of "
                    "exactly 2 columns (any number of rows) can be written. "
                    "Split it into 2-column tables or write it as a list. "
                    "Nothing was written."
                )


def notes_modal_title() -> str | None:
    """Title of a sheet or dialog open in Apple Notes, else None (issue #199).

    A read-only System Events probe (no keystrokes, Notes need not be
    frontmost) using the same detection as the per-key re-check, so the
    remote drain worker can hold a write while a dialog is up instead of
    running the whole write pipeline into it every pass. An untitled dialog
    reports as "dialog". Fails open: any probe failure returns None and the
    write's own guarded keystroke path stays the safety net.
    """
    script = (
        'tell application "System Events"\n'
        f'    if not (exists process "{UIScriptingExecutor.NOTES_PROCESS_NAME}") '
        'then return ""\n'
        + _modal_scan_script(UIScriptingExecutor.NOTES_PROCESS_NAME)
        + "    if modalTitle is not missing value then\n"
        f'        return "{MODAL_SENTINEL}:" & modalTitle\n'
        "    end if\n"
        '    return ""\n'
        "end tell\n"
    )
    try:
        success, output = AppleScriptExecutor().execute(
            script, timeout=UIScriptingExecutor.SYSTEM_EVENTS_TIMEOUT
        )
    except Exception:
        return None
    output = (output or "").strip()
    if not success or not output.startswith(f"{MODAL_SENTINEL}:"):
        return None
    return _clean_modal_title(output[len(MODAL_SENTINEL) + 1 :]) or "dialog"
