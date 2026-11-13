"""The edit_note_section MCP tool, its daemon route and error classification (issue #184).

Headless and CI-visible: the engine wrapper, database and snapshot reads are mocked.
"""

import asyncio
import types
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from apple_notes import unified_log
from apple_notes.exceptions import (
    SectionEditDamagedError,
    SectionEditRolledBackError,
)
from apple_notes.richtext.section_fragment import FragmentError, LinkTarget
from apple_notes.section_edit import SectionRequest, SectionResult
from apple_notes.sections import SectionError

HOME_U = "AAAAAAAA-1111-1111-1111-111111111111"
SRC = MagicMock(pk=7, uuid="SRC-UUID")
SRC.name = "Plan"
SECTION = MagicMock(a=2, b=4, fingerprint="fp-section")
PLAN = MagicMock()
RESULT = SectionResult(first_paragraph=2, paragraph_count=3, post_fingerprint="fp-post")


class FakeDB:
    owner = "_owner"
    titled = 1
    media = 0

    def note_media_bytes(self, pk):
        return type(self).media

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def count_notes_titled(self, name):
        return type(self).titled

    def get_note_by_name(self, name):
        return SRC

    live_title = "Home"

    def get_note_by_uuid(self, uuid):
        return types.SimpleNamespace(name=type(self).live_title)

    def link_owner_identifier(self, uuid):
        return type(self).owner


@pytest.fixture
def env():
    FakeDB.owner, FakeDB.titled = "_owner", 1
    FakeDB.media = 0
    backup = MagicMock()
    backup.create_backup.return_value = "/path/backup.sqlite"
    with (
        patch("apple_notes_mcp.server.NotesDatabase", side_effect=lambda *a: FakeDB()),
        patch("apple_notes_mcp.server.NoteContentReader") as reader,
        patch("apple_notes_mcp.server.load_snapshot", return_value=MagicMock()) as snap,
        patch(
            "apple_notes_mcp.server.resolve_section", return_value=SECTION
        ) as resolve,
        patch("apple_notes_mcp.server.plan_edit", return_value=PLAN) as plan,
        patch(
            "apple_notes_mcp.server._build_name_to_uuid", return_value={"home": HOME_U}
        ),
        patch("apple_notes_mcp.server.BackupManager", return_value=backup),
        patch("apple_notes_mcp.server.nudge_icloud_sync", return_value=True),
        patch("apple_notes_mcp.server._is_gui_session", return_value=True),
        patch(
            "apple_notes_mcp.server.execute_edit_note_section", return_value=RESULT
        ) as execute,
        patch("apple_notes_mcp.server.UIHelperClient") as client,
    ):
        reader.return_value.paragraph_renderer.return_value = lambda pk: []
        client.return_value.edit_note_section.return_value = {
            "first_paragraph": 2,
            "paragraph_count": 3,
            "post_fingerprint": "fp-post",
        }
        yield MagicMock(
            backup=backup,
            execute=execute,
            client=client,
            resolve=resolve,
            plan=plan,
            snap=snap,
        )


def call(**kw):
    from apple_notes_mcp.server import edit_note_section

    args = {"name": "Plan", "operation": "replace", "heading": "Tasks", "body": "new"}
    args.update(kw)
    return edit_note_section(**args)


def test_edit_note_section_local_success(env):
    result = call(body="see [[Home]] and [[Nowhere]]")
    result.pop("elapsed_ms"), result.pop("timing")
    assert result == {
        "success": True,
        "note_name": "Plan",
        "operation": "replace",
        "section": {"first_paragraph": 2, "paragraph_count": 3},
        "verified_outside_unchanged": True,
        "backup_path": "/path/backup.sqlite",
        "sync_nudged": True,
    }
    req = env.execute.call_args.args[0]
    assert isinstance(req, SectionRequest)
    assert (req.name, req.pk, req.operation, req.heading) == (
        "Plan",
        7,
        "replace",
        "Tasks",
    )
    assert req.expected_fingerprint == "fp-section"
    assert req.backup_path == "/path/backup.sqlite"
    # lower-case key; the unresolved [[Nowhere]] stays plain text
    assert dict(req.links) == {"home": LinkTarget(HOME_U, "Home", "_owner")}
    env.client.return_value.edit_note_section.assert_not_called()


def test_edit_note_section_routes_daemon_when_remote(env):
    with patch("apple_notes_mcp.server._is_gui_session", return_value=False):
        result = call()
    env.execute.assert_not_called()
    req = env.client.return_value.edit_note_section.call_args.args[0]
    assert isinstance(req, SectionRequest)
    assert result["section"] == {"first_paragraph": 2, "paragraph_count": 3}
    assert result["verified_outside_unchanged"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": "bogus"},
        {"heading": None, "section_text": None},
        {"section_text": "x"},  # both heading and section_text
        {"heading": "  "},
        {"heading": None, "section_text": "   "},
        {"body": None},  # replace needs body
        {"operation": "append", "body": " "},
        {"operation": "delete", "body": "x"},
        {"operation": "find_replace", "body": "x", "old_text": "a", "new_text": "b"},
        {"operation": "find_replace", "body": None, "old_text": "a"},
        {"operation": "find_replace", "body": None, "new_text": "b"},
        {"operation": "replace", "old_text": "a"},
        {"operation": "append", "new_text": "a"},
        {"operation": "delete", "body": None, "old_text": "a"},
        {"operation": "delete", "body": None, "new_text": "b"},
    ],
)
def test_edit_note_section_validation_matrix(env, kwargs):
    with pytest.raises(ToolError):
        call(**kwargs)
    env.backup.create_backup.assert_not_called()
    env.execute.assert_not_called()


def test_edit_note_section_validation_accepts_valid_shapes(env):
    call(operation="delete", body=None)
    call(operation="find_replace", body=None, old_text="a", new_text="")
    call(operation="append", body="more")
    call(operation="replace", heading=None, section_text="- item")


def test_edit_note_section_refusal_before_backup(env):
    env.resolve.side_effect = SectionError("No heading 'Tasks' in the note")
    with pytest.raises(ToolError, match="No heading 'Tasks'"):
        call()
    env.backup.create_backup.assert_not_called()
    env.execute.assert_not_called()


def test_edit_note_section_duplicate_or_missing_title(env):
    FakeDB.titled = 2
    with pytest.raises(ToolError, match="2 notes are titled 'Plan'"):
        call()
    FakeDB.titled = 0
    with pytest.raises(ToolError, match="not found"):
        call()
    env.backup.create_backup.assert_not_called()


def test_edit_note_section_rolled_back_is_tool_error(env):
    env.execute.side_effect = SectionEditRolledBackError(
        "Section edit in 'Plan' failed verification (x); it was undone and the note is unchanged."
    )
    with pytest.raises(ToolError, match="rolled_back") as info:
        call()
    assert "unchanged" in str(info.value)


def test_edit_note_section_link_owner_missing_refused(env):
    FakeDB.owner = None
    with pytest.raises(ToolError) as info:
        call(body="see [[Home]]")
    assert (
        str(info.value)
        == "Can't create a link to 'Home': its account owner id wasn't found."
    )
    env.backup.create_backup.assert_not_called()


def test_edit_note_section_fragment_error_is_tool_error(env):
    env.execute.side_effect = FragmentError("new tags aren't supported")
    with pytest.raises(ToolError, match="new tags"):
        call()


# --- logger classification ---------------------------------------------------


def _raised(env, exc):
    env.execute.side_effect = exc
    with pytest.raises(ToolError) as info:
        call()
    return info.value


def test_section_and_fragment_errors_are_not_faults(env):
    for exc in (SectionError("x"), FragmentError("y")):
        assert unified_log.should_report(_raised(env, exc)) is False


def test_rolled_back_is_still_reported(env):
    assert (
        unified_log.should_report(_raised(env, SectionEditRolledBackError("rb")))
        is True
    )


def test_damaged_is_not_double_reported(env):
    tool_error = _raised(env, SectionEditDamagedError("damaged"))
    assert str(tool_error) == "damaged"
    assert getattr(tool_error, unified_log._LOGGED_MARKER, False) is True
    assert unified_log.report_failure("edit_note_section", exc=tool_error) is None


# --- IPC ---------------------------------------------------------------------


def _request():
    return SectionRequest(
        name="Plan",
        pk=7,
        operation="replace",
        heading="Tasks",
        section_text=None,
        body="see [[Home]]",
        old_text=None,
        new_text=None,
        links={"home": LinkTarget(HOME_U, "Home", "_owner")},
        expected_fingerprint="fp",
        backup_path="/b",
    )


WIRE = {
    "name": "Plan",
    "pk": 7,
    "operation": "replace",
    "heading": "Tasks",
    "section_text": None,
    "body": "see [[Home]]",
    "old_text": None,
    "new_text": None,
    "links": [{"uuid": HOME_U, "title": "Home", "owner": "_owner"}],
    "expected_fingerprint": "fp",
    "backup_path": "/b",
}


def test_client_sends_edit_note_section():
    from apple_notes.ui_helper_client import UIHelperClient

    client = UIHelperClient()
    with patch.object(
        client, "_send_command", return_value={"first_paragraph": 2}
    ) as send:
        out = client.edit_note_section(_request())
    send.assert_called_once_with("edit_note_section", WIRE)
    assert out == {"first_paragraph": 2}


def test_daemon_dispatches_edit_note_section():
    from apple_notes.ui_helper_daemon import UI_COMMANDS, UIHelperDaemon

    assert "edit_note_section" in UI_COMMANDS
    daemon = UIHelperDaemon()
    with patch(
        "apple_notes.ui_helper_daemon.execute_edit_note_section", return_value=RESULT
    ) as execute:
        response = asyncio.run(daemon._dispatch("edit_note_section", dict(WIRE), "r1"))
    assert response["success"] is True
    assert response["result"] == {
        "first_paragraph": 2,
        "paragraph_count": 3,
        "post_fingerprint": "fp-post",
    }
    execute.assert_called_once_with(_request())


# --- SSH route hardening (fix round 1) -----------------------------------------


def test_edit_note_section_daemon_timeout_says_may_still_complete(env):
    from apple_notes.exceptions import UIHelperTimeoutError

    env.client.return_value.edit_note_section.side_effect = UIHelperTimeoutError("t/o")
    with patch("apple_notes_mcp.server._is_gui_session", return_value=False):
        with pytest.raises(ToolError, match="may still complete") as info:
            call()
    assert "not available" not in str(info.value)
    assert "300s" in str(info.value)
    env.client.assert_called_once_with(timeout=300.0)


def test_client_timeout_raises_distinct_error():
    import socket as socket_module

    from apple_notes.exceptions import UIHelperNotAvailableError, UIHelperTimeoutError
    from apple_notes.ui_helper_client import UIHelperClient

    sock = MagicMock()
    sock.recv.side_effect = socket_module.timeout()
    with patch("apple_notes.ui_helper_client.socket.socket", return_value=sock):
        with pytest.raises(UIHelperTimeoutError) as info:
            UIHelperClient()._send_command("ping")
    assert isinstance(info.value, UIHelperNotAvailableError)


def test_daemon_side_fragment_error_maps_to_tool_error(env):
    from apple_notes.ui_helper_client import _raise_error_response

    def daemon_raises(_req):
        _raise_error_response({"type": "FragmentError", "message": "new tags aren't supported"})

    env.client.return_value.edit_note_section.side_effect = daemon_raises
    with patch("apple_notes_mcp.server._is_gui_session", return_value=False):
        with pytest.raises(ToolError, match="new tags") as info:
            call()
    assert isinstance(info.value.__cause__, FragmentError)
    assert unified_log.should_report(info.value) is False


def test_new_tag_refused_before_backup(env):
    with pytest.raises(ToolError, match="tags"):
        call(body="todo {#urgent}")
    env.backup.create_backup.assert_not_called()
    env.execute.assert_not_called()


def test_new_table_refused_before_backup(env):
    with pytest.raises(ToolError, match="tables"):
        call(operation="find_replace", body=None, old_text="a", new_text="| a | b |\n|---|---|\n| 1 | 2 |")
    env.backup.create_backup.assert_not_called()


def test_link_target_uses_live_note_title(env):
    FakeDB.live_title = "Home Base"
    try:
        call(body="see [[home]]")
    finally:
        FakeDB.live_title = "Home"
    req = env.execute.call_args.args[0]
    assert dict(req.links) == {"home": LinkTarget(HOME_U, "Home Base", "_owner")}


def test_media_over_cap_refused_before_backup(env):
    FakeDB.media = 25_000_001
    with pytest.raises(ToolError, match="update_note_content"):
        call()
    env.backup.create_backup.assert_not_called()
    env.execute.assert_not_called()


def test_media_at_cap_goes_ahead(env):
    FakeDB.media = 25_000_000
    assert call()["success"] is True
    env.execute.assert_called_once()
