"""Demo mode is a hard boundary: nothing may reach the real Notes app.

Every check runs with a stub ``osascript`` first on PATH that records any call,
so the tests prove the refusal without the real app ever being scripted.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from apple_notes import demo_mode
from apple_notes.demo_mode import DemoModeError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def osascript_log(tmp_path, monkeypatch):
    """A stub osascript that logs its arguments and fails; returns the log path."""
    log = tmp_path / "osascript-calls.log"
    stub = tmp_path / "bin" / "osascript"
    stub.parent.mkdir()
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 1\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}:{os.environ['PATH']}")
    return log


@pytest.fixture
def demo(monkeypatch, tmp_path, osascript_log):
    """Demo mode on, HOME pointed at an empty folder, osascript stubbed."""
    monkeypatch.setenv("NOTES_GRAPH_DEMO", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    return osascript_log


def test_switch_defaults_to_normal_behaviour(monkeypatch):
    monkeypatch.delenv("NOTES_GRAPH_DEMO", raising=False)
    assert not demo_mode.is_demo()
    demo_mode.require_live("anything")  # no error
    for value in ("", "0", "false", "no"):
        monkeypatch.setenv("NOTES_GRAPH_DEMO", value)
        assert not demo_mode.is_demo()


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_switch_values(monkeypatch, value):
    monkeypatch.setenv("NOTES_GRAPH_DEMO", value)
    assert demo_mode.is_demo()
    with pytest.raises(DemoModeError, match="not available in demo mode"):
        demo_mode.require_live("Scripting the Notes app")


WRITE_TOOLS = {
    "add_note": dict(name="Demo", body="text"),
    "update_note_content": dict(name="Weather station", body="x", operation="append"),
    "delete_note": dict(name="Weather station"),
    "move_note": dict(name="Weather station", folder="Garden"),
    "toggle_checklist_item": dict(name="Pantry staples", item_text="Olive oil"),
    "replace_note_link": dict(name="Inbox", old_target="Weather station", new_target="Compost"),
    "edit_note_section": dict(name="Weather station", operation="append", heading="Status", body="x"),
}


def test_every_write_tool_is_covered():
    """A new write tool must be added here (and decorated with _live_only)."""
    from apple_notes_mcp import server

    read_only = {
        "list_notes", "list_folders", "read_note_content", "get_backlinks", "search_notes",
        "get_note_graph", "export_note", "export_folder", "export_graph", "find_orphans",
        "find_broken_links", "note_health_check", "suggest_links",
    }  # fmt: skip
    registered = set(server.mcp._tool_manager._tools)
    assert registered - read_only == set(WRITE_TOOLS)


@pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
def test_write_tool_refuses_in_demo_mode(tool, demo, tmp_path):
    from apple_notes_mcp import server

    with pytest.raises(ToolError, match=f"{tool} is not available in demo mode"):
        getattr(server, tool)(**WRITE_TOOLS[tool])
    assert not demo.exists(), "osascript was called"
    assert not (tmp_path / "Library").exists(), "a backup or other state was written"


def test_applescript_runner_refuses_before_spawning(demo):
    from apple_notes.applescript import AppleScriptExecutor

    with pytest.raises(DemoModeError):
        AppleScriptExecutor().execute('tell application "Notes" to activate')
    assert not demo.exists()


def test_low_level_write_layers_refuse(demo, tmp_path):
    from apple_notes.backup import BackupManager
    from apple_notes.clipboard import ClipboardManager
    from apple_notes.icloud_sync import nudge_icloud_sync
    from apple_notes.ui_helper_client import UIHelperClient
    from apple_notes.write_indicator import write_indicator

    with pytest.raises(DemoModeError):
        ClipboardManager()
    with pytest.raises(DemoModeError):
        BackupManager(backup_dir=str(tmp_path / "backups")).create_backup()
    assert not (tmp_path / "backups").exists()
    with pytest.raises(DemoModeError):
        UIHelperClient().delete_note("Weather station")
    with pytest.raises(DemoModeError):
        with write_indicator("add_note", "Demo"):
            pass
    os.environ["NOTES_GRAPH_SYNC_NUDGE"] = "1"
    try:
        assert nudge_icloud_sync() is False
    finally:
        del os.environ["NOTES_GRAPH_SYNC_NUDGE"]
    assert not demo.exists()


def test_ui_operations_refuse(demo):
    """The functions the server and daemon call to drive Notes."""
    from apple_notes import ui_operations

    with pytest.raises(DemoModeError):
        ui_operations.execute_delete_note("Weather station")
    with pytest.raises(DemoModeError):
        ui_operations.execute_toggle_checklist_item("Pantry staples", "Olive oil")
    with pytest.raises(DemoModeError):
        ui_operations.execute_move_note(
            "x-coredata://D0000000-0000-4000-8000-0000000000A1/ICNote/p1", "x-coredata://D0000000-0000-4000-8000-0000000000A1/ICFolder/p2"
        )
    assert not demo.exists()


def test_reminders_refuse(demo):
    from apple_notes_mcp.reminders.grant import wrapper_reminders_status
    from apple_notes_mcp.reminders.store import EventKitStore

    store = EventKitStore()
    with pytest.raises(DemoModeError):
        store.authorization_status()
    with pytest.raises(DemoModeError):
        store.lists()
    assert wrapper_reminders_status() == "unknown (demo mode)"


def test_remote_connector_refuses_to_start(demo, capsys):
    from apple_notes_mcp.remote import cli

    assert cli.main(["serve"]) == 2
    assert "not available in demo mode" in capsys.readouterr().err


@pytest.mark.parametrize(
    "script,args",
    [("add_link.py", ["--pk", "1", "--target", "Compost"]), ("delete_note.py", ["--pk", "1"])],
)
def test_ui_write_scripts_refuse(script, args, demo, tmp_path):
    """The scripts the Electron app runs for add-link and delete."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )  # fmt: skip
    assert "demo mode" in json.loads(result.stdout)["error"]
    assert not demo.exists()
