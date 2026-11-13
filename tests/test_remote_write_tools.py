"""Queued remote write tools + get_write_status (issue #169)."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from apple_notes import folder_scope
from apple_notes_mcp import server
from apple_notes_mcp.remote import tools as remote_tools
from apple_notes_mcp.remote.request_log import RequestLog
from apple_notes_mcp.remote.write_apply import WriteApplier, WriteRefused
from apple_notes_mcp.remote.write_queue import WriteQueue, WriteQueueConfig

ALL_WRITES = sorted(remote_tools.WRITE_TOOLS)


class StubApplier:
    def __init__(self, queue, refuse=None):
        self.queue = queue
        self.calls = []
        self.refuse = refuse

    def accept(self, tool, args, subject=None):
        self.calls.append((tool, args, subject))
        if self.refuse:
            raise WriteRefused(self.refuse)
        wid = self.queue.admit(
            dict(tool=tool, operation=args.get("operation", tool), args=args, note_title=args["name"])
        )
        return {"write_id": wid, "queued_ahead": 0}


class StubWorker:
    def __init__(self):
        self.woken = 0

    def wake(self):
        self.woken += 1

    def applies_hint(self):
        return "1 min after the Mac is next unlocked"


@pytest.fixture
def queue(tmp_path):
    return WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"))


def _tools(queue, applier=None, worker=None, write_tools=ALL_WRITES):
    applier = applier or StubApplier(queue)
    return {
        t.name: t
        for t in remote_tools.remote_tools(
            server.mcp, write_tools=write_tools, applier=applier, worker=worker
        )
    }


def _call(tool, **args):
    return asyncio.run(tool.run(args))


def test_write_tools_without_a_queue_refuse_to_build():
    with pytest.raises(ValueError, match="queue"):
        remote_tools.remote_tools(server.mcp, write_tools=["add_note"])


def test_queued_tools_keep_the_stdio_schema_and_say_they_queue(queue):
    tools = _tools(queue)
    stdio = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in ALL_WRITES:
        assert tools[name].parameters == stdio[name].parameters, name
        assert tools[name].description.startswith(stdio[name].description.rstrip()), name
        assert "get_write_status" in tools[name].description
        assert tools[name].is_async


def test_status_tool_only_when_write_tools_are_on(queue):
    assert "get_write_status" not in {t.name for t in remote_tools.remote_tools(server.mcp)}
    assert "get_write_status" in _tools(queue)


def test_queued_call_returns_immediately_and_wakes_the_worker(queue):
    worker = StubWorker()
    applier = StubApplier(queue)
    out = _call(
        _tools(queue, applier, worker)["update_note_content"],
        name="Daily", body="b", operation="append",
    )
    assert out["status"] == "queued" and out["write_id"].startswith("w_")
    assert out["applies"] == "1 min after the Mac is next unlocked"
    assert worker.woken == 1
    tool, args, _ = applier.calls[0]
    assert tool == "update_note_content" and args["operation"] == "append"
    assert queue.get(out["write_id"])["status"] == "queued"


def test_refusal_surfaces_as_tool_error(queue):
    tools = _tools(queue, StubApplier(queue, refuse="Note 'X' not found."))
    with pytest.raises(ToolError, match="not found"):
        _call(tools["add_note"], name="X", body="b")


def test_get_write_status_never_returns_args(queue):
    tools = _tools(queue)
    out = _call(tools["add_note"], name="Groceries", body="SECRET BODY")
    one = _call(tools["get_write_status"], write_id=out["write_id"])
    assert one["write_id"] == out["write_id"] and one["status"] == "queued"
    assert one["note_title"] == "Groceries" and one["tool"] == "add_note"
    assert "SECRET BODY" not in json.dumps(one)
    many = _call(tools["get_write_status"])
    assert [w["write_id"] for w in many["writes"]] == [out["write_id"]]
    with pytest.raises(ToolError, match="no write"):
        _call(tools["get_write_status"], write_id="w_nope")


class NoNotesDB:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def count_notes_titled(self, name):
        return 0


def test_requests_log_write_events_carry_no_titles_or_args(tmp_path, queue):
    log_path = tmp_path / "requests.log"
    applier = WriteApplier(queue, db_factory=NoNotesDB, log=RequestLog(log_path))
    applier.accept("add_note", {"name": "Private Title", "body": "private body"})
    with pytest.raises(WriteRefused):
        applier.accept("add_note", {"name": "Private Title 2", "body": "x" * 20_001})
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [e["action"] for e in events] == ["accept", "deny"]
    assert events[1]["cap"] == "max_body_chars"
    assert all(e["event"] == "write" and e["tool"] == "add_note" for e in events)
    assert "Private" not in log_path.read_text() and "private body" not in log_path.read_text()


def test_build_remote_app_wires_the_queue_but_does_not_start_the_worker(tmp_path):
    from apple_notes_mcp.remote.app import build_remote_app
    from apple_notes_mcp.remote.config import RemoteConfig

    cfg = RemoteConfig(
        public_url="https://connector.example.com",
        allowed_github_login="octo-owner",
        allowed_github_id=1001,
        github_client_id="id",
        github_client_secret="s",
        write_tools=("add_note",),
        write_queue=WriteQueueConfig(state_dir=tmp_path / "wq"),
        state_dir=tmp_path / "remote",
        log_path=tmp_path / "requests.log",
    )
    try:
        app = build_remote_app(cfg)
    finally:
        folder_scope.reset()
    worker = app.write_worker
    assert worker is not None and worker._thread is None
    assert worker.queue.state_dir == tmp_path / "wq"
    assert (tmp_path / "wq" / "queue.sqlite").exists()


def test_build_remote_app_without_write_tools_has_no_queue(tmp_path):
    from apple_notes_mcp.remote.app import build_remote_app
    from apple_notes_mcp.remote.config import RemoteConfig

    cfg = RemoteConfig(
        public_url="https://connector.example.com",
        allowed_github_login="octo-owner",
        allowed_github_id=1001,
        github_client_id="id",
        github_client_secret="s",
        write_queue=WriteQueueConfig(state_dir=tmp_path / "wq"),
        state_dir=tmp_path / "remote",
        log_path=tmp_path / "requests.log",
    )
    try:
        app = build_remote_app(cfg)
    finally:
        folder_scope.reset()
    assert app.write_worker is None
    assert not (tmp_path / "wq").exists()


def test_queued_delete_returns_the_note_to_confirm(queue):
    """#229: the caller sees which note it hit before the delete applies."""

    class ConfirmingApplier(StubApplier):
        def accept(self, tool, args, subject=None):
            out = super().accept(tool, args, subject)
            if tool == "delete_note":
                out["note"] = {"title": args["name"], "folder": "Notes", "preview": "first"}
            return out

    tools = _tools(queue, applier=ConfirmingApplier(queue))
    out = _call(tools["delete_note"], name="Old plan")
    assert out["status"] == "queued"
    assert out["note"] == {"title": "Old plan", "folder": "Notes", "preview": "first"}
    assert "explicitly asked" in tools["delete_note"].description
    # Tools without a summary keep their shape.
    assert "note" not in _call(tools["add_note"], name="New", body="x")
