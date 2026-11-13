"""Classification of UI-scripting AppleScript failures (issue #133).

Pure string-classification tests — no GUI session, no Apple Notes, so this
module deliberately carries no `macos_gui` marker and runs in CI.

Background: a genuine Accessibility/TCC denial and a `first process whose ...`
filter that matches nothing BOTH surface as AppleScript error -1719
(`errAEIllegalIndex`). The code alone cannot tell them apart; only the message
text can. The literal denial strings below were read out of
`/System/Library/CoreServices/System Events.app/Contents/MacOS/System Events`:

    %@ is not allowed assistive access.
    %@ is not allowed to send keystrokes.
    Access for assistive devices is disabled.
    Sending keystrokes is disabled.
"""

import pytest

from apple_notes.ui_scripting import (
    _is_accessibility_denial,
    _is_no_frontmost_match,
)

# Real osascript output captured on macOS 15 for a no-match filter reference.
NO_FRONTMOST_OUTPUT = (
    "43:96: execution error: System Events got an error: Can’t get process 1 "
    "whose frontmost = true. Invalid index. (-1719)"
)

DENIAL_ASSISTIVE = (
    "34:42: execution error: System Events got an error: osascript is not "
    "allowed assistive access. (-1719)"
)
DENIAL_KEYSTROKES = (
    "34:42: execution error: System Events got an error: osascript is not "
    "allowed to send keystrokes. (-25211)"
)
DENIAL_DEVICES_DISABLED = (
    "execution error: System Events got an error: Access for assistive "
    "devices is disabled. (-25211)"
)
DENIAL_SENDING_DISABLED = (
    "execution error: System Events got an error: Sending keystrokes is "
    "disabled. (-25211)"
)


class TestAccessibilityDenialDetection:
    @pytest.mark.parametrize(
        "output",
        [
            DENIAL_ASSISTIVE,
            DENIAL_KEYSTROKES,
            DENIAL_DEVICES_DISABLED,
            DENIAL_SENDING_DISABLED,
        ],
    )
    def test_recognizes_real_denial_messages(self, output):
        assert _is_accessibility_denial(output) is True

    def test_denial_match_is_case_insensitive(self):
        assert _is_accessibility_denial(DENIAL_ASSISTIVE.upper()) is True

    def test_no_frontmost_match_is_not_a_denial(self):
        """The crux of #133: -1719 alone must not imply a permissions problem."""
        assert _is_accessibility_denial(NO_FRONTMOST_OUTPUT) is False

    def test_bare_error_code_is_not_a_denial(self):
        assert _is_accessibility_denial("error -1719") is False

    @pytest.mark.parametrize(
        "output",
        [
            # -1713 errAENoUserInteraction. Documented by Apple with this exact
            # wording, and reachable on the headless/LaunchAgent write path when
            # a script would need to show a dialog. Nothing to do with TCC.
            "execution error: User interaction is not allowed. (-1713)",
            "execution error: The operation is not allowed. (-5000)",
            "execution error: Modifying a locked file is not allowed.",
            "execution error: not allowed to write to that volume",
        ],
    )
    def test_generic_not_allowed_is_not_a_denial(self, output):
        """A bare 'not allowed' substring is too broad to mean TCC denial."""
        assert _is_accessibility_denial(output) is False


class TestNoFrontmostMatchDetection:
    def test_recognizes_invalid_index_from_frontmost_filter(self):
        assert _is_no_frontmost_match(NO_FRONTMOST_OUTPUT) is True

    def test_denial_is_not_a_no_frontmost_match(self):
        assert _is_no_frontmost_match(DENIAL_ASSISTIVE) is False

    def test_unrelated_failure_is_not_a_no_frontmost_match(self):
        assert _is_no_frontmost_match("execution error: Unknown error") is False


# --- Fixtures the previous tests lacked -----------------------------------
#
# Every earlier denial fixture omits "invalid index", and NO_FRONTMOST_OUTPUT
# omits every denial phrase, so no test ever forced the two classifiers to
# disagree about the same string. That left the safety conjunct in
# `_is_no_frontmost_match` unexercised: deleting it kept the whole suite green.
# These fixtures make the classifiers actually compete.

# A denial and the frontmost-lookup failure in ONE output. This is the
# dangerous direction: misreading it as the retryable case would tell a user to
# retry forever on a permanent permissions problem.
DENIAL_WITH_INDEX_TEXT = (
    "34:42: execution error: System Events got an error: osascript is not "
    "allowed assistive access. Can’t get process 1 whose frontmost = true. "
    "Invalid index. (-1719)"
)

# errAEIllegalIndex from something that is NOT the frontmost lookup — e.g. an
# index-based menu/row/window reference. Retrying this never helps.
UNRELATED_INVALID_INDEX = (
    "execution error: System Events got an error: Can’t get menu item 7 of "
    "menu 1 of menu bar item 3. Invalid index. (-1719)"
)


class TestClassifierPrecedence:
    """The two predicates must never both claim the same output."""

    def test_denial_wins_when_output_carries_both_signals(self):
        assert _is_accessibility_denial(DENIAL_WITH_INDEX_TEXT) is True
        assert _is_no_frontmost_match(DENIAL_WITH_INDEX_TEXT) is False

    def test_unrelated_invalid_index_is_not_a_focus_race(self):
        """ "invalid index" alone must not mean "no frontmost process"."""
        assert _is_no_frontmost_match(UNRELATED_INVALID_INDEX) is False
        assert _is_accessibility_denial(UNRELATED_INVALID_INDEX) is False

    def test_genuine_no_frontmost_still_classifies(self):
        assert _is_no_frontmost_match(NO_FRONTMOST_OUTPUT) is True
        assert _is_accessibility_denial(NO_FRONTMOST_OUTPUT) is False


# --- Guard behavior, in CI ------------------------------------------------
#
# These exercise the real call sites, not just the predicates. They were put
# here while test_ui_scripting.py was marked macos_gui and invisible to CI;
# that module runs in CI too now (issue #143). Nothing below needs a GUI:
# AppleScript and the clipboard are fully mocked.

from unittest.mock import MagicMock, patch  # noqa: E402

from apple_notes.ui_scripting import UIScriptingExecutor  # noqa: E402
from apple_notes.exceptions import (  # noqa: E402
    AccessibilityPermissionError,
    NotesNotFrontmostError,
    NotesNotRespondingError,
    UIScriptingError,
)

SENTINEL_WITH_INDEX = (
    "execution error: NOTES_NOT_FRONTMOST: Ghostty (1) Can’t get process 1 "
    "whose frontmost = true. Invalid index. (-1719)"
)


class TestGuardBehavior:
    @pytest.fixture
    def mock_applescript(self):
        with patch("apple_notes.ui_scripting.AppleScriptExecutor") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            yield mock_instance

    @pytest.fixture(autouse=True)
    def mock_clipboard(self):
        with patch("apple_notes.ui_scripting.ClipboardManager") as mock_class:
            mock_class.return_value = MagicMock()
            yield

    def test_no_frontmost_returns_empty_rather_than_raising(self, mock_applescript):
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)
        assert UIScriptingExecutor()._frontmost_process_name() == ""

    def test_settle_poll_rides_out_a_transient_race(
        self, mock_applescript, frontmost_clock
    ):
        mock_applescript.execute.side_effect = [
            (False, NO_FRONTMOST_OUTPUT),
            (True, "Notes"),
        ]
        assert UIScriptingExecutor()._ensure_notes_frontmost() is None

    def test_poll_gives_up_on_the_deadline_not_an_attempt_count(
        self, mock_applescript, frontmost_clock
    ):
        """Issue #141: the guard is a wall-clock budget. With ~3s osascript
        probes the old fixed 20-attempt loop blocked ~67s before failing."""

        def slow_probe(script, timeout=None):
            frontmost_clock.advance(3.0)
            return (True, "Ghostty")

        mock_applescript.execute.side_effect = slow_probe
        with pytest.raises(NotesNotFrontmostError, match="Ghostty"):
            UIScriptingExecutor()._ensure_notes_frontmost()

        assert mock_applescript.execute.call_count == 2

    def test_persistent_no_frontmost_uses_the_distinct_message(
        self, mock_applescript, frontmost_clock
    ):
        """Pins the dedicated branch, not just any NotesNotFrontmostError.

        The generic fallback would say "frontmost app: ''"; deleting the
        dedicated branch must fail this test.
        """
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)
        with pytest.raises(NotesNotFrontmostError) as exc:
            UIScriptingExecutor()._ensure_notes_frontmost(timeout=0.3)

        message = str(exc.value)
        assert "no application reported as frontmost" in message
        assert "after 0.3s" in message
        assert "retry" in message.lower()
        # Permissions appear only as the conditional fallback for the case the
        # text classifier cannot see (non-English locale, bare index error) —
        # never as the primary diagnosis.
        assert "keeps failing" in message
        assert "Accessibility" in message

    def test_last_named_app_survives_a_final_poll_blip(
        self, mock_applescript, frontmost_clock
    ):
        """One "" on the last poll must not erase the offending app name."""
        mock_applescript.execute.side_effect = [
            (True, "Ghostty"),
            (False, NO_FRONTMOST_OUTPUT),
        ]
        # One interval of budget: probe, sleep 0.1s, probe, deadline reached.
        with pytest.raises(NotesNotFrontmostError) as exc:
            UIScriptingExecutor()._ensure_notes_frontmost(timeout=0.1)

        message = str(exc.value)
        assert "Ghostty" in message
        assert "no application reported as frontmost" not in message

    def test_sentinel_wins_when_output_also_carries_invalid_index(
        self, mock_applescript
    ):
        """Real precedence: the offending app name must survive.

        The fixture trips BOTH the sentinel branch and the no-frontmost
        classifier, so reordering the branches fails this test.
        """
        mock_applescript.execute.return_value = (False, SENTINEL_WITH_INDEX)
        with pytest.raises(NotesNotFrontmostError, match="Ghostty"):
            UIScriptingExecutor()._send_keystroke("v")

    def test_keystroke_race_does_not_promise_the_note_is_untouched(
        self, mock_applescript
    ):
        """`_raise_keystroke_failure` fires mid-sequence, after earlier steps
        may already have deleted or typed content — it must not claim
        "nothing was typed" for the whole operation."""
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)
        with pytest.raises(NotesNotFrontmostError) as exc:
            UIScriptingExecutor()._send_keystroke("a")

        message = str(exc.value)
        assert "may already have modified the note" in message
        assert "Nothing was typed" not in message

    def test_no_frontmost_message_does_not_promise_this_step_typed_nothing(
        self, mock_applescript
    ):
        """Since keystrokes batch (issue #141), a `_frontmost_recheck` abort
        can happen after earlier keys in the SAME script already fired — so
        "this step sent no keystrokes" is no longer true."""
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)
        with pytest.raises(NotesNotFrontmostError) as exc:
            with patch("apple_notes.ui_scripting.time.sleep"):
                UIScriptingExecutor().select_all_and_paste()

        message = str(exc.value)
        assert "sent no keystrokes" not in message
        assert "aborted at a frontmost check" in message
        assert "may already" in message

    def test_real_denial_still_reports_as_permissions(self, mock_applescript):
        mock_applescript.execute.return_value = (False, DENIAL_ASSISTIVE)
        with pytest.raises(AccessibilityPermissionError):
            UIScriptingExecutor()._send_keystroke("a")

    def test_sentinel_without_colon_still_refuses_keystrokes(self, mock_applescript):
        """A malformed sentinel must degrade, never crash.

        The branch is entered on the bare name `NOTES_NOT_FRONTMOST` but the
        app name was extracted by splitting on `NOTES_NOT_FRONTMOST:` — with
        the colon. Any output carrying the name without the colon raised
        IndexError out of the safety branch, replacing the "refused to send
        keystrokes" signal with an opaque crash. Losing the app name is
        acceptable; losing the classification is not.
        """
        mock_applescript.execute.return_value = (
            False,
            "execution error: NOTES_NOT_FRONTMOST (-2700)",
        )
        with pytest.raises(NotesNotFrontmostError) as exc:
            UIScriptingExecutor()._send_keystroke("a")

        assert "another app" in str(exc.value)


# --- Timeout classification (issue #141) ----------------------------------
#
# An osascript that blows its budget carries none of the markers above, so it
# fell through to the generic UIScriptingError — or, worse, was reported as a
# focus problem. A stall is its own failure mode and gets its own exception.

TIMEOUT_OUTPUT = "AppleScript execution timed out after 10s"


class TestTimeoutClassification:
    def test_timeout_output_raises_notes_not_responding(self):
        executor = UIScriptingExecutor.__new__(UIScriptingExecutor)
        with pytest.raises(NotesNotRespondingError) as exc_info:
            executor._raise_keystroke_failure(TIMEOUT_OUTPUT, "send keystroke")

        message = str(exc_info.value)
        assert "didn't respond in 10s" in message
        assert "not frontmost" not in message.lower()

    def test_timeout_is_checked_before_generic_failure(self):
        """A timeout must not fall through to the generic UIScriptingError."""
        executor = UIScriptingExecutor.__new__(UIScriptingExecutor)
        with pytest.raises(NotesNotRespondingError):
            executor._raise_keystroke_failure(TIMEOUT_OUTPUT, "paste")

    def test_timeout_message_names_the_action_and_warns_about_earlier_steps(self):
        """Mid-sequence stalls must not promise the note is untouched."""
        executor = UIScriptingExecutor.__new__(UIScriptingExecutor)
        with pytest.raises(NotesNotRespondingError) as exc_info:
            executor._raise_keystroke_failure(TIMEOUT_OUTPUT, "paste content")

        message = str(exc_info.value)
        assert "paste content" in message
        assert "may already have modified the note" in message

    def test_message_reports_the_scripts_own_budget(self):
        """Batched/long scripts run on a budget scaled to their event count
        (issue #141 fix-wave) — the message must name that budget, not the
        per-event 10s cap, and must agree with the executor's timeout text."""
        executor = UIScriptingExecutor.__new__(UIScriptingExecutor)
        with pytest.raises(NotesNotRespondingError) as exc_info:
            executor._raise_keystroke_failure(
                "AppleScript execution timed out after 20s",
                "select all and paste",
                timeout=20.0,
            )

        assert "didn't respond in 20s" in str(exc_info.value)

    def test_message_defaults_to_the_per_event_budget(self):
        executor = UIScriptingExecutor.__new__(UIScriptingExecutor)
        with pytest.raises(NotesNotRespondingError) as exc_info:
            executor._raise_keystroke_failure(TIMEOUT_OUTPUT, "send keystroke")

        assert "didn't respond in 10s" in str(exc_info.value)

    def test_not_responding_is_a_ui_scripting_error(self):
        """Callers catching UIScriptingError (server.py, the daemon) must keep
        surfacing this as a clean error rather than an uncaught exception."""
        assert issubclass(NotesNotRespondingError, UIScriptingError)


# --- Missing-note vs. other AppleScript failures (issue #126, NTG-021) --------
#
# The accessors used to wrap their whole script in `on error error "Note not
# found"`, so EVERY AppleScript failure — a timeout, a locked note, a
# permissions problem — was reported as NoteNotFoundError. Only Notes' own
# "Can’t get note …" (curly apostrophe in real output) is a missing note.

from apple_notes.applescript import is_note_not_found_output  # noqa: E402
from apple_notes.exceptions import (  # noqa: E402
    AppleScriptError,
    EncryptedNoteError,
    NoteNotFoundError,
)

MISSING_BY_FILTER = (
    "32:93: execution error: Notes got an error: Can’t get note 1 whose "
    'name = "nope". Invalid index. (-1719)'
)
MISSING_BY_NAME = (
    '28:67: execution error: Notes got an error: Can’t get note "nope". (-1728)'
)
UNRELATED_FAILURE = "execution error: Notes got an error: AppleEvent timed out. (-1712)"


class TestNoteNotFoundClassifier:
    @pytest.mark.parametrize(
        "output",
        [
            MISSING_BY_FILTER,
            MISSING_BY_NAME,
            'Can\'t get note whose name = "X"',  # ASCII apostrophe
        ],
    )
    def test_recognizes_missing_note_output(self, output):
        assert is_note_not_found_output(output)

    @pytest.mark.parametrize(
        "output",
        [
            UNRELATED_FAILURE,
            "AppleScript execution timed out after 10s",
            "Can’t get process 1 whose frontmost = true. Invalid index. (-1719)",
            "",
        ],
    )
    def test_other_failures_are_not_missing_notes(self, output):
        assert not is_note_not_found_output(output)


class TestNoteBodyHtmlErrorMapping:
    @pytest.fixture
    def mock_applescript(self):
        with patch("apple_notes.ui_scripting.AppleScriptExecutor") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            yield mock_instance

    @pytest.fixture(autouse=True)
    def mock_clipboard(self):
        with patch("apple_notes.ui_scripting.ClipboardManager") as mock_class:
            mock_class.return_value = MagicMock()
            yield

    def test_missing_note_is_not_found(self, mock_applescript):
        mock_applescript.execute.return_value = (False, MISSING_BY_FILTER)
        with pytest.raises(NoteNotFoundError):
            UIScriptingExecutor()._note_body_html("nope")

    def test_unrelated_failure_is_not_reported_as_not_found(self, mock_applescript):
        mock_applescript.execute.return_value = (False, UNRELATED_FAILURE)
        with pytest.raises(AppleScriptError) as exc_info:
            UIScriptingExecutor()._note_body_html("Real Note")
        assert not isinstance(exc_info.value, NoteNotFoundError)
        assert "timed out" in str(exc_info.value)

    def test_locked_note_is_reported_as_encrypted(self, mock_applescript):
        """Notes returns an EMPTY body for a locked note with no error at all,
        so the script has to ask `password protected` itself and signal it."""
        mock_applescript.execute.return_value = (
            False,
            "execution error: NOTE_PASSWORD_PROTECTED (-2700)",
        )
        with pytest.raises(EncryptedNoteError, match="Real Note"):
            UIScriptingExecutor()._note_body_html("Real Note")

    def test_script_checks_password_protected_and_has_no_blanket_handler(
        self, mock_applescript
    ):
        mock_applescript.execute.return_value = (True, "<div>body</div>")
        UIScriptingExecutor()._note_body_html("Real Note")
        script = mock_applescript.execute.call_args[0][0]
        assert "password protected" in script
        assert 'error "Note not found"' not in script


class TestOpenNoteErrorMapping:
    @pytest.fixture
    def mock_applescript(self):
        with patch("apple_notes.ui_scripting.AppleScriptExecutor") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            yield mock_instance

    @pytest.fixture(autouse=True)
    def mock_clipboard(self):
        with patch("apple_notes.ui_scripting.ClipboardManager") as mock_class:
            mock_class.return_value = MagicMock()
            yield

    def test_missing_note_with_curly_apostrophe_is_not_found(self, mock_applescript):
        mock_applescript.execute.return_value = (False, MISSING_BY_NAME)
        with pytest.raises(NoteNotFoundError):
            UIScriptingExecutor().open_note("nope")

    def test_unrelated_failure_is_not_reported_as_not_found(self, mock_applescript):
        mock_applescript.execute.return_value = (False, UNRELATED_FAILURE)
        with pytest.raises(AppleScriptError) as exc_info:
            UIScriptingExecutor().open_note("Real Note")
        assert not isinstance(exc_info.value, NoteNotFoundError)

    def test_script_has_no_blanket_not_found_handler(self, mock_applescript):
        mock_applescript.execute.return_value = (True, "")
        executor = UIScriptingExecutor()
        with patch.object(executor, "_ensure_notes_frontmost"):
            executor.open_note("Real Note")
        script = mock_applescript.execute.call_args[0][0]
        assert 'error "Note not found"' not in script


class TestWriteIndicatorBeforeActivation:
    """The write-guard overlay + lead countdown must appear BEFORE Notes is
    brought to the front, so the user is warned before the app switch and
    finishes typing in their own app rather than into Notes."""

    @pytest.fixture
    def order(self, monkeypatch):
        steps = []
        monkeypatch.setattr(
            "apple_notes.ui_scripting.show_pending", lambda: steps.append("show")
        )
        return steps

    @pytest.fixture
    def executor(self, order):
        with (
            patch("apple_notes.ui_scripting.AppleScriptExecutor") as as_class,
            patch("apple_notes.ui_scripting.ClipboardManager"),
        ):
            applescript = MagicMock()
            applescript.execute.side_effect = lambda script, **k: (
                order.append("activate" if "activate" in script else "script")
                or (True, "")
            )
            as_class.return_value = applescript
            ex = UIScriptingExecutor()
            ex._ensure_notes_frontmost = lambda *a, **k: order.append("frontmost")
            yield ex

    def test_open_note_shows_overlay_before_activating(self, executor, order):
        executor.open_note("Daily")
        assert order == ["show", "activate", "frontmost"]

    def test_activate_notes_shows_overlay_before_activating(self, executor, order):
        executor._activate_notes()
        assert order == ["show", "activate", "frontmost"]


class TestSectionKeys:
    """send_section_keys (issue #184): allowlisted keys, per-key guards."""

    SELECT_ALL = (0, "command down")
    COPY = (8, "command down")
    LEFT = (123, None)

    @pytest.fixture
    def executor(self):
        from apple_notes.ui_scripting import UIScriptingExecutor

        with (
            patch("apple_notes.ui_scripting.AppleScriptExecutor") as mock_class,
            patch("apple_notes.ui_scripting.ClipboardManager"),
            patch("apple_notes.ui_scripting.show_pending"),
        ):
            inst = MagicMock()
            inst.execute.return_value = (True, "")
            mock_class.return_value = inst
            yield UIScriptingExecutor()

    def test_section_keys_identity_check_before_every_key(self, executor):
        executor.send_section_keys(
            [self.SELECT_ALL, self.COPY, self.LEFT], 42, what="keys"
        )
        script = executor._applescript.execute.call_args[0][0]
        assert script.count("ICNote/p42") == 3
        # header check + 2 rechecks
        assert script.count("NOTES_NOT_FRONTMOST: ") == 3
        lines = [ln.strip() for ln in script.splitlines()]
        kinds = []
        for ln in lines:
            if ln.startswith('tell application "Notes"'):
                kinds.append("id")
            elif ln.startswith("tell process"):
                kinds.append("key")
            elif ln.startswith("delay"):
                kinds.append("delay")
            elif ln.startswith("set frontApp"):
                kinds.append("front")
        assert kinds == [
            "front",
            "id",
            "key",
            "delay",
            "front",
            "id",
            "key",
            "delay",
            "front",
            "id",
            "key",
        ]
        assert "key code 0 using command down" in script
        assert "key code 8 using command down" in script
        assert "key code 123\n" in script + "\n"
        assert "delay 0.12" in script

    def test_section_keys_dialog_scan_before_every_key(self, executor):
        """The #199 dialog scan rides _frontmost_recheck, so every section key
        (the first via the _guarded_system_events header) sits behind it and
        the section path never runs with _dialog_expected set."""
        executor.send_section_keys([self.SELECT_ALL, self.COPY], 42, what="keys")
        script = executor._applescript.execute.call_args[0][0]
        assert script.count('error "NOTES_MODAL_OPEN: "') == 2
        lines = [ln.strip() for ln in script.splitlines()]
        keys = [i for i, ln in enumerate(lines) if ln.startswith("tell process")]
        for k in keys:
            last_scan = max(
                i
                for i, ln in enumerate(lines[:k])
                if 'error "NOTES_MODAL_OPEN: "' in ln
            )
            assert all("tell process" not in ln for ln in lines[last_scan:k])
        assert executor._dialog_expected is False
        executor.send_section_keys([self.COPY], 42, what="copy")  # one-key send
        one = executor._applescript.execute.call_args[0][0]
        assert (
            one.index("NOTES_MODAL_OPEN")
            < one.index("ICNote/p42")
            < one.index("key code 8")
        )

    def test_section_keys_rejects_non_allowlisted_key(self, executor):
        with pytest.raises(ValueError):
            executor.send_section_keys(
                [self.COPY, (125, "option down")], 42, what="nav"
            )
        executor._applescript.execute.assert_not_called()

    def test_section_keys_send_one_script(self, executor):
        executor.send_section_keys([self.SELECT_ALL, self.COPY], 42, what="keys")
        calls = executor._applescript.execute.call_args_list
        assert len(calls) == 1
        assert calls[0].kwargs["timeout"] == 10.0

    def test_note_changed_maps_to_wrong_note_open(self, executor):
        from apple_notes.exceptions import NotesNotFrontmostError, WrongNoteOpenError

        executor._applescript.execute.return_value = (
            False,
            "execution error: NOTE_CHANGED: x-coredata://A/ICNote/p7 (-2700)",
        )
        with pytest.raises(WrongNoteOpenError) as ei:
            executor.send_section_keys([self.COPY], 42, what="nav")
        assert isinstance(ei.value, NotesNotFrontmostError)
        assert "different note is open" in str(ei.value)
        assert "may already have been sent" in str(ei.value)
        assert "ICNote/p7" in str(ei.value)

    def test_identity_check_not_inside_tell_process(self, executor):
        script = executor._note_identity_check(42)
        assert "tell process" not in script
        assert (
            'tell application "Notes" to set selId to id of item 1 of (get selection)'
            in script
        )
        assert 'does not end with "/ICNote/p42"' in script
        executor.send_section_keys([self.COPY, self.COPY], 42, what="nav")
        full = executor._applescript.execute.call_args[0][0]
        assert full.count("end tell") == 1  # only the System Events block

    def test_identity_check_no_selection_maps_to_wrong_note_open(self, executor):
        from apple_notes.exceptions import WrongNoteOpenError

        executor.send_section_keys([self.COPY], 42, what="nav")
        script = executor._applescript.execute.call_args[0][0]
        assert "try\n" in script and "on error" in script
        assert 'error "NOTE_CHANGED: no selection"' in script
        executor._applescript.execute.return_value = (
            False,
            "execution error: NOTE_CHANGED: no selection (-2700)",
        )
        with pytest.raises(WrongNoteOpenError):
            executor.send_section_keys([self.COPY], 42, what="nav")


# --- Open dialog in Notes (issue #199) ------------------------------------
#
# A sheet or alert inside Notes passes the frontmost-process check, but the
# select-all + copy then goes to the dialog and the write fails with a
# misleading "note may be empty or inaccessible" error. The per-key re-check
# now also refuses while a dialog is open.

from apple_notes.exceptions import NotesModalOpenError  # noqa: E402
from apple_notes import ui_scripting as ui_scripting_module  # noqa: E402

MODAL_OUTPUT = "45:120: execution error: NOTES_MODAL_OPEN: Delete “Groceries”? (-2700)"


class TestModalDialogGuard:
    @pytest.fixture
    def mock_applescript(self):
        with patch("apple_notes.ui_scripting.AppleScriptExecutor") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            yield mock_instance

    @pytest.fixture(autouse=True)
    def mock_clipboard(self):
        with patch("apple_notes.ui_scripting.ClipboardManager") as mock_class:
            mock_class.return_value = MagicMock()
            yield

    def test_sentinel_raises_modal_open_naming_the_dialog(self):
        with pytest.raises(NotesModalOpenError) as exc_info:
            UIScriptingExecutor()._raise_keystroke_failure(
                MODAL_OUTPUT, "send keystroke"
            )
        msg = str(exc_info.value)
        assert "dialog is open in Notes" in msg
        assert "Delete “Groceries”?" in msg
        assert "(-2700)" not in msg
        assert "close it and retry" in msg

    def test_title_with_parentheses_survives(self):
        with pytest.raises(NotesModalOpenError) as exc_info:
            UIScriptingExecutor()._raise_keystroke_failure(
                "execution error: NOTES_MODAL_OPEN: Export (PDF) (-2700)", "copy"
            )
        assert "Export (PDF)" in str(exc_info.value)

    def test_untitled_dialog_still_classifies(self):
        with pytest.raises(NotesModalOpenError) as exc_info:
            UIScriptingExecutor()._raise_keystroke_failure(
                "execution error: NOTES_MODAL_OPEN:  (-2700)", "copy"
            )
        assert "dialog is open in Notes" in str(exc_info.value)
        assert '("")' not in str(exc_info.value)

    def test_message_does_not_promise_the_note_is_untouched(self):
        """The check may be a mid-script re-check after earlier keys fired."""
        with pytest.raises(NotesModalOpenError) as exc_info:
            UIScriptingExecutor()._raise_keystroke_failure(MODAL_OUTPUT, "paste")
        assert "may already have modified the note" in str(exc_info.value)

    def test_keystroke_into_an_open_dialog_is_refused(self, mock_applescript):
        mock_applescript.execute.return_value = (False, MODAL_OUTPUT)
        with pytest.raises(NotesModalOpenError):
            UIScriptingExecutor()._send_keystroke("a")

    def test_every_frontmost_recheck_also_checks_for_a_dialog(self, mock_applescript):
        """Same granularity as the #90 guard: once before every key."""
        mock_applescript.execute.return_value = (True, "")
        with patch("apple_notes.ui_scripting.time.sleep"):
            UIScriptingExecutor()._focus_note_content()
        script = mock_applescript.execute.call_args[0][0]
        assert script.count("NOTES_MODAL_OPEN") == 3
        assert script.count("NOTES_MODAL_OPEN") == script.count("NOTES_NOT_FRONTMOST")

    def test_recheck_looks_for_sheets_and_dialog_windows_after_frontmost(self):
        recheck = UIScriptingExecutor()._frontmost_recheck()
        assert recheck.index("NOTES_NOT_FRONTMOST") < recheck.index("NOTES_MODAL_OPEN")
        # Only the front window takes the keys: a sheet on a background
        # window must not block writes (review F2).
        assert 'window 1 of process "Notes"' in recheck
        assert "windows of process" not in recheck
        assert "exists sheet 1" in recheck
        assert '"AXDialog"' in recheck and '"AXSystemDialog"' in recheck
        # Emitted at the System Events level (see _frontmost_recheck): a
        # `tell process` block would break the frontmost lookup after it.
        assert "tell process" not in recheck


class TestModalProbe:
    """Read-only probe the remote drain worker runs before each apply."""

    @pytest.fixture
    def mock_applescript(self):
        with patch("apple_notes.ui_scripting.AppleScriptExecutor") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            yield mock_instance

    def test_returns_the_dialog_title(self, mock_applescript):
        mock_applescript.execute.return_value = (
            True,
            "NOTES_MODAL_OPEN: Delete Note?\n",
        )
        assert ui_scripting_module.notes_modal_title() == "Delete Note?"

    def test_untitled_dialog_reports_a_placeholder(self, mock_applescript):
        mock_applescript.execute.return_value = (True, "NOTES_MODAL_OPEN:")
        assert ui_scripting_module.notes_modal_title() == "dialog"

    def test_no_dialog_is_none(self, mock_applescript):
        mock_applescript.execute.return_value = (True, "")
        assert ui_scripting_module.notes_modal_title() is None

    def test_probe_failure_fails_open(self, mock_applescript):
        mock_applescript.execute.return_value = (False, "not allowed assistive")
        assert ui_scripting_module.notes_modal_title() is None
        mock_applescript.execute.side_effect = RuntimeError("boom")
        assert ui_scripting_module.notes_modal_title() is None

    def test_probe_sends_no_keystrokes_and_shares_the_detection(self, mock_applescript):
        mock_applescript.execute.return_value = (True, "")
        ui_scripting_module.notes_modal_title()
        script = mock_applescript.execute.call_args[0][0]
        assert "keystroke" not in script and "key code" not in script
        assert "exists sheet 1" in script and '"AXDialog"' in script
        assert 'exists process "Notes"' in script


class TestModalGuardReviewFixes:
    """Round-1 review fixes for the #199 dialog guard."""

    @pytest.fixture
    def mock_applescript(self):
        with patch("apple_notes.ui_scripting.AppleScriptExecutor") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            yield mock_instance

    @pytest.fixture(autouse=True)
    def mock_clipboard(self):
        with patch("apple_notes.ui_scripting.ClipboardManager") as mock_class:
            mock_class.return_value = MagicMock()
            yield

    @staticmethod
    def _guarded_scripts(mock_applescript):
        scripts = [c[0][0] for c in mock_applescript.execute.call_args_list]
        return [s for s in scripts if "NOTES_NOT_FRONTMOST" in s]

    def test_popup_allowance_ends_when_the_tag_step_fails(self, mock_applescript):
        """The dialog-scan allowance for a write's own popup is reset even when
        a key inside it fails (the Cmd+K link flow is gone, #212; the #tag
        popup is the remaining user)."""
        calls = []

        def execute(script, **kwargs):
            calls.append(script)
            if "NOTES_NOT_FRONTMOST" in script and len(calls) > 2:
                return False, "execution error: NOTES_NOT_FRONTMOST: Ghostty (-2700)"
            return True, ""

        mock_applescript.execute.side_effect = execute
        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(NotesNotFrontmostError):
                executor._replace_with_hashtag("mcp212")
        assert "NOTES_MODAL_OPEN" in executor._frontmost_recheck()

    def test_probe_keeps_a_title_ending_in_a_number(self, mock_applescript):
        """F4: only osascript's error number is stripped, never title text."""
        mock_applescript.execute.return_value = (True, "NOTES_MODAL_OPEN: Untitled (2)")
        assert ui_scripting_module.notes_modal_title() == "Untitled (2)"

    def test_error_path_strips_only_the_error_number(self):
        with pytest.raises(NotesModalOpenError) as exc_info:
            UIScriptingExecutor()._raise_keystroke_failure(
                "execution error: NOTES_MODAL_OPEN: Untitled (2) (-2700)", "copy"
            )
        assert "Untitled (2)" in str(exc_info.value)

    def test_hashtag_confirm_keys_may_go_into_the_tag_suggestions(
        self, mock_applescript
    ):
        """Typing "#tag" opens Notes' tag-suggestion popup, a separate
        AXSystemDialog window (seen live): the Space that confirms the tag
        and the Backspace after it go to that popup on purpose. Keys up to
        and including the typed "#tag" still refuse an unrelated dialog."""
        mock_applescript.execute.return_value = (True, "")
        executor = UIScriptingExecutor()
        with (
            patch("apple_notes.ui_scripting.time.sleep"),
            # The fixture mocks the whole executor class; keep the typed text.
            patch(
                "apple_notes.ui_scripting.AppleScriptExecutor._escape_applescript_string",
                side_effect=lambda text: text,
            ),
        ):
            executor._replace_with_hashtag("mcp199")
        guarded = self._guarded_scripts(mock_applescript)
        typed = next(i for i, s in enumerate(guarded) if "#mcp199" in s)
        assert typed + 1 < len(guarded)
        assert all("NOTES_MODAL_OPEN" in s for s in guarded[: typed + 1])
        assert all("NOTES_MODAL_OPEN" not in s for s in guarded[typed + 1 :])
        assert "NOTES_MODAL_OPEN" in executor._frontmost_recheck()
