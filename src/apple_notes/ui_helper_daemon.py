"""UI Helper Daemon — thin asyncio Unix domain socket server for remote UI scripting.

Runs in the macOS GUI session as a LaunchAgent. Accepts JSON commands over
a Unix socket and delegates to ui_operations.py for actual UI scripting work.
All operations are serialized via an asyncio Lock since UI scripting is
inherently sequential.

Protocol (newline-delimited JSON):
    Request:  {"id": "<uuid>", "command": "<cmd>", "params": {...}}
    Success:  {"id": "<uuid>", "success": true, "result": {...}}
    Error:    {"id": "<uuid>", "success": false, "error": {"type": "...", "message": "..."}}
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import session_lock, unified_log
from .config import CONFIG_DIR
from .edit_lock import EDIT_LOCK_TIMEOUT
from .exceptions import (
    AppleNotesError,
    EditLockTimeoutError,
    SessionLockedError,
    UIScriptingError,
)
from .profiling import attach_error, outcome_for_error_type, profile
from .richtext.markdown_converter import PendingKeyboardAction
from .richtext.section_fragment import LinkTarget
from .section_edit import SectionRequest
from .ui_operations import (
    execute_append_to_note,
    execute_create_note,
    execute_delete_note,
    execute_edit_note_section,
    execute_find_replace_multiline,
    execute_move_note,
    execute_replace_note,
    execute_replace_note_link,
    execute_toggle_checklist_item,
)

logger = logging.getLogger(__name__)

SOCKET_PATH = CONFIG_DIR / "ui-helper.sock"
PID_FILE = CONFIG_DIR / "ui-helper.pid"

# Stream buffer limit for asyncio readline (default 64 KB is too small for
# large base64 richtext blobs or long link-target lists)
STREAM_LIMIT = 1024 * 1024  # 1 MB

# Commands that drive UI scripting (CGEvent keystrokes / clipboard paste) —
# the session-lock preflight gates these only. `delete_note` / `move_note` are
# pure AppleScript and work over Apple Events even when the GUI is locked, so
# they stay out. `ping` and any future non-UI commands also stay reachable
# when the screen is locked so clients can still probe liveness.
UI_COMMANDS = frozenset(
    {
        "create_note",
        "append_to_note",
        "replace_note",
        "toggle_checklist_item",
        "find_replace",
        "replace_note_link",
        "edit_note_section",
    }
)

# How long a command may wait behind the one the daemon is running before it
# is refused unstarted (issues #143, #125 NTG-012). The wait used to be
# unbounded, so a write queued behind a long section edit could outlast the
# client's timeout without ever starting. Same bound as the cross-process
# edit lock; the client's WRITE_TIMEOUT budgets for it.
QUEUE_WAIT_TIMEOUT = EDIT_LOCK_TIMEOUT


def _deserialize_links(params: dict) -> dict[str, LinkTarget]:
    """The client's serialized LinkTargets, keyed by lower-case title (#184, #212).

    A pre-#212 client sends only ``name_to_uuid``; its links would be written
    as plain text with a success reply, so that payload is refused.
    """
    if "links" not in params and params.get("name_to_uuid"):
        raise UIScriptingError(
            "This MCP server predates synthesized note links (issue #212) and "
            "sent no link targets; restart it so it runs current code. Nothing "
            "was written."
        )
    return {
        t["title"].lower(): LinkTarget(t["uuid"], t["title"], t["owner"])
        for t in params.get("links", [])
    }


def _deserialize_pending_actions(
    raw_actions: list[dict],
) -> list[PendingKeyboardAction]:
    """Reconstruct PendingKeyboardAction objects from serialized dicts."""
    actions = []
    for d in raw_actions:
        actions.append(
            PendingKeyboardAction(
                placeholder=d["placeholder"],
                action_type=d["action_type"],
                text_to_type=d["text_to_type"],
                original_markdown=d["original_markdown"],
                table_cells=d.get("table_cells"),
                table_col_count=d.get("table_col_count"),
                target_uuid=d.get("target_uuid"),
            )
        )
    return actions


def _error_response(request_id: str | None, exc: Exception) -> dict:
    """Build a JSON-serializable error response from an exception.

    Also attaches the exception to the active profile so the unified-log
    hook (#147) sees the real error object on EVERY error path — including
    the pre-dispatch session-lock check and the unknown-command branch —
    not just the two _dispatch except blocks. No-op outside a profile.
    """
    attach_error(exc)
    return {
        "id": request_id,
        "success": False,
        "error": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
    }


def _success_response(request_id: str | None, result: dict | None = None) -> dict:
    """Build a JSON-serializable success response."""
    return {
        "id": request_id,
        "success": True,
        "result": result or {},
    }


def _detail_for_command(params: dict) -> str:
    """Render the log detail for a command (its note name, when present).

    Tolerant of a malformed (non-dict) ``params``: returns an empty detail so a
    bad payload still flows into ``_dispatch``, which converts it to an error
    response, rather than raising out of the (unprofiled) detail extraction.
    """
    name = params.get("name") if isinstance(params, dict) else None
    return f'name="{name}"' if name else ""


class UIHelperDaemon:
    """Asyncio-based Unix domain socket server for UI scripting commands."""

    def __init__(self, socket_path: Path = SOCKET_PATH):
        self._socket_path = socket_path
        self._lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None

    async def _handle_command(self, request: dict) -> dict:
        """Profile and dispatch a parsed command.

        Every non-``ping`` command is timed: a START/END pair lands in
        ``timing.log`` (issue #98). ``lock_wait`` is captured automatically —
        ``edit_lock`` runs inside ``asyncio.to_thread``, which inherits this
        profile via the copied context. Errors are converted to error
        responses by ``_dispatch`` (not raised), so the outcome is derived from
        the response and applied via ``prof.outcome``.
        """
        request_id = request.get("id")
        command = request.get("command")
        params = request.get("params", {})

        logger.log(
            logging.INFO if command in UI_COMMANDS else logging.DEBUG,
            "Received command: %s (id=%s)",
            command,
            request_id,
        )

        # Ping is a liveness probe — return before profiling so it doesn't spam
        # the timing log.
        if command == "ping":
            return _success_response(request_id, {"status": "ok"})

        with profile(command or "unknown", _detail_for_command(params)) as prof:
            response = await self._dispatch(command, params, request_id)
            if not response.get("success"):
                error_type = response.get("error", {}).get("type", "error")
                prof.outcome = outcome_for_error_type(error_type)
        return response

    @contextlib.asynccontextmanager
    async def _turn(self):
        """Hold the command lock, waiting at most QUEUE_WAIT_TIMEOUT for it.

        Yields False (lock not taken) when the wait runs out. A timed-out
        ``Lock.acquire`` never leaves the lock held: it only claims the lock
        after its wait completes uncancelled.
        """
        try:
            async with asyncio.timeout(QUEUE_WAIT_TIMEOUT):
                await self._lock.acquire()
        except TimeoutError:
            yield False
            return
        try:
            yield True
        finally:
            self._lock.release()

    async def _dispatch(
        self, command: str | None, params: dict, request_id: str | None
    ) -> dict:
        """Run one command, returning a response dict.

        All commands are serialized via the lock to prevent concurrent UI
        scripting operations. AppleNotesError (and unexpected errors) are
        converted to error responses rather than raised.
        """
        if command in UI_COMMANDS:
            try:
                session_lock.require_ui_scripting_session()
            except SessionLockedError as exc:
                return _error_response(request_id, exc)

        async with self._turn() as started:
            if not started:
                return _error_response(
                    request_id,
                    EditLockTimeoutError(
                        "The UI helper is busy with another write; this one "
                        f"didn't start within {QUEUE_WAIT_TIMEOUT:.0f}s and "
                        "changed nothing. Retry once that write finishes."
                    ),
                )
            try:
                if command == "ping":
                    return _success_response(request_id, {"status": "ok"})

                elif command == "create_note":
                    richtext_data = base64.b64decode(params["richtext_data"])
                    pending_actions = _deserialize_pending_actions(
                        params.get("pending_actions", [])
                    )
                    await asyncio.to_thread(
                        execute_create_note,
                        name=params["name"],
                        richtext_data=richtext_data,
                        folder=params.get("folder", "Notes"),
                        pending_actions=pending_actions,
                    )
                    return _success_response(request_id)

                elif command == "append_to_note":
                    richtext_data = base64.b64decode(params["richtext_data"])
                    pending_actions = _deserialize_pending_actions(
                        params.get("pending_actions", [])
                    )
                    await asyncio.to_thread(
                        execute_append_to_note,
                        name=params["name"],
                        richtext_data=richtext_data,
                        pending_actions=pending_actions,
                    )
                    return _success_response(request_id)

                elif command == "replace_note":
                    await asyncio.to_thread(
                        execute_replace_note,
                        name=params["name"],
                        body_markdown=params["body_markdown"],
                        links=_deserialize_links(params),
                    )
                    return _success_response(request_id)

                elif command == "toggle_checklist_item":
                    await asyncio.to_thread(
                        execute_toggle_checklist_item,
                        name=params["name"],
                        item_text=params["item_text"],
                    )
                    return _success_response(request_id)

                elif command == "replace_note_link":
                    await asyncio.to_thread(
                        execute_replace_note_link,
                        name=params["name"],
                        old_uuid=params["old_uuid"],
                        new_uuid=params["new_uuid"],
                        new_alt=params["new_alt"],
                        expected_count=params["expected_count"],
                    )
                    return _success_response(request_id)

                elif command == "edit_note_section":
                    request = SectionRequest(
                        name=params["name"],
                        pk=params["pk"],
                        operation=params["operation"],
                        heading=params.get("heading"),
                        section_text=params.get("section_text"),
                        body=params.get("body"),
                        old_text=params.get("old_text"),
                        new_text=params.get("new_text"),
                        links=_deserialize_links(params),
                        expected_fingerprint=params["expected_fingerprint"],
                        backup_path=params["backup_path"],
                    )
                    result = await asyncio.to_thread(execute_edit_note_section, request)
                    return _success_response(
                        request_id,
                        {
                            "first_paragraph": result.first_paragraph,
                            "paragraph_count": result.paragraph_count,
                            "post_fingerprint": result.post_fingerprint,
                        },
                    )

                elif command == "find_replace":
                    await asyncio.to_thread(
                        execute_find_replace_multiline,
                        name=params["name"],
                        old_text=params["old_text"],
                        replace_text=params.get("replace_text", ""),
                        links=_deserialize_links(params),
                    )
                    return _success_response(request_id)

                elif command == "delete_note":
                    await asyncio.to_thread(
                        execute_delete_note,
                        name=params["name"],
                    )
                    return _success_response(request_id)

                elif command == "move_note":
                    await asyncio.to_thread(
                        execute_move_note,
                        note_id=params["note_id"],
                        folder_id=params["folder_id"],
                    )
                    return _success_response(request_id)

                else:
                    return _error_response(
                        request_id,
                        ValueError(f"Unknown command: {command}"),
                    )

            except AppleNotesError as e:
                return _error_response(request_id, e)
            except Exception as e:
                logger.exception("Unexpected error handling command %s", command)
                return _error_response(request_id, e)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single client connection (one request-response per line)."""
        addr = writer.get_extra_info("peername")
        logger.debug("Client connected: %s", addr)

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    request = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    response = _error_response(None, ValueError(f"Invalid JSON: {e}"))
                    writer.write(json.dumps(response).encode("utf-8") + b"\n")
                    await writer.drain()
                    continue

                response = await self._handle_command(request)
                writer.write(json.dumps(response).encode("utf-8") + b"\n")
                await writer.drain()
        except ConnectionResetError:
            logger.debug("Client disconnected: %s", addr)
        finally:
            writer.close()
            await writer.wait_closed()

    def _cleanup(self) -> None:
        """Remove socket and PID file, only if the PID file still names this process.

        launchd can kill the wrapper mid-write and start a replacement while the
        old Python child finishes, so the paths may now belong to a newer daemon.
        ``start`` writes the PID file before binding, so this process never has a
        socket without having named itself first.
        """
        try:
            owner = PID_FILE.read_text().strip()
        except FileNotFoundError:
            return
        if owner != str(os.getpid()):
            logger.info("Leaving socket and PID file to daemon pid=%s", owner)
            return
        self._socket_path.unlink(missing_ok=True)
        logger.info("Removed socket: %s", self._socket_path)
        PID_FILE.unlink(missing_ok=True)
        logger.info("Removed PID file: %s", PID_FILE)

    async def start(self) -> None:
        """Start the daemon server."""
        # Ensure config directory exists
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        # Remove stale socket from previous run
        if self._socket_path.exists():
            self._socket_path.unlink()
            logger.info("Removed stale socket: %s", self._socket_path)

        # Write PID file
        PID_FILE.write_text(str(os.getpid()))
        logger.info("PID file written: %s (pid=%d)", PID_FILE, os.getpid())

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path), limit=STREAM_LIMIT
        )

        # Set socket permissions (owner-only access)
        os.chmod(self._socket_path, 0o600)

        logger.info("UI Helper Daemon listening on %s", self._socket_path)
        unified_log.log_info(
            "ui-helper daemon started", {"socket": str(self._socket_path)}
        )

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self.stop()))

        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the daemon server and clean up."""
        # Enqueue (no delivery thread): the process exits right after, and a
        # fire-and-forget thread would be killed mid-flight.
        unified_log.log_info("ui-helper daemon stopped", enqueue=True)
        logger.info("Shutting down UI Helper Daemon...")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._cleanup()


def main() -> None:
    """Entry point for the UI helper daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    unified_log.configure(component="daemon")
    daemon = UIHelperDaemon()
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        pass
    finally:
        daemon._cleanup()


if __name__ == "__main__":
    main()
