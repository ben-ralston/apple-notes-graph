"""Tests for the UI helper daemon and client."""

import asyncio
import base64
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.macos_gui


@pytest.fixture
def short_tmp_path():
    """Create a short temp directory path for Unix sockets (108-char limit)."""
    d = tempfile.mkdtemp(prefix="ngh_", dir="/tmp")
    yield Path(d)
    # Cleanup
    import shutil

    shutil.rmtree(d, ignore_errors=True)


from apple_notes.exceptions import (
    AppleNotesError,
    NoteNotFoundError,
    NotesNotFrontmostError,
    NotesNotRespondingError,
    SessionLockedError,
    UIHelperNotAvailableError,
    UIScriptingError,
    WriteVerificationError,
)
from apple_notes.richtext.markdown_converter import PendingKeyboardAction
from apple_notes.richtext.section_fragment import LinkTarget
from apple_notes.ui_helper_client import (
    UIHelperClient,
    _serialize_pending_actions,
)
from apple_notes.ui_helper_daemon import (
    UIHelperDaemon,
    _deserialize_pending_actions,
    _error_response,
    _success_response,
)


# --- PendingKeyboardAction serialization round-trip ---


class TestPendingActionSerialization:
    """Test PendingKeyboardAction serialization/deserialization round-trip."""

    def test_round_trip_single_action(self):
        action = PendingKeyboardAction(
            placeholder="{__NG_TAG_0__}",
            action_type="hashtag",
            text_to_type="todo",
            original_markdown="#todo",
        )
        serialized = _serialize_pending_actions([action])
        deserialized = _deserialize_pending_actions(serialized)

        assert len(deserialized) == 1
        assert deserialized[0].placeholder == action.placeholder
        assert deserialized[0].action_type == action.action_type
        assert deserialized[0].text_to_type == action.text_to_type
        assert deserialized[0].original_markdown == action.original_markdown

    def test_round_trip_multiple_actions(self):
        actions = [
            PendingKeyboardAction(
                placeholder="{__NG_TAG_0__}",
                action_type="hashtag",
                text_to_type="todo",
                original_markdown="#todo",
            ),
            PendingKeyboardAction(
                placeholder="{__NG_LINK_0__}",
                action_type="wiki_link",
                text_to_type="My Note",
                original_markdown="[[My Note]]",
            ),
        ]
        serialized = _serialize_pending_actions(actions)
        deserialized = _deserialize_pending_actions(serialized)

        assert len(deserialized) == 2
        for original, restored in zip(actions, deserialized):
            assert original.placeholder == restored.placeholder
            assert original.action_type == restored.action_type
            assert original.text_to_type == restored.text_to_type
            assert original.original_markdown == restored.original_markdown

    def test_round_trip_empty(self):
        serialized = _serialize_pending_actions([])
        deserialized = _deserialize_pending_actions(serialized)
        assert deserialized == []

    def test_serialized_format_is_json_compatible(self):
        action = PendingKeyboardAction(
            placeholder="{__NG_TAG_0__}",
            action_type="hashtag",
            text_to_type="todo",
            original_markdown="#todo",
        )
        serialized = _serialize_pending_actions([action])
        # Must be JSON-serializable
        json_str = json.dumps(serialized)
        restored = json.loads(json_str)
        assert restored == serialized

    def test_round_trip_table_action(self):
        """Table action should preserve table_cells and table_col_count."""
        action = PendingKeyboardAction(
            placeholder="{__NG_TABLE_0__}",
            action_type="table",
            text_to_type="",
            original_markdown="| A | B |\n| --- | --- |\n| 1 | 2 |",
            table_cells=[["A", "B"], ["1", "2"]],
            table_col_count=2,
        )
        serialized = _serialize_pending_actions([action])
        deserialized = _deserialize_pending_actions(serialized)

        assert len(deserialized) == 1
        restored = deserialized[0]
        assert restored.placeholder == action.placeholder
        assert restored.action_type == "table"
        assert restored.table_cells == [["A", "B"], ["1", "2"]]
        assert restored.table_col_count == 2

    def test_round_trip_mixed_actions_with_table(self):
        """Mixed action types including table should all round-trip correctly."""
        actions = [
            PendingKeyboardAction(
                placeholder="{__NG_TAG_0__}",
                action_type="hashtag",
                text_to_type="todo",
                original_markdown="#todo",
            ),
            PendingKeyboardAction(
                placeholder="{__NG_TABLE_0__}",
                action_type="table",
                text_to_type="",
                original_markdown="| X | Y |\n| --- | --- |\n| 1 | 2 |",
                table_cells=[["X", "Y"], ["1", "2"]],
                table_col_count=2,
            ),
        ]
        serialized = _serialize_pending_actions(actions)
        deserialized = _deserialize_pending_actions(serialized)

        assert len(deserialized) == 2
        # Hashtag action should have no table fields
        assert deserialized[0].table_cells is None
        assert deserialized[0].table_col_count is None
        # Table action should have table fields
        assert deserialized[1].table_cells == [["X", "Y"], ["1", "2"]]
        assert deserialized[1].table_col_count == 2


# --- Base64 encoding/decoding round-trip ---


class TestBase64RoundTrip:
    """Test base64 encoding of richtext data for transport."""

    def test_round_trip_binary_data(self):
        original = b"\x00\x01\x02\xff\xfe\xfd" + b"hello richtext"
        encoded = base64.b64encode(original).decode("ascii")
        decoded = base64.b64decode(encoded)
        assert decoded == original

    def test_round_trip_large_data(self):
        original = bytes(range(256)) * 100  # 25600 bytes
        encoded = base64.b64encode(original).decode("ascii")
        decoded = base64.b64decode(encoded)
        assert decoded == original

    def test_round_trip_empty(self):
        original = b""
        encoded = base64.b64encode(original).decode("ascii")
        decoded = base64.b64decode(encoded)
        assert decoded == original


# --- Error response serialization ---


class TestErrorSerialization:
    """Test exception → JSON → re-raised exception with correct type."""

    def test_error_response_format(self):
        exc = NoteNotFoundError("Note 'Test' not found")
        response = _error_response("req-1", exc)

        assert response["id"] == "req-1"
        assert response["success"] is False
        assert response["error"]["type"] == "NoteNotFoundError"
        assert response["error"]["message"] == "Note 'Test' not found"

    def test_success_response_format(self):
        response = _success_response("req-2", {"status": "ok"})

        assert response["id"] == "req-2"
        assert response["success"] is True
        assert response["result"] == {"status": "ok"}

    def test_success_response_empty_result(self):
        response = _success_response("req-3")

        assert response["id"] == "req-3"
        assert response["success"] is True
        assert response["result"] == {}

    def test_error_response_json_serializable(self):
        exc = UIScriptingError("UI scripting failed: timeout")
        response = _error_response("req-4", exc)
        # Must be JSON-serializable
        json_str = json.dumps(response)
        restored = json.loads(json_str)
        assert restored == response


# --- Client is_available tests ---


class TestClientIsAvailable:
    """Test UIHelperClient.is_available() behavior."""

    def test_not_available_when_socket_missing(self, tmp_path):
        """is_available returns False when socket doesn't exist."""
        client = UIHelperClient(socket_path=tmp_path / "nonexistent.sock")
        assert client.is_available() is False

    def test_not_available_when_connection_refused(self, tmp_path):
        """is_available returns False when nothing is listening."""
        sock_path = tmp_path / "test.sock"
        # Create a file that's not a socket
        sock_path.touch()
        client = UIHelperClient(socket_path=sock_path)
        assert client.is_available() is False


# --- Client timeout handling ---


class TestClientTimeout:
    """Test UIHelperClient timeout behavior."""

    def test_timeout_raises_not_available(self, short_tmp_path):
        """Client raises UIHelperNotAvailableError on timeout."""
        sock_path = short_tmp_path / "s.sock"

        # Create a server socket that accepts but never responds
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        try:
            client = UIHelperClient(socket_path=sock_path, timeout=0.5)
            with pytest.raises(UIHelperNotAvailableError, match="timed out"):
                client._send_command("ping")
        finally:
            server.close()


# --- Client error re-raising ---


class TestClientErrorReraising:
    """Test that the client re-raises remote exceptions with correct types."""

    def _make_server_responding_with(self, sock_path, response_dict):
        """Create a mock server that responds with the given JSON dict."""
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            # Read request (discard)
            conn.recv(4096)
            # Send response
            conn.sendall(json.dumps(response_dict).encode("utf-8") + b"\n")
            conn.close()
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    def test_reraises_note_not_found(self, short_tmp_path):
        sock_path = short_tmp_path / "t.sock"
        response = {
            "id": "test",
            "success": False,
            "error": {
                "type": "NoteNotFoundError",
                "message": "Note 'Missing' not found",
            },
        }
        self._make_server_responding_with(str(sock_path), response)

        client = UIHelperClient(socket_path=sock_path, timeout=5)
        with pytest.raises(NoteNotFoundError, match="Note 'Missing' not found"):
            client._send_command("ping")

    def test_reraises_ui_scripting_error(self, short_tmp_path):
        sock_path = short_tmp_path / "t.sock"
        response = {
            "id": "test",
            "success": False,
            "error": {
                "type": "UIScriptingError",
                "message": "Failed to paste",
            },
        }
        self._make_server_responding_with(str(sock_path), response)

        client = UIHelperClient(socket_path=sock_path, timeout=5)
        with pytest.raises(UIScriptingError, match="Failed to paste"):
            client._send_command("ping")

    def test_reraises_notes_not_frontmost(self, short_tmp_path):
        """Focus-guard abort must round-trip as NotesNotFrontmostError so the
        server's verbatim branch fires (not the generic UI scripting wrapper)."""
        sock_path = short_tmp_path / "t.sock"
        response = {
            "id": "test",
            "success": False,
            "error": {
                "type": "NotesNotFrontmostError",
                "message": "Apple Notes is not frontmost (frontmost app: 'Ghostty')",
            },
        }
        self._make_server_responding_with(str(sock_path), response)

        client = UIHelperClient(socket_path=sock_path, timeout=5)
        with pytest.raises(NotesNotFrontmostError, match="Ghostty") as exc_info:
            client._send_command("ping")
        assert type(exc_info.value) is NotesNotFrontmostError

    def test_reraises_notes_not_responding(self, short_tmp_path):
        """A System Events stall on the remote path must round-trip as
        NotesNotRespondingError (issue #141) — falling back to the bare
        AppleNotesError entry hides it from every MCP tool handler."""
        sock_path = short_tmp_path / "t.sock"
        response = {
            "id": "test",
            "success": False,
            "error": {
                "type": "NotesNotRespondingError",
                "message": "Apple Notes didn't respond in 10s while trying to paste",
            },
        }
        self._make_server_responding_with(str(sock_path), response)

        client = UIHelperClient(socket_path=sock_path, timeout=5)
        with pytest.raises(NotesNotRespondingError, match="didn't respond") as exc_info:
            client._send_command("ping")
        assert type(exc_info.value) is NotesNotRespondingError

    def test_reraises_write_verification(self, short_tmp_path):
        """Verify-after-write failure must round-trip as WriteVerificationError."""
        sock_path = short_tmp_path / "t.sock"
        response = {
            "id": "test",
            "success": False,
            "error": {
                "type": "WriteVerificationError",
                "message": "note 'X' is unchanged afterward",
            },
        }
        self._make_server_responding_with(str(sock_path), response)

        client = UIHelperClient(socket_path=sock_path, timeout=5)
        with pytest.raises(WriteVerificationError, match="unchanged") as exc_info:
            client._send_command("ping")
        assert type(exc_info.value) is WriteVerificationError

    def test_unknown_error_type_falls_back(self, short_tmp_path):
        """Unknown error types fall back to AppleNotesError."""
        sock_path = short_tmp_path / "t.sock"
        response = {
            "id": "test",
            "success": False,
            "error": {
                "type": "SomeNewError",
                "message": "unexpected",
            },
        }
        self._make_server_responding_with(str(sock_path), response)

        client = UIHelperClient(socket_path=sock_path, timeout=5)
        from apple_notes.exceptions import AppleNotesError

        with pytest.raises(AppleNotesError, match="unexpected"):
            client._send_command("ping")


# --- Daemon command dispatch ---


class TestDaemonCommandDispatch:
    """Test daemon command parsing and dispatch with mocked executors."""

    @pytest.fixture
    def daemon(self, tmp_path):
        return UIHelperDaemon(socket_path=tmp_path / "test.sock")

    @pytest.mark.asyncio
    async def test_ping(self, daemon):
        request = {"id": "1", "command": "ping", "params": {}}
        response = await daemon._handle_command(request)
        assert response["success"] is True
        assert response["result"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unknown_command(self, daemon):
        request = {"id": "2", "command": "unknown_cmd", "params": {}}
        response = await daemon._handle_command(request)
        assert response["success"] is False
        assert "Unknown command" in response["error"]["message"]

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_create_note")
    async def test_create_note_dispatch(self, mock_create, daemon):
        richtext = b"fake richtext data"
        actions = [
            {
                "placeholder": "{__NG_TAG_0__}",
                "action_type": "hashtag",
                "text_to_type": "todo",
                "original_markdown": "#todo",
            }
        ]
        request = {
            "id": "3",
            "command": "create_note",
            "params": {
                "name": "Test Note",
                "richtext_data": base64.b64encode(richtext).decode("ascii"),
                "folder": "Notes",
                "pending_actions": actions,
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is True

        mock_create.assert_called_once()
        call_args = mock_create.call_args
        assert call_args.kwargs["name"] == "Test Note"
        assert call_args.kwargs["richtext_data"] == richtext
        assert call_args.kwargs["folder"] == "Notes"
        assert len(call_args.kwargs["pending_actions"]) == 1
        assert call_args.kwargs["pending_actions"][0].action_type == "hashtag"

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_append_to_note")
    async def test_append_to_note_dispatch(self, mock_append, daemon):
        richtext = b"append data"
        request = {
            "id": "4",
            "command": "append_to_note",
            "params": {
                "name": "Existing Note",
                "richtext_data": base64.b64encode(richtext).decode("ascii"),
                "pending_actions": [],
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is True

        mock_append.assert_called_once()
        assert mock_append.call_args.kwargs["name"] == "Existing Note"
        assert mock_append.call_args.kwargs["richtext_data"] == richtext

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_replace_note")
    async def test_replace_note_dispatch(self, mock_replace, daemon):
        request = {
            "id": "5",
            "command": "replace_note",
            "params": {
                "name": "My Note",
                "body_markdown": "## New content\nHello world",
                "links": [{"uuid": "uuid-123", "title": "My Note", "owner": "_o"}],
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is True

        mock_replace.assert_called_once()
        assert mock_replace.call_args.kwargs["name"] == "My Note"
        assert (
            mock_replace.call_args.kwargs["body_markdown"]
            == "## New content\nHello world"
        )
        assert mock_replace.call_args.kwargs["links"] == {
            "my note": LinkTarget("uuid-123", "My Note", "_o")
        }

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_delete_note")
    async def test_delete_note_dispatch(self, mock_delete, daemon):
        request = {
            "id": "7",
            "command": "delete_note",
            "params": {
                "name": "Note To Delete",
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is True

        mock_delete.assert_called_once()
        assert mock_delete.call_args.kwargs["name"] == "Note To Delete"

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_create_note")
    async def test_error_propagation(self, mock_create, daemon):
        mock_create.side_effect = NoteNotFoundError("Note 'X' not found")
        request = {
            "id": "6",
            "command": "create_note",
            "params": {
                "name": "X",
                "richtext_data": base64.b64encode(b"data").decode("ascii"),
                "folder": "Notes",
                "pending_actions": [],
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is False
        assert response["error"]["type"] == "NoteNotFoundError"
        assert "Note 'X' not found" in response["error"]["message"]


class TestDaemonTimingInstrumentation:
    """Issue #98: remote-path writes log a timing line in the same timing.log."""

    @pytest.fixture
    def daemon(self, tmp_path):
        return UIHelperDaemon(socket_path=tmp_path / "test.sock")

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_replace_note")
    async def test_write_command_logs_timing(
        self, mock_replace, daemon, tmp_path, monkeypatch
    ):
        from apple_notes import profiling

        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        request = {
            "id": "1",
            "command": "replace_note",
            "params": {
                "name": "My Note",
                "body_markdown": "hi",
                "links": [],
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is True

        lines = log.read_text().splitlines()
        assert any(l.endswith("START") and "replace_note" in l for l in lines)
        assert any(
            'name="My Note"' in l and "total=" in l and l.endswith("ok") for l in lines
        )

    @pytest.mark.asyncio
    async def test_malformed_params_returns_error_response(
        self, daemon, tmp_path, monkeypatch
    ):
        # A non-dict params must not raise out of the (unprofiled) detail
        # extraction — it should flow into _dispatch and become an error
        # response, as it did before profiling was added.
        from apple_notes import profiling

        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        request = {"id": "9", "command": "create_note", "params": None}
        response = await daemon._handle_command(request)
        assert response["success"] is False

    @pytest.mark.asyncio
    async def test_ping_is_not_profiled(self, daemon, tmp_path, monkeypatch):
        from apple_notes import profiling

        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)

        await daemon._handle_command({"id": "2", "command": "ping", "params": {}})
        assert not log.exists() or log.read_text() == ""

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_replace_note")
    async def test_error_outcome_logged(
        self, mock_replace, daemon, tmp_path, monkeypatch
    ):
        from apple_notes import profiling

        log = tmp_path / "timing.log"
        monkeypatch.setattr(profiling, "TIMING_LOG_PATH", log)
        mock_replace.side_effect = NoteNotFoundError("nope")

        request = {
            "id": "3",
            "command": "replace_note",
            "params": {"name": "Gone", "body_markdown": "x", "links": []},
        }
        response = await daemon._handle_command(request)
        assert response["success"] is False
        end = [l for l in log.read_text().splitlines() if not l.endswith("START")][0]
        assert end.endswith("error:NoteNotFoundError")


class TestDaemonSessionLockPreflight:
    """The daemon must short-circuit UI commands when the session is locked,
    without entering the executor or dispatching to asyncio.to_thread."""

    @pytest.fixture
    def daemon(self, tmp_path):
        return UIHelperDaemon(socket_path=tmp_path / "test.sock")

    @pytest.fixture
    def locked_preflight(self):
        with patch(
            "apple_notes.session_lock.require_ui_scripting_session",
            side_effect=SessionLockedError("locked; unlock the Mac"),
        ) as m:
            yield m

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_create_note")
    async def test_create_note_short_circuits_when_locked(
        self, mock_create, daemon, locked_preflight
    ):
        request = {
            "id": "lock-1",
            "command": "create_note",
            "params": {
                "name": "Locked Note",
                "richtext_data": base64.b64encode(b"data").decode("ascii"),
                "folder": "Notes",
                "pending_actions": [],
            },
        }
        response = await daemon._handle_command(request)

        assert response["success"] is False
        assert response["error"]["type"] == "SessionLockedError"
        assert "unlock" in response["error"]["message"].lower()
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_append_to_note")
    async def test_append_to_note_short_circuits_when_locked(
        self, mock_append, daemon, locked_preflight
    ):
        request = {
            "id": "lock-2",
            "command": "append_to_note",
            "params": {
                "name": "Locked",
                "richtext_data": base64.b64encode(b"data").decode("ascii"),
                "pending_actions": [],
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is False
        assert response["error"]["type"] == "SessionLockedError"
        mock_append.assert_not_called()

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_replace_note")
    async def test_replace_note_short_circuits_when_locked(
        self, mock_replace, daemon, locked_preflight
    ):
        request = {
            "id": "lock-3",
            "command": "replace_note",
            "params": {
                "name": "Locked",
                "body_markdown": "body",
                "links": [],
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is False
        assert response["error"]["type"] == "SessionLockedError"
        mock_replace.assert_not_called()

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_toggle_checklist_item")
    async def test_toggle_short_circuits_when_locked(
        self, mock_toggle, daemon, locked_preflight
    ):
        request = {
            "id": "lock-4",
            "command": "toggle_checklist_item",
            "params": {"name": "Locked", "item_text": "item"},
        }
        response = await daemon._handle_command(request)
        assert response["success"] is False
        assert response["error"]["type"] == "SessionLockedError"
        mock_toggle.assert_not_called()

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_find_replace_multiline")
    async def test_find_replace_short_circuits_when_locked(
        self, mock_fr, daemon, locked_preflight
    ):
        request = {
            "id": "lock-5",
            "command": "find_replace",
            "params": {
                "name": "Locked",
                "old_text": "old",
                "replace_text": "new",
            },
        }
        response = await daemon._handle_command(request)
        assert response["success"] is False
        assert response["error"]["type"] == "SessionLockedError"
        mock_fr.assert_not_called()

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_delete_note")
    async def test_delete_note_dispatched_even_when_locked(
        self, mock_delete, daemon, locked_preflight
    ):
        """delete_note is pure AppleScript (works over Apple Events, no
        GUI session needed) and must NOT be gated by the session-lock
        preflight. Daemon should dispatch it normally even when locked."""
        request = {
            "id": "lock-6",
            "command": "delete_note",
            "params": {"name": "Locked"},
        }
        response = await daemon._handle_command(request)
        assert response["success"] is True
        mock_delete.assert_called_once_with(name="Locked")
        locked_preflight.assert_not_called()

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_move_note")
    async def test_move_note_dispatched_even_when_locked(
        self, mock_move, daemon, locked_preflight
    ):
        """move_note (#230) is pure AppleScript like delete_note: dispatched by
        id and never gated by the session-lock preflight."""
        request = {
            "id": "lock-move",
            "command": "move_note",
            "params": {"note_id": "NOTE-ID", "folder_id": "FOLDER-ID"},
        }
        response = await daemon._handle_command(request)
        assert response["success"] is True
        mock_move.assert_called_once_with(note_id="NOTE-ID", folder_id="FOLDER-ID")
        locked_preflight.assert_not_called()

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_move_note")
    async def test_move_note_trash_refusal_keeps_its_type(self, mock_move, daemon):
        from apple_notes.exceptions import NoteInTrashError
        from apple_notes.ui_helper_client import _raise_error_response

        mock_move.side_effect = NoteInTrashError("in Recently Deleted")
        response = await daemon._handle_command(
            {"id": "m2", "command": "move_note", "params": {"note_id": "a", "folder_id": "b"}}
        )
        assert response["error"]["type"] == "NoteInTrashError"
        with pytest.raises(NoteInTrashError):
            _raise_error_response(response["error"])

    @pytest.mark.asyncio
    async def test_ping_still_works_when_locked(self, daemon, locked_preflight):
        """Ping is not a UI command; must succeed regardless of lock state."""
        response = await daemon._handle_command(
            {"id": "lock-7", "command": "ping", "params": {}}
        )
        assert response["success"] is True
        assert response["result"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unknown_command_reports_unknown_not_locked(
        self, daemon, locked_preflight
    ):
        """Malformed requests must still report 'Unknown command', not a
        session-lock error — otherwise clients can't tell bugs from lock state."""
        response = await daemon._handle_command(
            {"id": "lock-8", "command": "bogus", "params": {}}
        )
        assert response["success"] is False
        assert "Unknown command" in response["error"]["message"]
        assert response["error"]["type"] != "SessionLockedError"


class TestClientReraisesSessionLockedError:
    """Critical regression: SessionLockedError must round-trip through the
    Unix socket as its concrete type, not demote to AppleNotesError. Without
    this, the MCP server's specific `except SessionLockedError` branch never
    fires for SSH callers — which is the exact bug this plan is fixing."""

    def _make_server_responding_with(self, sock_path, response_dict):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(json.dumps(response_dict).encode("utf-8") + b"\n")
            conn.close()
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return thread

    def test_reraises_session_locked_error(self, short_tmp_path):
        sock_path = short_tmp_path / "sl.sock"
        response = {
            "id": "test",
            "success": False,
            "error": {
                "type": "SessionLockedError",
                "message": "macOS GUI session is unavailable; unlock the Mac",
            },
        }
        self._make_server_responding_with(str(sock_path), response)

        client = UIHelperClient(socket_path=sock_path, timeout=5)
        with pytest.raises(SessionLockedError) as exc_info:
            client._send_command("create_note")

        # Must not demote to the generic base
        assert type(exc_info.value) is SessionLockedError
        assert "unlock" in str(exc_info.value).lower()

    def test_session_locked_is_not_demoted_to_apple_notes_error(self, short_tmp_path):
        """Defensive catch: the `except AppleNotesError` branch in the MCP
        server would eat this if the client fell back to the default map
        entry. Pinning the concrete type prevents that regression."""
        sock_path = short_tmp_path / "sl2.sock"
        response = {
            "id": "test",
            "success": False,
            "error": {
                "type": "SessionLockedError",
                "message": "locked",
            },
        }
        self._make_server_responding_with(str(sock_path), response)

        client = UIHelperClient(socket_path=sock_path, timeout=5)
        try:
            client._send_command("create_note")
        except SessionLockedError as e:
            # Preferred path: concrete subclass is reached
            assert type(e) is SessionLockedError
            return
        except AppleNotesError:
            pytest.fail(
                "Client demoted SessionLockedError to AppleNotesError; "
                "_ERROR_TYPE_MAP is missing the SessionLockedError entry."
            )
        pytest.fail("Expected SessionLockedError to be raised")


def test_error_type_map_relays_encrypted_note_error():
    """A locked note hit on the daemon path must arrive as EncryptedNoteError
    (issue #126) — otherwise it demotes to the bare AppleNotesError fallback
    and no MCP tool handler recognises it."""
    from apple_notes.exceptions import EncryptedNoteError
    from apple_notes.ui_helper_client import _ERROR_TYPE_MAP

    assert _ERROR_TYPE_MAP.get("EncryptedNoteError") is EncryptedNoteError


# --- Integration: full round-trip through socket ---


class TestIntegrationRoundTrip:
    """Integration test: start daemon, send command, verify response."""

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_create_note")
    async def test_full_round_trip(self, mock_create, short_tmp_path):
        """Start a real daemon, send a real command through socket, verify."""
        sock_path = short_tmp_path / "i.sock"
        daemon = UIHelperDaemon(socket_path=sock_path)

        # Start daemon in background
        server_task = asyncio.create_task(daemon.start())

        # Wait for socket to appear
        for _ in range(50):
            if sock_path.exists():
                break
            await asyncio.sleep(0.05)
        assert sock_path.exists(), "Daemon socket not created"

        try:
            client = UIHelperClient(socket_path=sock_path, timeout=10)

            # Test ping (run client in thread to avoid blocking event loop)
            result = await asyncio.to_thread(client.is_available)
            assert result is True

            # Test create_note
            richtext = b"test richtext content"
            action = PendingKeyboardAction(
                placeholder="{__NG_TAG_0__}",
                action_type="hashtag",
                text_to_type="test",
                original_markdown="#test",
            )
            await asyncio.to_thread(
                client.create_note,
                name="Integration Test",
                richtext_data=richtext,
                folder="Notes",
                pending_actions=[action],
            )

            mock_create.assert_called_once()
            assert mock_create.call_args.kwargs["name"] == "Integration Test"
            assert mock_create.call_args.kwargs["richtext_data"] == richtext

        finally:
            await daemon.stop()
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    @patch("apple_notes.ui_helper_daemon.execute_create_note")
    async def test_round_trip_error_propagation(self, mock_create, short_tmp_path):
        """Verify errors propagate correctly through the socket."""
        sock_path = short_tmp_path / "e.sock"
        daemon = UIHelperDaemon(socket_path=sock_path)
        mock_create.side_effect = NoteNotFoundError("Note 'Missing' not found")

        server_task = asyncio.create_task(daemon.start())

        for _ in range(50):
            if sock_path.exists():
                break
            await asyncio.sleep(0.05)

        try:
            client = UIHelperClient(socket_path=sock_path, timeout=10)

            with pytest.raises(NoteNotFoundError, match="Note 'Missing' not found"):
                await asyncio.to_thread(
                    client.create_note,
                    name="Missing",
                    richtext_data=b"data",
                    folder="Notes",
                    pending_actions=[],
                )
        finally:
            await daemon.stop()
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_session_locked_preserves_exact_type_through_real_socket(
        self, short_tmp_path
    ):
        """End-to-end regression: SessionLockedError from the daemon-side preflight
        must arrive at the SSH caller as `SessionLockedError` (not demoted to its
        parent `AppleNotesError`). Exercises daemon wire format + client parsing +
        `_ERROR_TYPE_MAP` + MRO handling in a single real-socket round-trip."""
        sock_path = short_tmp_path / "l.sock"
        daemon = UIHelperDaemon(socket_path=sock_path)

        server_task = asyncio.create_task(daemon.start())

        for _ in range(50):
            if sock_path.exists():
                break
            await asyncio.sleep(0.05)
        assert sock_path.exists(), "Daemon socket not created"

        message = (
            "macOS GUI session is unavailable for UI scripting (integration test)."
        )
        try:
            client = UIHelperClient(socket_path=sock_path, timeout=10)

            with patch(
                "apple_notes.session_lock.require_ui_scripting_session",
                side_effect=SessionLockedError(message),
            ):
                with pytest.raises(SessionLockedError) as exc_info:
                    await asyncio.to_thread(
                        client.create_note,
                        name="Locked",
                        richtext_data=b"data",
                        folder="Notes",
                        pending_actions=[],
                    )

            assert type(exc_info.value) is SessionLockedError
            assert str(exc_info.value) == message
        finally:
            await daemon.stop()
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass
