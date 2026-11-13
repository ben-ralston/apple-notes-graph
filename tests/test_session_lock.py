"""Tests for the session_lock preflight detector."""

from unittest.mock import patch

import pytest

from apple_notes import ui_operations
from apple_notes.exceptions import SessionLockedError
from apple_notes.session_lock import require_ui_scripting_session

pytestmark = pytest.mark.macos_gui


UNLOCKED_SESSION = {
    "kCGSSessionOnConsoleKey": 1,
    "kCGSessionLoginDoneKey": 1,
}


def _patched_session(info):
    """Patch CGSessionCopyCurrentDictionary to return the given dict."""
    return patch(
        "apple_notes.session_lock.CGSessionCopyCurrentDictionary",
        return_value=info,
    )


class TestRequireUIScriptingSession:
    """Exercise each branch of the fail-closed session check."""

    def test_unlocked_on_console_session_returns_none(self):
        with _patched_session(UNLOCKED_SESSION):
            assert require_ui_scripting_session() is None

    def test_missing_session_dict_raises(self):
        with _patched_session(None):
            with pytest.raises(SessionLockedError):
                require_ui_scripting_session()

    def test_not_on_console_raises(self):
        info = dict(UNLOCKED_SESSION)
        info["kCGSSessionOnConsoleKey"] = 0
        with _patched_session(info):
            with pytest.raises(SessionLockedError):
                require_ui_scripting_session()

    def test_on_console_key_missing_raises(self):
        info = dict(UNLOCKED_SESSION)
        info.pop("kCGSSessionOnConsoleKey")
        with _patched_session(info):
            with pytest.raises(SessionLockedError):
                require_ui_scripting_session()

    def test_login_not_done_raises(self):
        info = dict(UNLOCKED_SESSION)
        info["kCGSessionLoginDoneKey"] = 0
        with _patched_session(info):
            with pytest.raises(SessionLockedError):
                require_ui_scripting_session()

    def test_login_done_key_missing_raises(self):
        info = dict(UNLOCKED_SESSION)
        info.pop("kCGSessionLoginDoneKey")
        with _patched_session(info):
            with pytest.raises(SessionLockedError):
                require_ui_scripting_session()

    def test_screen_locked_raises(self):
        info = dict(UNLOCKED_SESSION)
        info["CGSSessionScreenIsLocked"] = 1
        with _patched_session(info):
            with pytest.raises(SessionLockedError):
                require_ui_scripting_session()

    def test_error_message_mentions_unlock_and_doc(self):
        with _patched_session(None):
            with pytest.raises(SessionLockedError) as exc_info:
                require_ui_scripting_session()
        message = str(exc_info.value)
        assert "Unlock" in message or "unlock" in message
        assert "docs/keep-gui-unlocked-for-remote-writes.md" in message


class TestUIOperationsPreflight:
    """Every execute_* reached by the MCP server must fail loudly when the
    session is locked, without touching clipboard state or the AppleScript
    subprocess."""

    @pytest.fixture
    def locked_preflight(self):
        with patch(
            "apple_notes.session_lock.require_ui_scripting_session",
            side_effect=SessionLockedError("locked"),
        ) as m:
            yield m

    @pytest.fixture(autouse=True)
    def mock_clipboard_and_executor(self):
        """Ensure clipboard and executor are not touched when preflight fails."""
        with (
            patch("apple_notes.ui_operations.ClipboardManager") as clipboard,
            patch("apple_notes.ui_operations.UIScriptingExecutor") as executor,
        ):
            yield clipboard, executor

    def _assert_untouched(self, mocks):
        clipboard, executor = mocks
        clipboard.assert_not_called()
        executor.assert_not_called()

    def test_execute_create_note_preflight(
        self, locked_preflight, mock_clipboard_and_executor
    ):
        with pytest.raises(SessionLockedError):
            ui_operations.execute_create_note(
                name="n", richtext_data=b"x", folder="Notes", pending_actions=[]
            )
        self._assert_untouched(mock_clipboard_and_executor)

    def test_execute_append_to_note_preflight(
        self, locked_preflight, mock_clipboard_and_executor
    ):
        with pytest.raises(SessionLockedError):
            ui_operations.execute_append_to_note(
                name="n", richtext_data=b"x", pending_actions=[]
            )
        self._assert_untouched(mock_clipboard_and_executor)

    def test_execute_replace_note_preflight(
        self, locked_preflight, mock_clipboard_and_executor
    ):
        with pytest.raises(SessionLockedError):
            ui_operations.execute_replace_note(
                name="n", body_markdown="body", links={}
            )
        self._assert_untouched(mock_clipboard_and_executor)

    def test_execute_toggle_checklist_item_preflight(
        self, locked_preflight, mock_clipboard_and_executor
    ):
        with pytest.raises(SessionLockedError):
            ui_operations.execute_toggle_checklist_item(name="n", item_text="item")
        self._assert_untouched(mock_clipboard_and_executor)

    def test_execute_find_replace_multiline_preflight(
        self, locked_preflight, mock_clipboard_and_executor
    ):
        with pytest.raises(SessionLockedError):
            ui_operations.execute_find_replace_multiline(
                name="n", old_text="old", replace_text="new"
            )
        self._assert_untouched(mock_clipboard_and_executor)

    def test_execute_delete_note_skips_preflight(
        self, locked_preflight, mock_clipboard_and_executor
    ):
        """delete_note uses pure AppleScript (no UI scripting), so the
        session-lock preflight must not gate it. SSH-driven deletes need
        to keep working with the screen locked."""
        _, executor = mock_clipboard_and_executor
        ui_operations.execute_delete_note(name="n")
        locked_preflight.assert_not_called()
        executor.return_value.delete_note.assert_called_once_with("n")
