"""Daemon write budgets vs the client's socket timeout (issues #143, #125 NTG-012).

A remote (SSH) write goes client -> Unix socket -> daemon. The client used to
give up after 60s while the daemon's own budgets (System Events scripts, the
90s pasteboard read, the edit lock) could legitimately run longer, so a write
that was still landing was reported as "daemon not available": the caller
retried and the write applied twice. The daemon also queued commands behind
its in-process lock with no bound.

Headless and CI-visible: the socket and the executors are mocked.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apple_notes import ui_helper_client, ui_helper_daemon
from apple_notes.clipboard import PASTEBOARD_READ_TIMEOUT
from apple_notes.edit_lock import EDIT_LOCK_TIMEOUT
from apple_notes.ui_helper_client import UIHelperClient
from apple_notes.ui_helper_daemon import UIHelperDaemon
from apple_notes.ui_scripting import UIScriptingExecutor
from apple_notes.write_indicator import DEFAULT_LEAD_MS, HANDSHAKE_TIMEOUT


_SECTION_REQ = SimpleNamespace(
    name="N",
    pk=1,
    operation="append",
    heading="H",
    section_text=None,
    body="b",
    old_text=None,
    new_text=None,
    links={},
    expected_fingerprint="fp",
    backup_path="/b.sqlite",
)


def _worst_case_daemon_write() -> float:
    """One daemon write's realistic worst case before it answers.

    Counts one stalled pasteboard read, not every read a write makes: each
    read has its own 90s budget, so several stalls can still outrun
    WRITE_TIMEOUT. The client reports that as "may still complete".
    """
    return (
        ui_helper_daemon.QUEUE_WAIT_TIMEOUT  # behind another daemon command
        + EDIT_LOCK_TIMEOUT  # behind a local MCP / Electron write
        + HANDSHAKE_TIMEOUT
        + DEFAULT_LEAD_MS / 1000  # write-guard overlay + lead window
        + UIScriptingExecutor.FRONTMOST_POLL_TIMEOUT
        + UIScriptingExecutor.SYSTEM_EVENTS_TIMEOUT  # one in-flight probe
        + PASTEBOARD_READ_TIMEOUT  # one read that hits the 60s+ race
        + 3 * UIScriptingExecutor.TEXT_ENTRY_TIMEOUT  # focus, copy, paste scripts
    )


class TestClientTimeoutCeiling:
    def test_write_timeout_exceeds_the_daemon_budgets(self):
        assert ui_helper_client.WRITE_TIMEOUT > _worst_case_daemon_write()

    def test_client_defaults_to_the_write_timeout(self):
        assert UIHelperClient()._timeout == ui_helper_client.WRITE_TIMEOUT

    @pytest.mark.parametrize(
        "method, args",
        [
            ("create_note", ("N", b"rt", "Notes", [])),
            ("append_to_note", ("N", b"rt", [])),
            ("replace_note", ("N", "body", {})),
            ("toggle_checklist_item", ("N", "item")),
            ("replace_note_link", ("N", "OLD", "NEW", "Alt", 1)),
            ("edit_note_section", (_SECTION_REQ,)),
            ("find_replace", ("N", "old", "new")),
            ("delete_note", ("N",)),
            ("move_note", ("note-id", "folder-id")),
        ],
    )
    def test_every_write_command_uses_the_write_timeout(self, method, args):
        sock = MagicMock()
        sock.recv.return_value = b'{"success": true, "result": {}}\n'
        with patch("apple_notes.ui_helper_client.socket.socket", return_value=sock):
            getattr(UIHelperClient(), method)(*args)
        sock.settimeout.assert_called_once_with(ui_helper_client.WRITE_TIMEOUT)

    def test_every_client_write_method_is_covered(self):
        """A new client command must join the parametrized list above."""
        methods = {
            name
            for name, value in vars(UIHelperClient).items()
            if callable(value) and not name.startswith("_")
        }
        assert methods - {"is_available"} == {
            "create_note",
            "append_to_note",
            "replace_note",
            "toggle_checklist_item",
            "replace_note_link",
            "edit_note_section",
            "find_replace",
            "delete_note",
            "move_note",
        }

    def test_ping_uses_a_short_timeout(self):
        """A wedged daemon must not hang a liveness probe for the write budget."""
        sock = MagicMock()
        sock.recv.return_value = b'{"success": true, "result": {"status": "ok"}}\n'
        with patch("apple_notes.ui_helper_client.socket.socket", return_value=sock):
            assert UIHelperClient().is_available() is True
        sock.settimeout.assert_called_once_with(ui_helper_client.PING_TIMEOUT)
        assert ui_helper_client.PING_TIMEOUT <= 10.0


class TestDaemonQueueBound:
    def test_queue_wait_matches_the_cross_process_lock(self):
        assert ui_helper_daemon.QUEUE_WAIT_TIMEOUT == EDIT_LOCK_TIMEOUT

    def test_command_queued_past_the_budget_is_refused_unstarted(self):
        daemon = UIHelperDaemon()

        async def run():
            await daemon._lock.acquire()  # another command is running
            try:
                with (
                    patch.object(ui_helper_daemon, "QUEUE_WAIT_TIMEOUT", 0.05),
                    patch(
                        "apple_notes.ui_helper_daemon.execute_delete_note"
                    ) as execute,
                ):
                    response = await daemon._dispatch(
                        "delete_note", {"name": "N"}, "r1"
                    )
            finally:
                daemon._lock.release()
            return response, execute

        response, execute = asyncio.run(run())
        execute.assert_not_called()
        assert response["success"] is False
        assert response["error"]["type"] == "EditLockTimeoutError"
        message = response["error"]["message"]
        assert "didn't start" in message
        assert "retry" in message.lower()

    def test_refusal_leaves_the_lock_usable(self):
        daemon = UIHelperDaemon()

        async def run():
            await daemon._lock.acquire()
            with patch.object(ui_helper_daemon, "QUEUE_WAIT_TIMEOUT", 0.01):
                await daemon._dispatch("delete_note", {"name": "N"}, "r1")
            daemon._lock.release()
            with patch("apple_notes.ui_helper_daemon.execute_delete_note"):
                return await daemon._dispatch("delete_note", {"name": "N"}, "r2")

        response = asyncio.run(run())
        assert response["success"] is True
        assert not daemon._lock.locked()

    def test_command_runs_when_the_lock_frees_inside_the_budget(self):
        daemon = UIHelperDaemon()

        async def run():
            await daemon._lock.acquire()
            asyncio.get_running_loop().call_later(0.02, daemon._lock.release)
            with (
                patch.object(ui_helper_daemon, "QUEUE_WAIT_TIMEOUT", 5.0),
                patch("apple_notes.ui_helper_daemon.execute_delete_note") as execute,
            ):
                response = await daemon._dispatch("delete_note", {"name": "N"}, "r1")
            return response, execute

        response, execute = asyncio.run(run())
        assert response["success"] is True
        execute.assert_called_once_with(name="N")
