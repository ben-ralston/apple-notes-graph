"""Synchronous client for the UI Helper Daemon.

Connects to the daemon's Unix domain socket, sends JSON commands, and
re-raises remote exceptions as local exception types.
"""

import base64
import json
import socket
import uuid
from pathlib import Path

from . import demo_mode, unified_log
from .richtext.section_fragment import FragmentError
from .sections import MediaCapExceeded, NoteBusyError, SectionError
from .config import CONFIG_DIR
from .exceptions import (
    AccessibilityPermissionError,
    AppleNotesError,
    AppleScriptError,
    ClipboardError,
    EditLockTimeoutError,
    EncryptedNoteError,
    FolderNotFoundError,
    NoteInTrashError,
    LinksNotConfirmedError,
    NoteNotFoundError,
    NotesModalOpenError,
    NotesNotFrontmostError,
    NotesNotRespondingError,
    PartialWriteError,
    PlaceholderNotFoundError,
    SectionEditDamagedError,
    SectionEditRolledBackError,
    SessionLockedError,
    UIHelperNotAvailableError,
    UIHelperTimeoutError,
    UIScriptingError,
    WrongNoteOpenError,
    WriteVerificationError,
)
from .richtext import MarkdownConversionError, markdown_to_richtext_new
from .richtext.markdown_converter import PendingKeyboardAction
from .ui_scripting import refuse_untypeable_actions

SOCKET_PATH = CONFIG_DIR / "ui-helper.sock"

# How long the client waits for a write command's answer (issues #143, #125).
# It covers one write's realistic worst case: up to 30s queued behind another
# daemon command (QUEUE_WAIT_TIMEOUT), 30s for the cross-process edit lock,
# the overlay handshake and lead window, the 5s frontmost poll plus one
# in-flight probe, ONE stalled 90s pasteboard read (PASTEBOARD_READ_TIMEOUT)
# and three 30s keystroke scripts: about 265s. tests/test_ui_helper_timeouts.py
# sums the real constants. It is not a hard bound: a write makes several
# pasteboard reads, each with its own 90s budget, and if more than one stalls
# it can still run past this. That is why a timeout here is reported as "may
# still complete" (UIHelperTimeoutError), never as a failure: the old 60s
# ceiling reported writes that were still landing as failed, and the retry
# applied them twice.
WRITE_TIMEOUT = 300.0

# Liveness probe: a wedged daemon must not hang ping for the write budget.
PING_TIMEOUT = 5.0

# Map remote exception type names to local exception classes
_ERROR_TYPE_MAP: dict[str, type[Exception]] = {
    "NoteNotFoundError": NoteNotFoundError,
    # A locked note hit by the daemon's baseline read (issue #126) — without
    # this it demotes to AppleNotesError and loses the "encrypted" diagnosis.
    "EncryptedNoteError": EncryptedNoteError,
    "UIScriptingError": UIScriptingError,
    "AccessibilityPermissionError": AccessibilityPermissionError,
    "SessionLockedError": SessionLockedError,
    "NotesNotFrontmostError": NotesNotFrontmostError,
    # An open dialog in Notes (issue #199): keeps the "close it and retry"
    # diagnosis, and the remote queue waits on it instead of failing.
    "NotesModalOpenError": NotesModalOpenError,
    # Without this entry a System Events stall on the remote path (issue #141)
    # demotes to the bare AppleNotesError fallback, which no MCP tool handler
    # catches — the caller loses the "Notes isn't answering" diagnosis.
    "NotesNotRespondingError": NotesNotRespondingError,
    # A write that landed in part (issue #200): without these the remote
    # queue would see a bare AppleNotesError and file it as a plain failure.
    "LinksNotConfirmedError": LinksNotConfirmedError,
    # A Find that missed a tag/table placeholder (issue #122): partial, so
    # the remote queue never retries it.
    "PlaceholderNotFoundError": PlaceholderNotFoundError,
    "PartialWriteError": PartialWriteError,
    "WriteVerificationError": WriteVerificationError,
    # Section editing (issue #184): keep the specific diagnosis across the daemon.
    "SectionError": SectionError,
    "MediaCapExceeded": MediaCapExceeded,
    "NoteBusyError": NoteBusyError,
    "FragmentError": FragmentError,
    "WrongNoteOpenError": WrongNoteOpenError,
    "SectionEditRolledBackError": SectionEditRolledBackError,
    "SectionEditDamagedError": SectionEditDamagedError,
    "AppleScriptError": AppleScriptError,
    "ClipboardError": ClipboardError,
    "FolderNotFoundError": FolderNotFoundError,
    # move_note (#230): the AppleScript side re-checks trash and lock state.
    "NoteInTrashError": NoteInTrashError,
    "MarkdownConversionError": MarkdownConversionError,
    "EditLockTimeoutError": EditLockTimeoutError,
    "AppleNotesError": AppleNotesError,
    "ValueError": ValueError,
}


def _raise_error_response(error_info: dict) -> None:
    """Reconstruct and raise the daemon's error as a local exception.

    The daemon side already reported the failure to the unified logger
    (#147), so the reconstructed exception is marked as reported — otherwise
    the caller-side profiler hook would emit a duplicate event for the same
    user-visible failure.
    """
    error_type = error_info.get("type", "AppleNotesError")
    error_message = error_info.get("message", "Unknown error")
    exc_class = _ERROR_TYPE_MAP.get(error_type, AppleNotesError)
    exc = exc_class(error_message)
    unified_log.mark_reported(exc)
    raise exc


def _serialize_links(links) -> list[dict]:
    """LinkTargets (lower-case title -> LinkTarget) for the wire (#184, #212)."""
    return [
        {"uuid": t.uuid, "title": t.title, "owner": t.owner}
        for t in (links or {}).values()
    ]


def _legacy_name_to_uuid(links) -> dict[str, str]:
    """The pre-#212 wire field, so a daemon still on older code resolves links."""
    return {key: t.uuid for key, t in (links or {}).items()}


def _serialize_pending_actions(
    actions: list[PendingKeyboardAction],
) -> list[dict]:
    """Serialize PendingKeyboardAction objects to dicts for JSON transport."""
    result = []
    for a in actions:
        d: dict = {
            "placeholder": a.placeholder,
            "action_type": a.action_type,
            "text_to_type": a.text_to_type,
            "original_markdown": a.original_markdown,
        }
        if a.table_cells is not None:
            d["table_cells"] = a.table_cells
        if a.table_col_count is not None:
            d["table_col_count"] = a.table_col_count
        if a.target_uuid is not None:
            d["target_uuid"] = a.target_uuid
        result.append(d)
    return result


class UIHelperClient:
    """Synchronous client for the UI Helper Daemon."""

    def __init__(
        self,
        socket_path: Path = SOCKET_PATH,
        timeout: float = WRITE_TIMEOUT,
    ):
        self._socket_path = socket_path
        self._timeout = timeout

    def _send_command(
        self,
        command: str,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Send a command to the daemon and return the parsed response.

        `timeout` overrides the client's own (the write budget) for this call.

        Raises:
            UIHelperNotAvailableError: If the daemon is not reachable
            UIHelperTimeoutError: If it accepted the command but didn't answer
                in time (the command may still complete)
            Re-raised remote exceptions based on error type
        """
        demo_mode.require_live("Sending a write to the UI helper daemon")
        timeout = self._timeout if timeout is None else timeout
        request = {
            "id": str(uuid.uuid4()),
            "command": command,
            "params": params or {},
        }

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(self._socket_path))
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise UIHelperNotAvailableError(
                f"UI helper daemon not available at {self._socket_path}: {e}"
            ) from e

        try:
            # Send newline-delimited JSON
            payload = json.dumps(request).encode("utf-8") + b"\n"
            sock.sendall(payload)

            # Read response (line-delimited)
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise UIHelperNotAvailableError(
                        "UI helper daemon closed connection unexpectedly"
                    )
                data += chunk

            response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        except socket.timeout:
            raise UIHelperTimeoutError(
                f"UI helper daemon timed out after {timeout:.0f}s"
            )
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            raise UIHelperNotAvailableError(
                f"UI helper daemon connection lost: {e}"
            ) from e
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise UIHelperNotAvailableError(
                f"UI helper daemon sent invalid response: {e}"
            ) from e
        finally:
            sock.close()

        # Handle errors
        if not response.get("success"):
            _raise_error_response(response.get("error", {}))

        return response.get("result", {})

    def is_available(self) -> bool:
        """Check if the daemon is running and responsive."""
        try:
            self._send_command("ping", timeout=PING_TIMEOUT)
            return True
        except (UIHelperNotAvailableError, OSError):
            return False

    def create_note(
        self,
        name: str,
        richtext_data: bytes,
        folder: str,
        pending_actions: list[PendingKeyboardAction],
    ) -> None:
        """Send a create_note command to the daemon."""
        refuse_untypeable_actions(pending_actions)  # stale daemon (#260)
        self._send_command(
            "create_note",
            {
                "name": name,
                "richtext_data": base64.b64encode(richtext_data).decode("ascii"),
                "folder": folder,
                "pending_actions": _serialize_pending_actions(pending_actions),
            },
        )

    def append_to_note(
        self,
        name: str,
        richtext_data: bytes,
        pending_actions: list[PendingKeyboardAction],
    ) -> None:
        """Send an append_to_note command to the daemon."""
        refuse_untypeable_actions(pending_actions)  # stale daemon (#260)
        self._send_command(
            "append_to_note",
            {
                "name": name,
                "richtext_data": base64.b64encode(richtext_data).decode("ascii"),
                "pending_actions": _serialize_pending_actions(pending_actions),
            },
        )

    def replace_note(self, name: str, body_markdown: str, links) -> None:
        """Send a replace_note command to the daemon.

        `links` (lower-case title -> LinkTarget) are synthesized daemon-side
        as link attachments (issue #212).
        """
        _refuse_untypeable_markdown(body_markdown)
        self._send_command(
            "replace_note",
            {
                "name": name,
                "body_markdown": body_markdown,
                "links": _serialize_links(links),
                "name_to_uuid": _legacy_name_to_uuid(links),
            },
        )

    def toggle_checklist_item(self, name: str, item_text: str) -> None:
        """Send a toggle_checklist_item command to the daemon."""
        self._send_command(
            "toggle_checklist_item",
            {"name": name, "item_text": item_text},
        )

    def replace_note_link(
        self,
        name: str,
        old_uuid: str,
        new_uuid: str,
        new_alt: str,
        expected_count: int,
    ) -> None:
        """Send a replace_note_link command to the daemon (issue #183)."""
        self._send_command(
            "replace_note_link",
            {
                "name": name,
                "old_uuid": old_uuid,
                "new_uuid": new_uuid,
                "new_alt": new_alt,
                "expected_count": expected_count,
            },
        )

    def edit_note_section(self, req) -> dict:
        """Send an edit_note_section command to the daemon (issue #184).

        Returns the daemon's {first_paragraph, paragraph_count, post_fingerprint}.
        """
        return self._send_command(
            "edit_note_section",
            {
                "name": req.name,
                "pk": req.pk,
                "operation": req.operation,
                "heading": req.heading,
                "section_text": req.section_text,
                "body": req.body,
                "old_text": req.old_text,
                "new_text": req.new_text,
                "links": _serialize_links(req.links),
                "expected_fingerprint": req.expected_fingerprint,
                "backup_path": req.backup_path,
            },
        )

    def find_replace(
        self,
        name: str,
        old_text: str,
        replace_text: str,
        links=None,
    ) -> None:
        """Send a find_replace command to the daemon (links: see replace_note)."""
        _refuse_untypeable_markdown(replace_text)
        self._send_command(
            "find_replace",
            {
                "name": name,
                "old_text": old_text,
                "replace_text": replace_text,
                "links": _serialize_links(links),
                "name_to_uuid": _legacy_name_to_uuid(links),
            },
        )

    def delete_note(self, name: str) -> None:
        """Send a delete_note command to the daemon."""
        self._send_command("delete_note", {"name": name})

    def move_note(self, note_id: str, folder_id: str) -> None:
        """Send a move_note command (AppleScript ids, issue #230) to the daemon."""
        self._send_command("move_note", {"note_id": note_id, "folder_id": folder_id})


def _refuse_untypeable_markdown(markdown: str) -> None:
    """Refuse a tag/table the keyboard flow can't type before the command
    leaves this process (issue #260).

    The daemon refuses too, but it is a long-lived LaunchAgent: after a pull
    it can still run older code that types a wide table as loose cell text.
    """
    refuse_untypeable_actions(markdown_to_richtext_new(markdown).pending_actions)
