"""Tests for UIScriptingExecutor.

Fully mocked (AppleScriptExecutor, ClipboardManager), so CI runs this module:
it used to be marked macos_gui and merge gating never saw it (issue #143).
A test that needs a live GUI session belongs in a macos_gui-marked module;
tests/test_ci_markers.py keeps this one unmarked.
"""

from unittest.mock import MagicMock, patch, call
import pytest

from apple_notes import profiling
from apple_notes.ui_scripting import UIScriptingExecutor
from apple_notes.richtext.markdown_converter import PendingKeyboardAction
from apple_notes.exceptions import (
    UIScriptingError,
    AccessibilityPermissionError,
    NoteNotFoundError,
    NotesNotFrontmostError,
    NotesNotRespondingError,
    AppleScriptError,
)

# Real osascript output shapes (issue #133). Both carry -1719, so the code
# alone cannot distinguish them — see tests/test_ui_scripting_errors.py.
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

# What AppleScriptExecutor.execute returns when osascript blows its budget
# (issue #141) — the stall used to surface as a bogus "not frontmost" abort.
TIMEOUT_OUTPUT = "AppleScript execution timed out after 10s"


def _script_steps(script):
    """Reduce an AppleScript to its ordered guard/delay/keystroke steps.

    Lets the batched-script tests assert that a frontmost re-check sits
    between every inline delay and the keystroke that follows it, rather than
    just counting occurrences (issue #141).
    """
    steps = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("set frontApp to name of first process"):
            steps.append("check")
        elif stripped.startswith("delay "):
            steps.append(f"delay {stripped.split()[1]}")
        elif "keystroke" in stripped:
            steps.append(stripped.split("keystroke", 1)[1].strip())
    return steps


class TestUIScriptingExecutor:
    """Tests for UIScriptingExecutor class."""

    @pytest.fixture
    def mock_applescript(self):
        """Create a mock AppleScriptExecutor."""
        with patch("apple_notes.ui_scripting.AppleScriptExecutor") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            mock_class._escape_applescript_string = lambda s: s.replace(
                '"', '\\"'
            ).replace("\\", "\\\\")
            yield mock_instance

    @pytest.fixture
    def mock_clipboard(self):
        """Create a mock ClipboardManager."""
        with patch("apple_notes.ui_scripting.ClipboardManager") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            yield mock_instance

    @pytest.fixture(autouse=True)
    def _bypass_frontmost_guard(self):
        """Neutralize the Layer-A focus settle-poll for workflow tests.

        Wiring `_ensure_notes_frontmost` into `_activate_notes`/`open_note`
        adds frontmost-poll osascript calls to every workflow. The guard has
        dedicated tests (TestEnsureNotesFrontmost, TestActivationFocusGuard);
        other tests exercise the keystroke/clipboard workflow and shouldn't
        each enumerate the poll — mirroring how conftest neutralizes the
        session-lock preflight. Classes that test the guard override this with
        a plain `yield`.
        """
        with patch.object(
            UIScriptingExecutor, "_ensure_notes_frontmost", return_value=None
        ):
            yield


class TestEnsureNotesFrontmost(TestUIScriptingExecutor):
    """Focus guard (Layer A): activate-and-verify settle poll.

    Keystrokes go to whatever app is frontmost, so before any UI scripting we
    must confirm Apple Notes actually came to the front and abort otherwise —
    never type into another app (issue #90)."""

    @pytest.fixture(autouse=True)
    def _bypass_frontmost_guard(self):
        # This class tests the real _ensure_notes_frontmost — do not bypass.
        yield

    def test_reads_frontmost_via_system_events(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "Notes")

        executor = UIScriptingExecutor()
        assert executor._frontmost_process_name() == "Notes"

        script = mock_applescript.execute.call_args[0][0]
        assert "System Events" in script
        assert "frontmost" in script

    def test_frontmost_raises_accessibility_error(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, DENIAL_ASSISTIVE)

        executor = UIScriptingExecutor()
        with pytest.raises(AccessibilityPermissionError):
            executor._frontmost_process_name()

    def test_returns_when_notes_already_frontmost(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        mock_applescript.execute.return_value = (True, "Notes")

        executor = UIScriptingExecutor()
        assert executor._ensure_notes_frontmost() is None

    def test_succeeds_after_notes_comes_forward(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        # Notes not frontmost on the first poll, then activates.
        mock_applescript.execute.side_effect = [
            (True, "Dock"),
            (True, "Notes"),
        ]

        executor = UIScriptingExecutor()
        assert executor._ensure_notes_frontmost() is None

    def test_raises_when_notes_never_frontmost(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        mock_applescript.execute.return_value = (True, "Ghostty")

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError, match="Ghostty"):
            executor._ensure_notes_frontmost(timeout=0.3)

        # Deadline semantics, not attempt count: the poll gives up as soon as
        # the wall clock passes the budget it was handed.
        assert frontmost_clock.now == pytest.approx(0.3)

    def test_poll_is_bounded_by_the_deadline_not_an_attempt_count(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        """Issue #141: every probe is its own osascript subprocess, and under
        load one takes ~3s. The old fixed 20-attempt loop therefore blocked
        ~67s before failing with a nominally "2 second" guard. A wall-clock
        deadline must cut the loop off after the second slow probe."""

        def slow_probe(script, timeout=None):
            frontmost_clock.advance(3.0)
            return (True, "Ghostty")

        mock_applescript.execute.side_effect = slow_probe

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError, match="Ghostty"):
            executor._ensure_notes_frontmost()

        assert mock_applescript.execute.call_count == 2

    def test_probes_once_even_when_the_deadline_has_already_passed(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        """A zero budget still gets one probe — a caller must never be told
        Notes is not frontmost without having actually looked."""
        mock_applescript.execute.return_value = (True, "Notes")

        executor = UIScriptingExecutor()
        assert executor._ensure_notes_frontmost(timeout=0.0) is None
        assert mock_applescript.execute.call_count == 1

    def test_sleep_never_overshoots_the_deadline(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        """The wait between probes is clamped to the time left, so the guard
        cannot block past the budget it advertises."""
        mock_applescript.execute.return_value = (True, "Ghostty")

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError):
            executor._ensure_notes_frontmost(timeout=0.25)

        assert frontmost_clock.sleeps == pytest.approx([0.1, 0.1, 0.05])
        assert frontmost_clock.now == pytest.approx(0.25)

    def test_named_app_failure_message_reports_elapsed_seconds(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        """The stall was invisible in the old message: "not frontmost" said
        nothing about the minute spent waiting first (issue #141)."""
        mock_applescript.execute.return_value = (True, "Ghostty")

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError, match=r"not frontmost after 5\.0s"):
            executor._ensure_notes_frontmost()

    def test_no_frontmost_failure_message_reports_elapsed_seconds(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError, match=r"after 0\.3s"):
            executor._ensure_notes_frontmost(timeout=0.3)

    def test_poll_budget_constants(self):
        """Pins the wall-clock budget the design settled on (issue #141)."""
        assert UIScriptingExecutor.FRONTMOST_POLL_TIMEOUT == 5.0
        assert UIScriptingExecutor.FRONTMOST_POLL_INTERVAL == 0.1

    def test_no_frontmost_match_is_not_a_permissions_error(
        self, mock_applescript, mock_clipboard
    ):
        """Issue #133: an empty `first process whose frontmost` filter yields
        -1719 (errAEIllegalIndex), the same code as an assistive-access
        denial. It must NOT be reported as a permissions problem."""
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)

        executor = UIScriptingExecutor()
        assert executor._frontmost_process_name() == ""

    def test_transient_no_frontmost_is_retried_by_the_poll(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        """The settle-poll must ride out a momentary no-frontmost race."""
        mock_applescript.execute.side_effect = [
            (False, NO_FRONTMOST_OUTPUT),
            (True, "Notes"),
        ]

        executor = UIScriptingExecutor()
        assert executor._ensure_notes_frontmost() is None

    def test_persistent_no_frontmost_reports_retryable_not_system_settings(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError) as exc_info:
            executor._ensure_notes_frontmost(timeout=0.3)

        message = str(exc_info.value)
        assert "no application reported as frontmost" in message
        assert "retry" in message.lower()

    def test_frontmost_raises_accessibility_error_on_real_denial(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, DENIAL_ASSISTIVE)

        executor = UIScriptingExecutor()
        with pytest.raises(AccessibilityPermissionError):
            executor._frontmost_process_name()

    def test_frontmost_generic_failure_is_ui_scripting_error(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (
            False,
            "execution error: The operation is not allowed. (-5000)",
        )

        executor = UIScriptingExecutor()
        with pytest.raises(UIScriptingError, match="Failed to read frontmost app"):
            executor._frontmost_process_name()


class TestKeystrokeFailureClassification(TestUIScriptingExecutor):
    """Issue #133: -1719 is overloaded — assistive-access denial AND
    errAEIllegalIndex from a no-match `first process whose ...` filter.
    Classification keys on message text, never on the bare code."""

    def test_no_frontmost_match_is_retryable_not_permissions(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError) as exc_info:
            executor._send_keystroke("a")

        message = str(exc_info.value)
        assert "may already have modified the note" in message
        assert "Nothing was typed" not in message

    def test_no_frontmost_message_does_not_promise_this_step_typed_nothing(
        self, mock_applescript, mock_clipboard
    ):
        """A mid-script `_frontmost_recheck` abort happens AFTER earlier keys
        in the same batched script already fired (issue #141), so the message
        must not claim this step sent no keystrokes."""
        mock_applescript.execute.return_value = (False, NO_FRONTMOST_OUTPUT)

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(NotesNotFrontmostError) as exc_info:
                executor.select_all_and_paste()

        message = str(exc_info.value)
        assert "sent no keystrokes" not in message
        assert "aborted at a frontmost check" in message

    def test_real_assistive_denial_still_raises_accessibility_error(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, DENIAL_ASSISTIVE)

        executor = UIScriptingExecutor()
        with pytest.raises(AccessibilityPermissionError):
            executor._send_keystroke("a")

    def test_send_keystrokes_denial_still_raises_accessibility_error(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, DENIAL_KEYSTROKES)

        executor = UIScriptingExecutor()
        with pytest.raises(AccessibilityPermissionError):
            executor._send_keystroke("a")

    def test_generic_not_allowed_is_not_a_permissions_error(
        self, mock_applescript, mock_clipboard
    ):
        """The old bare `"not allowed" in output` match was too broad."""
        mock_applescript.execute.return_value = (
            False,
            "execution error: Modifying a locked file is not allowed. (-5000)",
        )

        executor = UIScriptingExecutor()
        with pytest.raises(UIScriptingError, match="Failed to send keystroke"):
            executor._send_keystroke("a")

    def test_bare_error_code_is_not_a_permissions_error(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, "error -1719")

        executor = UIScriptingExecutor()
        with pytest.raises(UIScriptingError, match="Failed to send keystroke"):
            executor._send_keystroke("a")

    def test_frontmost_sentinel_still_wins_over_index_classification(
        self, mock_applescript, mock_clipboard
    ):
        """The NOTES_NOT_FRONTMOST branch must keep naming the offending app."""
        mock_applescript.execute.return_value = (
            False,
            "execution error: NOTES_NOT_FRONTMOST: Ghostty (1)",
        )

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError, match="Ghostty"):
            executor._send_keystroke("v")


class TestSystemEventsTimeout(TestUIScriptingExecutor):
    """Issue #141: System Events calls normally answer in <1s. A minute-long
    stall used to burn the executor's 30s default and then be reported as a
    focus problem; it is now bounded at 10s and named for what it is."""

    def test_frontmost_probe_timeout_raises_not_responding(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, TIMEOUT_OUTPUT)

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotRespondingError, match=r"didn't respond in 10s"):
            executor._frontmost_process_name()

    def test_probe_passes_system_events_timeout(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "Notes")

        executor = UIScriptingExecutor()
        executor._frontmost_process_name()

        assert mock_applescript.execute.call_args.kwargs["timeout"] == 10.0

    def test_guarded_script_passes_system_events_timeout(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._guarded_system_events('tell process "Notes" to keystroke tab')

        assert mock_applescript.execute.call_args.kwargs["timeout"] == 10.0

    def test_keystroke_timeout_raises_not_responding(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, TIMEOUT_OUTPUT)

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotRespondingError, match=r"didn't respond in 10s"):
            executor._send_keystroke("a")

    def test_other_applescript_calls_keep_the_default_budget(
        self, mock_applescript, mock_clipboard
    ):
        """The tight budget is scoped to System Events, not to data reads —
        a `whose name = ...` note lookup is legitimately slower."""
        mock_applescript.execute.return_value = (True, "true")

        executor = UIScriptingExecutor()
        executor._note_exists("Test Note")

        assert "timeout" not in mock_applescript.execute.call_args.kwargs

    def test_focus_script_gets_a_budget_scaled_to_its_events(
        self, mock_applescript, mock_clipboard
    ):
        """The 10s cap is per System Events event, not per script.

        The focus script carries three keystrokes, three frontmost re-checks
        and two inline delays; a flat 10s cap could kill it partway through,
        which the pre-batch per-key code (30s per key) never did.
        """
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor._focus_note_content()

        assert mock_applescript.execute.call_args.kwargs["timeout"] == 30.0

    def test_pair_scripts_get_a_budget_scaled_to_their_events(
        self, mock_applescript, mock_clipboard
    ):
        """Never kill a select-all+copy/paste script between its two keys."""
        mock_applescript.execute.return_value = (True, "")
        executor = UIScriptingExecutor()

        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.select_all_and_copy()
        assert mock_applescript.execute.call_args.kwargs["timeout"] == 20.0

        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.select_all_and_paste()
        assert mock_applescript.execute.call_args.kwargs["timeout"] == 20.0

    def test_type_text_keeps_the_length_scaled_budget(
        self, mock_applescript, mock_clipboard
    ):
        """A keystroke script's duration scales with the caller's text — it
        keeps the pre-branch 30s budget rather than the per-event cap."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._type_text("some placeholder text")

        assert mock_applescript.execute.call_args.kwargs["timeout"] == 30.0

    def test_paste_text_keeps_the_length_scaled_budget(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._paste_text("some placeholder text")

        assert mock_applescript.execute.call_args.kwargs["timeout"] == 30.0

    def test_timeout_message_names_the_scripts_actual_budget(
        self, mock_applescript, mock_clipboard
    ):
        """A 20s-budget script that stalls must not report "10s"."""
        mock_applescript.execute.return_value = (
            False,
            "AppleScript execution timed out after 20s",
        )

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(NotesNotRespondingError) as exc_info:
                executor.select_all_and_paste()

        assert "didn't respond in 20s" in str(exc_info.value)


class TestDeferredWriteIndicator(TestUIScriptingExecutor):
    """Issue #141: the guarded-keystroke choke point also spawns the write-guard
    overlay (idempotent fallback; it is normally already up from before Notes
    was activated - see test_ui_scripting_errors.py)."""

    def test_guarded_system_events_triggers_show_pending(
        self, mock_applescript, mock_clipboard, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            "apple_notes.ui_scripting.show_pending", lambda: calls.append(1)
        )
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._guarded_system_events('tell process "Notes" to keystroke tab')

        assert calls == [1]

    def test_show_pending_runs_before_the_script(
        self, mock_applescript, mock_clipboard, monkeypatch
    ):
        """The overlay must be up before the keystroke fires, not after."""
        order = []
        monkeypatch.setattr(
            "apple_notes.ui_scripting.show_pending", lambda: order.append("show")
        )
        mock_applescript.execute.side_effect = lambda *a, **k: (
            order.append("execute") or (True, "")
        )

        executor = UIScriptingExecutor()
        executor._guarded_system_events('tell process "Notes" to keystroke tab')

        assert order == ["show", "execute"]


class TestActivationFocusGuard(TestUIScriptingExecutor):
    """Layer A wiring: every activation entry point must run the focus guard
    after `activate`, and abort the whole operation if Notes never fronts."""

    @pytest.fixture(autouse=True)
    def _bypass_frontmost_guard(self):
        # Exercise the real wiring; tests patch _ensure_notes_frontmost as needed.
        yield

    def test_activate_notes_verifies_frontmost(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "")
        executor = UIScriptingExecutor()
        with patch.object(executor, "_ensure_notes_frontmost") as guard:
            executor._activate_notes()
        guard.assert_called_once()

    def test_activate_notes_propagates_guard_error(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (True, "")
        executor = UIScriptingExecutor()
        with patch.object(
            executor,
            "_ensure_notes_frontmost",
            side_effect=NotesNotFrontmostError("frontmost app: 'Ghostty'"),
        ):
            with pytest.raises(NotesNotFrontmostError, match="Ghostty"):
                executor._activate_notes()

    def test_open_note_verifies_frontmost(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "")
        executor = UIScriptingExecutor()
        with patch.object(executor, "_ensure_notes_frontmost") as guard:
            executor.open_note("Test Note")
        guard.assert_called_once()

    def test_open_note_propagates_guard_error(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "")
        executor = UIScriptingExecutor()
        with patch.object(
            executor,
            "_ensure_notes_frontmost",
            side_effect=NotesNotFrontmostError("frontmost app: 'Ghostty'"),
        ):
            with pytest.raises(NotesNotFrontmostError, match="Ghostty"):
                executor.open_note("Test Note")

    def test_create_aborts_without_keystrokes_when_not_frontmost(
        self, mock_applescript, mock_clipboard, frontmost_clock
    ):
        """The actual #90 bug: if Notes never fronts, no keystrokes/paste fire —
        nothing is typed into the (non-Notes) frontmost app."""
        mock_applescript.execute.return_value = (True, "Ghostty")
        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError):
            executor.create_note_with_richtext("Note", b"rt", "Notes")

        mock_clipboard.write_plain_text.assert_not_called()
        mock_clipboard.write_notes_richtext.assert_not_called()

    def test_focus_note_content_aborts_when_not_frontmost(
        self, mock_applescript, mock_clipboard
    ):
        """Inline System Events sites are guarded too (mid-sequence theft)."""
        mock_applescript.execute.return_value = (
            False,
            "execution error: NOTES_NOT_FRONTMOST: Finder (1)",
        )
        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(NotesNotFrontmostError, match="Finder"):
                executor._focus_note_content()


class TestNoteBodyHtml(TestUIScriptingExecutor):
    """Tests for _note_body_html (formatting-aware read-back used by
    verify-after-write). Body HTML (not plaintext) is the change signal so a
    formatting-only edit — e.g. bolding a word — is not mistaken for a no-op."""

    def test_returns_body_html(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "<div>body <b>text</b></div>")

        executor = UIScriptingExecutor()
        assert executor._note_body_html("My Note") == "<div>body <b>text</b></div>"

        script = mock_applescript.execute.call_args[0][0]
        assert "body of" in script
        assert 'name = "My Note"' in script

    def test_raises_not_found(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (False, "Note not found")

        executor = UIScriptingExecutor()
        with pytest.raises(NoteNotFoundError, match="My Note"):
            executor._note_body_html("My Note")


class TestCheckAccessibilityPermissions(TestUIScriptingExecutor):
    """Tests for check_accessibility_permissions method."""

    def test_returns_true_when_granted(self, mock_applescript, mock_clipboard):
        """Test returns True when permissions are granted."""
        mock_applescript.execute.return_value = (True, "true")

        executor = UIScriptingExecutor()
        result = executor.check_accessibility_permissions()

        assert result is True

    def test_returns_false_when_denied(self, mock_applescript, mock_clipboard):
        """Test returns False when permissions are denied."""
        mock_applescript.execute.return_value = (True, "false")

        executor = UIScriptingExecutor()
        result = executor.check_accessibility_permissions()

        assert result is False

    def test_returns_false_on_error(self, mock_applescript, mock_clipboard):
        """Test returns False when AppleScript fails."""
        mock_applescript.execute.return_value = (False, "error")

        executor = UIScriptingExecutor()
        result = executor.check_accessibility_permissions()

        assert result is False


class TestOpenNote(TestUIScriptingExecutor):
    """Tests for open_note method."""

    def test_opens_note_successfully(self, mock_applescript, mock_clipboard):
        """Test opens note when it exists."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor.open_note("Test Note")

        mock_applescript.execute.assert_called_once()
        call_args = mock_applescript.execute.call_args[0][0]
        assert 'show note "Test Note"' in call_args
        assert "activate" in call_args

    def test_raises_not_found_error(self, mock_applescript, mock_clipboard):
        """Test raises NoteNotFoundError when note doesn't exist."""
        mock_applescript.execute.return_value = (False, "Note not found")

        executor = UIScriptingExecutor()
        with pytest.raises(NoteNotFoundError, match="Test Note"):
            executor.open_note("Test Note")

    def test_raises_applescript_error_on_other_failure(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises AppleScriptError on other failures."""
        mock_applescript.execute.return_value = (False, "Some other error")

        executor = UIScriptingExecutor()
        with pytest.raises(AppleScriptError, match="Failed to open note"):
            executor.open_note("Test Note")


class TestSendKeystroke(TestUIScriptingExecutor):
    """Tests for _send_keystroke method."""

    def test_sends_keystroke_successfully(self, mock_applescript, mock_clipboard):
        """Test sends keystroke via System Events."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._send_keystroke("a")

        call_args = mock_applescript.execute.call_args[0][0]
        assert 'keystroke "a" using command down' in call_args

    def test_raises_accessibility_error_on_permission_denied(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises AccessibilityPermissionError on an assistive-access denial."""
        mock_applescript.execute.return_value = (False, DENIAL_ASSISTIVE)

        executor = UIScriptingExecutor()
        with pytest.raises(AccessibilityPermissionError):
            executor._send_keystroke("a")

    def test_raises_accessibility_error_on_keystroke_denial(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises AccessibilityPermissionError on a send-keystrokes denial."""
        mock_applescript.execute.return_value = (False, DENIAL_KEYSTROKES)

        executor = UIScriptingExecutor()
        with pytest.raises(AccessibilityPermissionError):
            executor._send_keystroke("a")

    def test_raises_ui_scripting_error_on_other_failure(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises UIScriptingError on other failures."""
        mock_applescript.execute.return_value = (False, "Unknown error")

        executor = UIScriptingExecutor()
        with pytest.raises(UIScriptingError, match="Failed to send keystroke"):
            executor._send_keystroke("a")

    def test_script_embeds_frontmost_check(self, mock_applescript, mock_clipboard):
        """Layer B: the keystroke script itself guards on frontmost."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._send_keystroke("a")

        script = mock_applescript.execute.call_args[0][0]
        assert "frontmost" in script
        assert "NOTES_NOT_FRONTMOST" in script

    def test_aborts_when_not_frontmost(self, mock_applescript, mock_clipboard):
        """Layer B: the embedded check errors with the sentinel → raise."""
        mock_applescript.execute.return_value = (
            False,
            "execution error: NOTES_NOT_FRONTMOST: Ghostty (1)",
        )

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError, match="Ghostty"):
            executor._send_keystroke("v")


class TestPasteFromClipboard(TestUIScriptingExecutor):
    """Tests for paste_from_clipboard method."""

    def test_sends_cmd_v(self, mock_applescript, mock_clipboard):
        """Test sends Cmd+V keystroke."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.paste_from_clipboard()

        call_args = mock_applescript.execute.call_args[0][0]
        assert 'keystroke "v"' in call_args


class TestBatchedKeystrokes(TestUIScriptingExecutor):
    """Batched keystroke scripts (issue #141).

    Each keystroke used to be its own osascript subprocess. The focus
    sequence, select-all+copy, and select-all+paste now ride one guarded
    script each, with the inter-key delays inside the AppleScript.
    """

    def test_focus_note_content_is_one_script(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor._focus_note_content()

        assert mock_applescript.execute.call_count == 1
        script = mock_applescript.execute.call_args[0][0]
        assert 'keystroke "f" using {option down, command down}' in script
        assert script.count("keystroke tab") == 2
        assert script.count("delay 0.3") == 2
        assert "NOTES_NOT_FRONTMOST" in script  # still guarded

    def test_select_all_and_copy_is_one_script(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.select_all_and_copy()

        assert mock_applescript.execute.call_count == 1
        script = mock_applescript.execute.call_args[0][0]
        assert 'keystroke "a" using command down' in script
        assert 'keystroke "c" using command down' in script
        assert "delay 0.3" in script
        assert "NOTES_NOT_FRONTMOST" in script

    def test_select_all_and_paste_is_one_script(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.select_all_and_paste()

        assert mock_applescript.execute.call_count == 1
        script = mock_applescript.execute.call_args[0][0]
        assert 'keystroke "a" using command down' in script
        assert 'keystroke "v" using command down' in script
        assert "delay 0.3" in script
        assert "NOTES_NOT_FRONTMOST" in script

    def test_focus_rechecks_frontmost_before_every_key(
        self, mock_applescript, mock_clipboard
    ):
        """Batching must not cost the #90 guard its per-keystroke granularity.

        Each key gets its own frontmost check, so focus stolen during an
        inline delay aborts instead of sending the remaining keys to whatever
        app took the front.
        """
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor._focus_note_content()

        script = mock_applescript.execute.call_args[0][0]
        assert _script_steps(script) == [
            "check",
            '"f" using {option down, command down}',
            "delay 0.3",
            "check",
            "tab",
            "delay 0.3",
            "check",
            "tab",
        ]
        assert script.count("NOTES_NOT_FRONTMOST") == 3

    def test_select_all_and_copy_rechecks_frontmost_before_the_copy(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.select_all_and_copy()

        script = mock_applescript.execute.call_args[0][0]
        assert _script_steps(script) == [
            "check",
            '"a" using command down',
            "delay 0.3",
            "check",
            '"c" using command down',
        ]
        assert script.count("NOTES_NOT_FRONTMOST") == 2

    def test_select_all_and_paste_rechecks_frontmost_before_the_paste(
        self, mock_applescript, mock_clipboard
    ):
        """The highest-stakes re-check: a stolen Cmd+V pastes note content
        into another app."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.select_all_and_paste()

        script = mock_applescript.execute.call_args[0][0]
        assert _script_steps(script) == [
            "check",
            '"a" using command down',
            "delay 0.3",
            "check",
            '"v" using command down',
        ]
        assert script.count("NOTES_NOT_FRONTMOST") == 2

    def test_batched_failure_maps_to_keystroke_failure(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (
            False,
            "execution error: NOTES_NOT_FRONTMOST: ghostty",
        )

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(NotesNotFrontmostError, match="ghostty"):
                executor.select_all_and_copy()

    def test_batched_paste_failure_maps_to_keystroke_failure(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (False, DENIAL_KEYSTROKES)

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(AccessibilityPermissionError):
                executor.select_all_and_paste()

    def test_batched_timeout_maps_to_not_responding(
        self, mock_applescript, mock_clipboard
    ):
        """A stalled batched script names the step it was on (issue #141)."""
        mock_applescript.execute.return_value = (False, TIMEOUT_OUTPUT)

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(NotesNotRespondingError, match="select all and copy"):
                executor.select_all_and_copy()

    def test_trailing_sleeps_use_the_class_constants(
        self, mock_applescript, mock_clipboard
    ):
        """copy settles for KEYSTROKE_DELAY, paste for the longer PASTE_DELAY."""
        mock_applescript.execute.return_value = (True, "")
        executor = UIScriptingExecutor()

        with patch("apple_notes.ui_scripting.time.sleep") as mock_sleep:
            executor.select_all_and_copy()
        assert mock_sleep.call_args_list == [call(executor.KEYSTROKE_DELAY)]

        with patch("apple_notes.ui_scripting.time.sleep") as mock_sleep:
            executor.select_all_and_paste()
        assert mock_sleep.call_args_list == [call(executor.PASTE_DELAY)]

    def test_read_notes_richtext_uses_two_scripts_for_focus_and_copy(
        self, mock_applescript, mock_clipboard
    ):
        """Read path: open_note + focus + select-all-and-copy — no per-key calls."""
        mock_applescript.execute.return_value = (True, "")
        mock_clipboard.read_notes_richtext.return_value = b"data"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            assert executor.read_note_as_notes_richtext("Test Note") == b"data"

        scripts = [c[0][0] for c in mock_applescript.execute.call_args_list]
        # open_note, then exactly two keystroke scripts
        keystroke_scripts = [s for s in scripts if "keystroke" in s]
        assert len(keystroke_scripts) == 2
        assert "keystroke tab" in keystroke_scripts[0]
        assert 'keystroke "c" using command down' in keystroke_scripts[1]

    def test_find_replace_pastes_with_one_batched_script(
        self, mock_applescript, mock_clipboard
    ):
        """Write-back leg of find_replace is a single select-all+paste script."""
        mock_applescript.execute.return_value = (True, "true")
        mock_clipboard.read_notes_richtext.return_value = b"notes richtext data"
        mock_clipboard.read_rtf.return_value = b"{\\rtf1 original}"
        mock_clipboard.find_replace_in_notes_richtext_variable.return_value = (
            b"modified data",
            1,
            [],
        )
        mock_clipboard.find_replace_in_raw_rtf.return_value = (b"{\\rtf1 modified}", 1)

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            assert executor.find_replace_in_note("Test Note", "old", "new") == 1

        scripts = [c[0][0] for c in mock_applescript.execute.call_args_list]
        paste_scripts = [s for s in scripts if 'keystroke "v"' in s]
        assert len(paste_scripts) == 1
        assert 'keystroke "a" using command down' in paste_scripts[0]

    def test_transform_full_note_richtext_pastes_with_one_batched_script(
        self, mock_applescript, mock_clipboard
    ):
        """Its write-back leg was still two per-key scripts (issue #141)."""
        mock_applescript.execute.return_value = (True, "")
        mock_clipboard.read_notes_richtext.return_value = b"template"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.transform_full_note_richtext("Test Note", lambda d: b"new")

        scripts = [c[0][0] for c in mock_applescript.execute.call_args_list]
        paste_scripts = [s for s in scripts if 'keystroke "v"' in s]
        assert len(paste_scripts) == 1
        assert _script_steps(paste_scripts[0]) == [
            "check",
            '"a" using command down',
            "delay 0.3",
            "check",
            '"v" using command down',
        ]

    def test_replace_note_richtext_reads_with_one_batched_script(
        self, mock_applescript, mock_clipboard
    ):
        """Its read leg was still a per-key Cmd+A then Cmd+C (issue #141).

        Only the read leg batches: the write leg selects the *body* via key
        codes (Cmd+Up, Down, Cmd+Shift+Down) so the title survives — it is not
        a select-all+paste and must not become one.
        """
        mock_applescript.execute.return_value = (True, "")
        mock_clipboard.read_notes_richtext.return_value = b"template"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.replace_note_richtext("Test Note", lambda d: b"new")

        scripts = [c[0][0] for c in mock_applescript.execute.call_args_list]
        copy_scripts = [s for s in scripts if 'keystroke "c"' in s]
        assert len(copy_scripts) == 1
        assert _script_steps(copy_scripts[0]) == [
            "check",
            '"a" using command down',
            "delay 0.3",
            "check",
            '"c" using command down',
        ]
        # The body-preserving write leg is untouched.
        assert any("key code 126 using command down" in s for s in scripts)

    def test_read_note_as_rtf_uses_focus_then_batched_copy(
        self, mock_applescript, mock_clipboard
    ):
        """Mirrors the migration already done in read_note_as_notes_richtext."""
        mock_applescript.execute.return_value = (True, "")
        mock_clipboard.read_rtf.return_value = b"{\\rtf1 content}"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            assert executor.read_note_as_rtf("Test Note") == b"{\\rtf1 content}"

        scripts = [c[0][0] for c in mock_applescript.execute.call_args_list]
        keystroke_scripts = [s for s in scripts if "keystroke" in s]
        assert len(keystroke_scripts) == 2
        assert "keystroke tab" in keystroke_scripts[0]
        assert 'keystroke "c" using command down' in keystroke_scripts[1]
        assert 'keystroke "a" using command down' in keystroke_scripts[1]


class TestProfilingSpans(TestUIScriptingExecutor):
    """Per-phase spans inside the write path's `execute` phase (issue #141).

    A slow write used to surface as one opaque `execute=NNNNms`. These spans
    split it into open/focus/copy/paste so timing.log shows where the time
    actually went.
    """

    def test_open_note_records_open_span(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with profiling.profile("test") as prof:
            executor.open_note("Some Note")

        assert "open" in prof.timing_ms

    def test_activate_notes_records_open_span(self, mock_applescript, mock_clipboard):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with profiling.profile("test") as prof:
            executor._activate_notes()

        assert "open" in prof.timing_ms

    def test_focus_and_copy_and_paste_record_spans(
        self, mock_applescript, mock_clipboard
    ):
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with profiling.profile("test") as prof:
            with patch("apple_notes.ui_scripting.time.sleep"):
                executor._focus_note_content()
                executor.select_all_and_copy()
                executor.select_all_and_paste()

        assert {"focus", "copy", "paste"} <= set(prof.timing_ms)

    def test_paste_from_clipboard_records_paste_span(
        self, mock_applescript, mock_clipboard
    ):
        """The append path pastes via the single-key method, not the batched
        one — its keystroke time must still land in `paste`."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with profiling.profile("test") as prof:
            with patch("apple_notes.ui_scripting.time.sleep"):
                executor.paste_from_clipboard()

        assert "paste" in prof.timing_ms

    def test_spans_are_noop_without_active_profile(
        self, mock_applescript, mock_clipboard
    ):
        """The write path must work identically with no profile active."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.open_note("Some Note")
            executor._focus_note_content()
            executor.select_all_and_copy()
            executor.select_all_and_paste()


class TestReadNoteAsRtf(TestUIScriptingExecutor):
    """Tests for read_note_as_rtf method."""

    def test_reads_note_content(self, mock_applescript, mock_clipboard):
        """Test reads note content via clipboard."""
        mock_applescript.execute.return_value = (True, "")
        mock_clipboard.read_rtf.return_value = b"{\\rtf1 content}"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            result = executor.read_note_as_rtf("Test Note")

        assert result == b"{\\rtf1 content}"
        mock_clipboard.read_rtf.assert_called_once()

    def test_raises_error_when_clipboard_empty(self, mock_applescript, mock_clipboard):
        """Test raises UIScriptingError when clipboard has no RTF."""
        mock_applescript.execute.return_value = (True, "")
        mock_clipboard.read_rtf.return_value = None

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(UIScriptingError, match="Failed to read note content"):
                executor.read_note_as_rtf("Test Note")


class TestWriteNoteFromRtf(TestUIScriptingExecutor):
    """Tests for write_note_from_rtf method."""

    def test_writes_rtf_to_note(self, mock_applescript, mock_clipboard):
        """Test writes RTF content to note via clipboard, preserving title."""
        # First call for _note_exists, rest for open_note and keystrokes
        mock_applescript.execute.side_effect = [
            (True, "true"),  # _note_exists
            (True, ""),  # open_note
            (True, ""),  # _focus_note_content: Option+Cmd+F
            (True, ""),  # _focus_note_content: Tab
            (True, ""),  # _focus_note_content: Tab
            (True, ""),  # select_body_content: Cmd+Up (move to start)
            (True, ""),  # select_body_content: Down arrow (skip title)
            (True, ""),  # select_body_content: Cmd+Shift+Down (select to end)
            (True, ""),  # paste (keystroke "v")
        ]

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.write_note_from_rtf("Test Note", b"{\\rtf1 content}")

        mock_clipboard.write_rtf.assert_called_once_with(b"{\\rtf1 content}")

    def test_raises_not_found_when_note_missing(self, mock_applescript, mock_clipboard):
        """Test raises NoteNotFoundError when note doesn't exist."""
        mock_applescript.execute.return_value = (True, "false")  # _note_exists

        executor = UIScriptingExecutor()
        with pytest.raises(NoteNotFoundError, match="Test Note"):
            executor.write_note_from_rtf("Test Note", b"{\\rtf1}")


class TestUpdateNoteContent(TestUIScriptingExecutor):
    """Tests for update_note_content method."""

    def test_updates_note_with_plain_text(self, mock_applescript, mock_clipboard):
        """Test updates note content from plain text, preserving title."""
        mock_applescript.execute.side_effect = [
            (True, "true"),  # _note_exists
            (True, ""),  # open_note
            (True, ""),  # _focus_note_content: Option+Cmd+F
            (True, ""),  # _focus_note_content: Tab
            (True, ""),  # _focus_note_content: Tab
            (True, ""),  # select_body_content: Cmd+Up (move to start)
            (True, ""),  # select_body_content: Down arrow (skip title)
            (True, ""),  # select_body_content: Cmd+Shift+Down (select to end)
            (True, ""),  # paste
        ]
        mock_clipboard.text_to_rtf.return_value = b"{\\rtf1 converted}"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.update_note_content("Test Note", "New content")

        mock_clipboard.text_to_rtf.assert_called_once_with("New content")
        mock_clipboard.write_rtf.assert_called_once_with(b"{\\rtf1 converted}")

    def test_raises_not_found_for_missing_note(self, mock_applescript, mock_clipboard):
        """Test raises NoteNotFoundError for missing note."""
        mock_applescript.execute.return_value = (True, "false")

        executor = UIScriptingExecutor()
        mock_clipboard.text_to_rtf.return_value = b"{\\rtf1}"

        with pytest.raises(NoteNotFoundError, match="Test Note"):
            executor.update_note_content("Test Note", "Content")


class TestSendKeyCode(TestUIScriptingExecutor):
    """Tests for _send_key_code method."""

    def test_sends_key_code_without_modifier(self, mock_applescript, mock_clipboard):
        """Test sends key code without modifier."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._send_key_code(125)

        call_args = mock_applescript.execute.call_args[0][0]
        assert "key code 125" in call_args
        assert "using" not in call_args

    def test_sends_key_code_with_modifier(self, mock_applescript, mock_clipboard):
        """Test sends key code with modifier."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._send_key_code(125, "command down")

        call_args = mock_applescript.execute.call_args[0][0]
        assert "key code 125 using command down" in call_args

    def test_raises_accessibility_error_on_permission_denied(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises AccessibilityPermissionError on an assistive-access denial."""
        mock_applescript.execute.return_value = (False, DENIAL_ASSISTIVE)

        executor = UIScriptingExecutor()
        with pytest.raises(AccessibilityPermissionError):
            executor._send_key_code(125)

    def test_raises_ui_scripting_error_on_other_failure(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises UIScriptingError on other failures."""
        mock_applescript.execute.return_value = (False, "Unknown error")

        executor = UIScriptingExecutor()
        with pytest.raises(UIScriptingError, match="Failed to send key code"):
            executor._send_key_code(125)

    def test_script_embeds_frontmost_check(self, mock_applescript, mock_clipboard):
        """Layer B: the key-code script itself guards on frontmost."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._send_key_code(125, "command down")

        script = mock_applescript.execute.call_args[0][0]
        assert "frontmost" in script
        assert "NOTES_NOT_FRONTMOST" in script

    def test_aborts_when_not_frontmost(self, mock_applescript, mock_clipboard):
        """Layer B: the embedded check errors with the sentinel → raise."""
        mock_applescript.execute.return_value = (
            False,
            "execution error: NOTES_NOT_FRONTMOST: Terminal (1)",
        )

        executor = UIScriptingExecutor()
        with pytest.raises(NotesNotFrontmostError, match="Terminal"):
            executor._send_key_code(36)


class TestMoveCursorToEnd(TestUIScriptingExecutor):
    """Tests for move_cursor_to_end method."""

    def test_sends_cmd_down(self, mock_applescript, mock_clipboard):
        """Test sends Cmd+Down key code."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.move_cursor_to_end()

        call_args = mock_applescript.execute.call_args[0][0]
        assert "key code 125" in call_args  # 125 = Down arrow
        assert "command down" in call_args


class TestMoveCursorToStart(TestUIScriptingExecutor):
    """Tests for move_cursor_to_start method."""

    def test_sends_cmd_up(self, mock_applescript, mock_clipboard):
        """Test sends Cmd+Up key code."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.move_cursor_to_start()

        call_args = mock_applescript.execute.call_args[0][0]
        assert "key code 126" in call_args  # 126 = Up arrow
        assert "command down" in call_args


class TestAppendToNote(TestUIScriptingExecutor):
    """Tests for append_to_note method."""

    def test_appends_to_note(self, mock_applescript, mock_clipboard):
        """Test appends content to note."""
        mock_applescript.execute.side_effect = [
            (True, "true"),  # _note_exists
            (True, ""),  # open_note
            (True, ""),  # _focus_note_content: Option+Cmd+F
            (True, ""),  # _focus_note_content: Tab
            (True, ""),  # _focus_note_content: Tab
            (True, ""),  # move_cursor_to_end (key code)
            (True, ""),  # paste (keystroke "v")
        ]
        mock_clipboard.text_to_rtf.return_value = b"{\\rtf1 appended}"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.append_to_note("Test Note", "Appended text")

        # Verify RTF was created with newline prefix
        mock_clipboard.text_to_rtf.assert_called_once_with("\nAppended text")
        mock_clipboard.write_rtf.assert_called_once_with(b"{\\rtf1 appended}")

    def test_raises_not_found_when_note_missing(self, mock_applescript, mock_clipboard):
        """Test raises NoteNotFoundError when note doesn't exist."""
        mock_applescript.execute.return_value = (True, "false")  # _note_exists

        executor = UIScriptingExecutor()
        with pytest.raises(NoteNotFoundError, match="Test Note"):
            executor.append_to_note("Test Note", "Content")


class TestFindReplaceInNote(TestUIScriptingExecutor):
    """Tests for find_replace_in_note method."""

    def test_find_replace_same_length_uses_notes_richtext(
        self, mock_applescript, mock_clipboard
    ):
        """Test same-length find_replace uses Notes richtext format."""
        mock_applescript.execute.side_effect = [
            (True, "true"),  # _note_exists
            (True, ""),  # open_note
            (True, ""),  # _focus_note_content (batched: Opt+Cmd+F, Tab, Tab)
            (True, ""),  # select_all_and_copy (batched: Cmd+A, Cmd+C)
            # After replacement - focus already in note, skip re-focus
            (True, ""),  # select_all_and_paste (batched: Cmd+A, Cmd+V)
        ]
        mock_clipboard.read_notes_richtext.return_value = b"notes richtext data"
        mock_clipboard.read_rtf.return_value = b"{\\rtf1 original}"
        mock_clipboard.find_replace_in_notes_richtext_variable.return_value = (
            b"modified data",
            2,
            [],
        )
        mock_clipboard.find_replace_in_raw_rtf.return_value = (b"{\\rtf1 modified}", 2)

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            # "old" and "new" are same length (3 chars) - uses Notes richtext
            result = executor.find_replace_in_note("Test Note", "old", "new")

        assert result == 2
        mock_clipboard.find_replace_in_notes_richtext_variable.assert_called_once_with(
            b"notes richtext data",
            "old",
            "new",
            links=None,
        )
        # Now also passes RTF data for fallback pasting
        mock_clipboard.write_notes_richtext.assert_called_once_with(
            b"modified data", b"{\\rtf1 modified}"
        )

    def test_find_replace_different_length_uses_notes_richtext(
        self, mock_applescript, mock_clipboard
    ):
        """Test different-length find_replace also uses Notes richtext format."""
        mock_applescript.execute.side_effect = [
            (True, "true"),  # _note_exists
            (True, ""),  # open_note
            (True, ""),  # _focus_note_content (batched: Opt+Cmd+F, Tab, Tab)
            (True, ""),  # select_all_and_copy (batched: Cmd+A, Cmd+C)
            # After replacement - focus already in note, skip re-focus
            (True, ""),  # select_all_and_paste (batched: Cmd+A, Cmd+V)
        ]
        mock_clipboard.read_notes_richtext.return_value = b"notes richtext data"
        mock_clipboard.read_rtf.return_value = b"{\\rtf1 original}"
        mock_clipboard.find_replace_in_notes_richtext_variable.return_value = (
            b"modified data",
            2,
            [],
        )
        mock_clipboard.find_replace_in_raw_rtf.return_value = (b"{\\rtf1 modified}", 2)

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            # "old" (3 chars) and "newtext" (7 chars) differ - now uses Notes richtext
            result = executor.find_replace_in_note("Test Note", "old", "newtext")

        assert result == 2
        mock_clipboard.find_replace_in_notes_richtext_variable.assert_called_once_with(
            b"notes richtext data",
            "old",
            "newtext",
            links=None,
        )
        mock_clipboard.write_notes_richtext.assert_called_once_with(
            b"modified data", b"{\\rtf1 modified}"
        )

    def test_find_replace_rtf_flavor_is_body_scoped(
        self, mock_applescript, mock_clipboard
    ):
        """The RTF fallback flavor must skip the title paragraph (NTG-003)."""
        mock_applescript.execute.side_effect = [(True, "true")] + [(True, "")] * 4
        mock_clipboard.read_notes_richtext.return_value = b"notes richtext data"
        mock_clipboard.read_rtf.return_value = b"{\\rtf1 original}"
        mock_clipboard.find_replace_in_notes_richtext_variable.return_value = (
            b"modified data",
            1,
            [],
        )
        mock_clipboard.find_replace_in_raw_rtf.return_value = (b"{\\rtf1 modified}", 1)

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.find_replace_in_note("Test Note", "old", "new")

        mock_clipboard.find_replace_in_raw_rtf.assert_called_once_with(
            b"{\\rtf1 original}", "old", "new", body_only=True
        )

    def test_find_replace_no_changes_when_no_matches(
        self, mock_applescript, mock_clipboard
    ):
        """Test find_replace returns 0 and doesn't paste when no matches."""
        mock_applescript.execute.side_effect = [
            (True, "true"),  # _note_exists
            (True, ""),  # open_note
            (True, ""),  # _focus_note_content (batched: Opt+Cmd+F, Tab, Tab)
            (True, ""),  # select_all_and_copy (batched: Cmd+A, Cmd+C)
            (True, ""),  # Escape (deselect on 0 replacements)
        ]
        mock_clipboard.read_notes_richtext.return_value = b"notes richtext data"
        mock_clipboard.read_rtf.return_value = b"{\\rtf1 original}"
        mock_clipboard.find_replace_in_notes_richtext_variable.return_value = (
            b"notes richtext data",
            0,
            [],
        )

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            result = executor.find_replace_in_note("Test Note", "nonexistent", "new")

        assert result == 0
        # write_notes_richtext should not be called when no replacements made
        mock_clipboard.write_notes_richtext.assert_not_called()

    def test_find_replace_raises_not_found_when_note_missing(
        self, mock_applescript, mock_clipboard
    ):
        """Test find_replace raises NoteNotFoundError when note doesn't exist."""
        mock_applescript.execute.return_value = (True, "false")  # _note_exists

        executor = UIScriptingExecutor()
        with pytest.raises(NoteNotFoundError, match="Test Note"):
            executor.find_replace_in_note("Test Note", "search", "replace")


class TestActivateNotes(TestUIScriptingExecutor):
    """Tests for _activate_notes method."""

    def test_activates_notes_app(self, mock_applescript, mock_clipboard):
        """Test activates Notes application."""
        mock_applescript.execute.return_value = (True, "")

        executor = UIScriptingExecutor()
        executor._activate_notes()

        call_args = mock_applescript.execute.call_args[0][0]
        assert 'tell application "Notes"' in call_args
        assert "activate" in call_args

    def test_raises_applescript_error_on_failure(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises AppleScriptError when activation fails."""
        mock_applescript.execute.return_value = (False, "Some error")

        executor = UIScriptingExecutor()
        with pytest.raises(AppleScriptError, match="Failed to activate Notes"):
            executor._activate_notes()


class TestCreateNoteWithContent(TestUIScriptingExecutor):
    """Tests for create_note_with_content method."""

    def test_creates_note_with_body(self, mock_applescript, mock_clipboard):
        """Test creates note via UI scripting (Cmd+N) and pastes title and body."""
        mock_applescript.execute.side_effect = [
            (True, ""),  # _activate_notes
            (True, ""),  # Cmd+N (create new note)
            (True, ""),  # paste title (Cmd+V)
            (True, ""),  # Return key (move to body)
            (True, ""),  # paste body (Cmd+V)
        ]
        mock_clipboard.text_to_rtf.return_value = b"{\\rtf1 body content}"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.create_note_with_content("Test Note", "Body text", "Notes")

        # Verify plain text was written for title
        mock_clipboard.write_plain_text.assert_called_once_with("Test Note")
        # Verify RTF was created for body (no newline prefix now)
        mock_clipboard.text_to_rtf.assert_called_once_with("Body text")
        mock_clipboard.write_rtf.assert_called_once_with(b"{\\rtf1 body content}")

    def test_creates_note_with_empty_body(self, mock_applescript, mock_clipboard):
        """Test creates note with just title when body is empty."""
        mock_applescript.execute.side_effect = [
            (True, ""),  # _activate_notes
            (True, ""),  # Cmd+N (create new note)
            (True, ""),  # paste title (Cmd+V)
            (True, ""),  # Return key (move to body)
        ]

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.create_note_with_content("Test Note", "", "Notes")

        # Title should still be pasted
        mock_clipboard.write_plain_text.assert_called_once_with("Test Note")
        # No RTF operations when body is empty
        mock_clipboard.text_to_rtf.assert_not_called()
        mock_clipboard.write_rtf.assert_not_called()

    def test_creates_note_with_whitespace_only_body(
        self, mock_applescript, mock_clipboard
    ):
        """Test creates note with just title when body is whitespace only."""
        mock_applescript.execute.side_effect = [
            (True, ""),  # _activate_notes
            (True, ""),  # Cmd+N (create new note)
            (True, ""),  # paste title (Cmd+V)
            (True, ""),  # Return key (move to body)
        ]

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.create_note_with_content("Test Note", "   \n\t  ", "Notes")

        # Title should still be pasted
        mock_clipboard.write_plain_text.assert_called_once_with("Test Note")
        # No RTF operations for whitespace-only body
        mock_clipboard.text_to_rtf.assert_not_called()
        mock_clipboard.write_rtf.assert_not_called()

    def test_creates_note_with_multiline_body(self, mock_applescript, mock_clipboard):
        """Test creates note with multiline body content."""
        mock_applescript.execute.side_effect = [
            (True, ""),  # _activate_notes
            (True, ""),  # Cmd+N
            (True, ""),  # paste title
            (True, ""),  # Return key
            (True, ""),  # paste body
        ]
        mock_clipboard.text_to_rtf.return_value = b"{\\rtf1 multiline}"

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.create_note_with_content(
                "Test Note", "Line 1\nLine 2\nLine 3", "Notes"
            )

        # Verify RTF was created (no newline prefix in full UI scripting approach)
        mock_clipboard.text_to_rtf.assert_called_once_with("Line 1\nLine 2\nLine 3")

    def test_rejects_custom_folder(self, mock_applescript, mock_clipboard):
        """Test raises ValueError when folder is not 'Notes'."""
        executor = UIScriptingExecutor()

        with pytest.raises(
            ValueError, match="UI scripting only supports 'Notes' folder"
        ):
            executor.create_note_with_content("Test Note", "Body", "Work/Projects")

    def test_raises_accessibility_error_during_ui_scripting(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises AccessibilityPermissionError when UI scripting fails."""
        # _activate_notes succeeds, but Cmd+N fails
        mock_applescript.execute.side_effect = [
            (True, ""),  # _activate_notes success
            (False, DENIAL_ASSISTIVE),  # Cmd+N fails
        ]

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(AccessibilityPermissionError):
                executor.create_note_with_content("Test Note", "Body", "Notes")

    def test_raises_applescript_error_when_activation_fails(
        self, mock_applescript, mock_clipboard
    ):
        """Test raises AppleScriptError when Notes activation fails."""
        mock_applescript.execute.return_value = (False, "Notes not responding")

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(AppleScriptError, match="Failed to activate Notes"):
                executor.create_note_with_content("Test Note", "Body", "Notes")


class TestApplyPendingTableAction(TestUIScriptingExecutor):
    """Tests for apply_pending_keyboard_actions with table action type."""

    def test_table_action_dispatches_to_replace_with_table(
        self, mock_applescript, mock_clipboard
    ):
        """Table action type should call _replace_with_table."""
        mock_applescript.execute.return_value = (True, "")
        # The Find selection check copies the placeholder back (issue #122).
        mock_clipboard.read_plain_text.return_value = "{__NG_TABLE_0__}"

        action = PendingKeyboardAction(
            placeholder="{__NG_TABLE_0__}",
            action_type="table",
            text_to_type="",
            original_markdown="| A | B |\n| --- | --- |\n| 1 | 2 |",
            table_cells=[["A", "B"], ["1", "2"]],
            table_col_count=2,
        )

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            result = executor.apply_pending_keyboard_actions([action])

        assert result == 1

    def test_table_action_deletes_placeholder_first(
        self, mock_applescript, mock_clipboard
    ):
        """Table insertion should delete the placeholder before inserting."""
        calls = []

        mock_clipboard.read_plain_text.return_value = "{__NG_TABLE_0__}"

        def capture_execute(script, **kwargs):
            calls.append(script)
            return (True, "")

        mock_applescript.execute.side_effect = capture_execute

        action = PendingKeyboardAction(
            placeholder="{__NG_TABLE_0__}",
            action_type="table",
            text_to_type="",
            original_markdown="| A | B |\n| --- | --- |\n| 1 | 2 |",
            table_cells=[["A", "B"], ["1", "2"]],
            table_col_count=2,
        )

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            executor.apply_pending_keyboard_actions([action])

        # Should have calls for: find (Cmd+F), type placeholder, Return, Escape,
        # Delete, Option+Cmd+T (insert table), tab navigation, cell pastes
        script_text = " ".join(calls)
        # Delete key should be used (key code 51)
        assert "key code 51" in script_text

    def test_unknown_action_type_raises_error(self, mock_applescript, mock_clipboard):
        """Unknown action type should raise UIScriptingError."""
        mock_applescript.execute.return_value = (True, "")

        action = PendingKeyboardAction(
            placeholder="{__NG_UNKNOWN_0__}",
            action_type="unknown_type",
            text_to_type="foo",
            original_markdown="foo",
        )

        executor = UIScriptingExecutor()
        with patch("apple_notes.ui_scripting.time.sleep"):
            with pytest.raises(UIScriptingError, match="Unknown action type"):
                executor.apply_pending_keyboard_actions([action])
