"""Drain worker for queued remote writes (issue #169)."""

from types import SimpleNamespace

import pytest

from apple_notes_mcp.remote.write_queue import WriteQueue, WriteQueueConfig
from apple_notes_mcp.remote.write_worker import WriteWorker


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class FakeApplier:
    def __init__(self, queue, on_apply=None):
        self.queue = queue
        self.applied = []
        self.on_apply = on_apply

    def apply(self, wid):
        if not self.queue.claim(wid):
            return self.queue.get(wid)["status"]
        self.applied.append(wid)
        if self.on_apply:
            self.on_apply(wid)
        self.queue.finish(wid, "applied")
        return "applied"


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"), clock=clock)
    session = SimpleNamespace(unlocked=False)
    activated, reports = [], []
    front = SimpleNamespace(bid="com.mitchellh.ghostty")
    applier = FakeApplier(queue)
    worker = WriteWorker(
        queue,
        applier,
        delay=60,
        clock=clock,
        session_probe=lambda: session.unlocked,
        modal_probe=lambda: None,
        frontmost=lambda: front.bid,
        activate=activated.append,
        report=lambda exc: reports.append(exc),
    )
    return SimpleNamespace(
        clock=clock, queue=queue, session=session, applier=applier, worker=worker,
        activated=activated, reports=reports, front=front,
    )


def _queue(env, title="Daily"):
    return env.queue.admit(
        dict(tool="update_note_content", operation="append",
             args={"name": title, "body": "b", "operation": "append"}, note_title=title)
    )


def test_nothing_applies_while_locked(env):
    _queue(env)
    assert env.worker.run_once() == []
    assert env.applier.applied == []


def test_accepted_while_unlocked_applies_in_the_same_pass(env):
    env.session.unlocked = True
    env.worker.run_once()  # notices the unlock
    env.clock.now += 1
    wid = _queue(env)
    assert env.worker.run_once() == [wid]


def test_queued_while_locked_waits_sixty_seconds_after_unlock(env):
    wid = _queue(env)
    env.clock.now += 3600
    env.session.unlocked = True
    assert env.worker.run_once() == []
    env.clock.now += 59
    assert env.worker.run_once() == []
    env.clock.now += 1
    assert env.worker.run_once() == [wid]


def test_relocking_resets_the_unlock_timer(env):
    wid = _queue(env)
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 30
    env.session.unlocked = False
    env.worker.run_once()
    env.clock.now += 10
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 59
    assert env.worker.run_once() == []
    env.clock.now += 1
    assert env.worker.run_once() == [wid]


def test_strict_fifo_an_older_waiting_write_blocks_newer_ones(env):
    old = _queue(env)
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 10
    new = _queue(env)  # accepted while unlocked, but behind `old`
    assert env.worker.run_once() == []
    env.clock.now += 50
    assert env.worker.run_once() == [old, new]


def test_lock_mid_drain_stops_before_the_next_write(env):
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 1
    first, second = _queue(env), _queue(env, "Other")
    env.applier.on_apply = lambda wid: setattr(env.session, "unlocked", False)
    assert env.worker.run_once() == [first]
    assert env.queue.get(second)["status"] == "queued"


def test_drain_lock_held_elsewhere_skips_the_pass(env):
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 1
    _queue(env)
    with WriteQueue(env.queue.config).drain_lock() as got:
        assert got
        assert env.worker.run_once() == []


def test_paused_or_auto_apply_off_applies_nothing(env):
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 1
    _queue(env)
    env.queue.set_paused(True)
    assert env.worker.run_once() == []
    env.queue.set_paused(False)
    env.worker.auto_apply = False
    assert env.worker.run_once() == []


@pytest.mark.parametrize(
    "bid, restored",
    [
        ("com.mitchellh.ghostty", ["com.mitchellh.ghostty"]),
        ("com.apple.Notes", []),
        ("com.apple.loginwindow", []),
        ("", []),
        (None, []),
    ],
)
def test_frontmost_app_is_restored_except_notes_loginwindow_or_unknown(env, bid, restored):
    env.front.bid = bid
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 1
    _queue(env)
    env.worker.run_once()
    assert env.activated == restored


def test_recover_marks_stuck_applying_failed(env):
    wid = _queue(env)
    env.queue.claim(wid)
    env.worker.recover()
    assert env.queue.get(wid)["status"] == "failed"


def test_tick_reports_faults_and_keeps_going(env):
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 1
    _queue(env)

    def boom(wid):
        raise RuntimeError("drain bug")

    env.applier.apply = boom
    env.worker.tick()  # must not raise
    assert len(env.reports) == 1 and isinstance(env.reports[0], RuntimeError)


def test_tick_expires_old_writes(env):
    wid = _queue(env)
    env.clock.now += 48 * 3600 + 1
    env.worker.tick()
    assert env.queue.get(wid)["status"] == "expired"


def test_applies_hint(env):
    assert env.worker.applies_hint() == "1 min after the Mac is next unlocked"
    env.session.unlocked = True
    assert env.worker.applies_hint() == "now (Mac unlocked)"
    env.queue.set_paused(True)
    assert "paused" in env.worker.applies_hint()
    env.queue.set_paused(False)
    env.worker.auto_apply = False
    assert "remote-writes apply" in env.worker.applies_hint()


def test_pause_during_a_drain_stops_before_the_next_write(env):
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 1
    first, second = _queue(env), _queue(env, "Other")
    env.applier.on_apply = lambda wid: env.queue.set_paused(True)
    assert env.worker.run_once() == [first]
    assert env.queue.get(second)["status"] == "queued"


def test_restore_frontmost_swallows_activate_errors():
    from apple_notes_mcp.remote.write_worker import restore_frontmost

    def boom(bid):
        raise RuntimeError("no such app")

    restore_frontmost("com.example.gone", boom)  # must not raise
    seen = []
    restore_frontmost("com.apple.Notes", seen.append)
    restore_frontmost(None, seen.append)
    restore_frontmost("com.mitchellh.ghostty", seen.append)
    assert seen == ["com.mitchellh.ghostty"]


def test_applied_unverified_counts_as_applied(env):
    """#200: a partial write is done, not waiting; the pass moves on."""

    def apply(wid):
        env.queue.claim(wid)
        env.queue.finish(wid, "applied_unverified", "partial")
        return "applied_unverified"

    env.applier.apply = apply
    env.session.unlocked = True
    env.worker.run_once()
    env.clock.now += 1
    first, second = _queue(env), _queue(env, "Other")
    assert env.worker.run_once() == [first, second]


def test_an_open_notes_dialog_holds_the_queue_without_applying(tmp_path):
    """While a dialog is open in Notes the worker never starts a write
    (issue #199): no backup, no overlay, no focus switch every 5 s."""
    from apple_notes_mcp.remote.write_apply import MODAL_WAIT_REASON

    clock = Clock()
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"), clock=clock)
    applier = FakeApplier(queue)
    modal = SimpleNamespace(title="Delete Note?")
    fronted = []
    worker = WriteWorker(
        queue,
        applier,
        delay=60,
        clock=clock,
        session_probe=lambda: True,
        modal_probe=lambda: modal.title,
        frontmost=lambda: fronted.append(1) or "com.mitchellh.ghostty",
        activate=lambda bid: None,
        report=lambda exc: None,
    )
    worker.run_once()
    clock.now += 1
    wid = queue.admit(
        dict(tool="update_note_content", operation="append",
             args={"name": "Daily", "body": "b", "operation": "append"},
             note_title="Daily")
    )
    assert worker.run_once() == []
    assert applier.applied == [] and fronted == []
    rec = queue.get(wid)
    assert rec["status"] == "queued" and rec["reason"] == MODAL_WAIT_REASON
    modal.title = None
    assert worker.run_once() == [wid]
