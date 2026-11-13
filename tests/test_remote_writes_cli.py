"""`apple-notes-mcp-remote writes …` and the serve-time worker (issue #169)."""

import json

import pytest

from apple_notes import folder_scope
from apple_notes_mcp.remote import cli
from apple_notes_mcp.remote.write_apply import WriteRefused
from apple_notes_mcp.remote.write_queue import WriteQueue, WriteQueueConfig


@pytest.fixture
def setup(tmp_path, monkeypatch):
    wq = tmp_path / "wq"
    config = tmp_path / "remote.json"
    config.write_text(
        json.dumps(
            {
                "public_url": "https://n.example.com",
                "allowed_github_login": "owner",
                "allowed_github_id": 1,
                "github_client_id": "x",
                "state_dir": str(tmp_path / "state"),
                "write_tools": ["update_note_content"],
                "write_queue": {"state_dir": str(wq)},
            }
        )
    )
    queue = WriteQueue(WriteQueueConfig(state_dir=wq))

    class FakeApplier:
        def __init__(self):
            self.applied, self.undone = [], []

        def apply(self, wid):
            queue.claim(wid)
            queue.finish(wid, "applied")
            self.applied.append(wid)
            return "applied"

        def undo(self, wid):
            if wid == "w_refuse":
                raise WriteRefused("the note changed after this write")
            self.undone.append(wid)
            return "undone"

    applier = FakeApplier()
    monkeypatch.setattr(cli, "_make_applier", lambda q: applier)
    monkeypatch.setattr(cli, "_session_available", lambda: True)
    monkeypatch.setattr(cli, "_restore_frontmost", lambda: (lambda: None))
    return config, queue, applier


def _add(queue, title="Daily", body="SECRET BODY"):
    return queue.admit(
        dict(tool="update_note_content", operation="append",
             args={"name": title, "body": body, "operation": "append"}, note_title=title)
    )


def run(config, *args):
    return cli.main(["--config", str(config), "writes", *args])


def test_list_shows_status_and_title_but_never_args(setup, capsys):
    config, queue, _ = setup
    wid = _add(queue)
    assert run(config, "list") == 0
    out = capsys.readouterr().out
    assert wid in out and "queued" in out and "Daily" in out and "append" in out
    assert "SECRET BODY" not in out


def test_pause_resume_clear(setup, capsys):
    config, queue, _ = setup
    assert run(config, "pause") == 0 and queue.paused()
    assert run(config, "resume") == 0 and not queue.paused()
    wid = _add(queue)
    assert run(config, "clear") == 0
    assert queue.get(wid)["status"] == "rejected"
    assert "1" in capsys.readouterr().out


def test_apply_all_is_fifo_and_apply_one(setup, capsys):
    config, queue, applier = setup
    a, b = _add(queue), _add(queue, "Other")
    assert run(config, "apply", a) == 0
    assert applier.applied == [a]
    assert run(config, "apply", "--all") == 0
    assert applier.applied == [a, b]
    assert f"{b}: applied" in capsys.readouterr().out


def test_apply_needs_an_id_or_all(setup):
    config, _, _ = setup
    assert run(config, "apply") == 2


def test_apply_refuses_while_locked(setup, monkeypatch, capsys):
    config, queue, applier = setup
    _add(queue)
    monkeypatch.setattr(cli, "_session_available", lambda: False)
    assert run(config, "apply", "--all") == 1
    assert applier.applied == [] and "locked" in capsys.readouterr().err


def test_apply_refuses_while_another_drain_holds_the_lock(setup, capsys):
    config, queue, applier = setup
    _add(queue)
    with WriteQueue(queue.config).drain_lock():
        assert run(config, "apply", "--all") == 1
    assert applier.applied == [] and "already applying" in capsys.readouterr().err


def test_undo_success_and_refusal(setup, capsys):
    config, _, applier = setup
    assert run(config, "undo", "w_ok") == 0 and applier.undone == ["w_ok"]
    assert run(config, "undo", "w_refuse") == 1
    assert "changed after" in capsys.readouterr().err


def test_check_config_lists_queued_tools_without_touching_the_real_queue(setup, capsys, tmp_path):
    config, _, _ = setup
    data = json.loads(config.read_text())
    data["write_queue"]["state_dir"] = str(tmp_path / "never-created")
    config.write_text(json.dumps(data))
    secrets = tmp_path / "remote-secrets.json"
    secrets.write_text('{"github_client_secret": "s"}')
    secrets.chmod(0o600)
    try:
        assert cli.main(["--config", str(config), "check-config"]) == 0
    finally:
        folder_scope.reset()
    out = capsys.readouterr().out
    assert "get_write_status" in out and "update_note_content" in out
    assert "write queue:" in out and str(tmp_path / "never-created") in out
    assert not (tmp_path / "never-created").exists()


def test_serve_starts_the_write_worker(monkeypatch):
    import uvicorn

    from apple_notes_mcp.remote import app as remote_app
    from apple_notes_mcp.remote import request_log
    from apple_notes_mcp.remote.config import RemoteConfig

    class Worker:
        started = False

        def start(self):
            Worker.started = True

    class App:
        write_worker = Worker()

    cfg = RemoteConfig(public_url="https://n.example.com", allowed_github_login="owner", allowed_github_id=1, github_client_id="x")
    monkeypatch.setattr(cli, "load_remote_config", lambda path: cfg)
    monkeypatch.setattr(remote_app, "build_remote_app", lambda c: App())
    monkeypatch.setattr(request_log, "configure_remote_logging", lambda *a: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    assert cli.main(["serve"]) == 0
    assert Worker.started


def test_apply_runs_in_remote_mode_and_expires_overdue_writes_first(setup, monkeypatch):
    config, queue, applier = setup
    seen = []
    real_apply = applier.apply
    applier.apply = lambda wid: (seen.append(folder_scope._remote_mode), real_apply(wid))[1]
    wid = _add(queue)
    stale = queue.admit(
        dict(tool="update_note_content", operation="append",
             args={"name": "Old", "body": "b", "operation": "append"}, note_title="Old")
    )
    queue.update(stale, expires_at=0)
    try:
        assert run(config, "apply", "--all") == 0
    finally:
        folder_scope.reset()
    assert seen == [True] and applier.applied == [wid]
    assert queue.get(stale)["status"] == "expired"


def test_undo_tool_error_is_reported_not_a_traceback(setup, capsys):
    from mcp.server.fastmcp.exceptions import ToolError

    config, _, applier = setup

    def boom(wid):
        raise ToolError("UI scripting failed: Notes is not frontmost")

    applier.undo = boom
    assert run(config, "undo", "w_x") == 1
    assert "not frontmost" in capsys.readouterr().err
