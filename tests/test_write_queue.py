"""WriteQueue store for remote writes (issue #169)."""

import os
import stat
import threading

import pytest

from apple_notes_mcp.remote.write_queue import (
    CapExceeded,
    WriteQueue,
    WriteQueueConfig,
)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cfg(tmp_path):
    return WriteQueueConfig(state_dir=tmp_path / "wq")


@pytest.fixture
def queue(cfg, clock):
    return WriteQueue(cfg, clock=clock)


def _rec(title="Daily", tool="update_note_content", op="append", body="x", **extra):
    args = {"name": title, "body": body, "operation": op}
    args.update(extra.pop("args", {}))
    return dict(
        tool=tool, operation=op, args=args, note_title=title, note_uuid="U-1", **extra
    )


def test_state_dir_and_file_are_private(queue, cfg):
    queue.admit(_rec())
    assert stat.S_IMODE(os.stat(cfg.state_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(cfg.state_dir / "queue.sqlite").st_mode) == 0o600


def test_admit_returns_queued_record_with_expiry(queue, clock):
    wid = queue.admit(_rec(subject="owner"))
    rec = queue.get(wid)
    assert wid.startswith("w_") and len(wid) == 14
    assert rec["status"] == "queued"
    assert rec["args"]["operation"] == "append"
    assert rec["expires_at"] == clock.now + 48 * 3600
    assert rec["subject"] == "owner"


def test_pending_is_fifo_and_queued_ahead_counts_earlier(queue, clock):
    a = queue.admit(_rec())
    clock.now += 1
    b = queue.admit(_rec(title="Other"))
    assert [r["id"] for r in queue.pending()] == [a, b]
    assert queue.queued_ahead(b) == 1 and queue.queued_ahead(a) == 0


def test_claim_is_atomic_across_threads(queue):
    wid = queue.admit(_rec())
    wins = []
    threads = [
        threading.Thread(target=lambda: wins.append(queue.claim(wid))) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
    assert queue.get(wid)["status"] == "applying"
    assert queue.claim(wid) is False


def test_finish_and_requeue(queue, clock):
    wid = queue.admit(_rec())
    queue.claim(wid)
    queue.requeue(wid, count_attempt=True)
    assert queue.get(wid)["status"] == "queued" and queue.get(wid)["attempts"] == 1
    queue.claim(wid)
    clock.now += 5
    queue.finish(wid, "applied", post_fingerprint="abc")
    rec = queue.get(wid)
    assert rec["status"] == "applied" and rec["applied_at"] == clock.now
    assert rec["post_fingerprint"] == "abc"
    with pytest.raises(ValueError):
        queue.finish(wid, "bogus")


@pytest.mark.parametrize(
    "rec, cap",
    [
        (_rec(body="y" * 20_001), "max_body_chars"),
        (_rec(op="find_replace", args={"old_text": "o" * 2_001}), "max_old_text_chars"),
    ],
)
def test_length_caps(queue, rec, cap):
    with pytest.raises(CapExceeded) as e:
        queue.admit(rec)
    assert e.value.cap == cap


def test_operation_allowlist(tmp_path, clock):
    q = WriteQueue(
        WriteQueueConfig(
            state_dir=tmp_path / "wq", allowed_operations=frozenset({"append"})
        ),
        clock=clock,
    )
    with pytest.raises(CapExceeded) as e:
        q.admit(_rec(op="replace"))
    assert e.value.cap == "allowed_operations"


def test_daily_cap_only_no_hourly_cap(tmp_path, clock):
    q = WriteQueue(
        WriteQueueConfig(state_dir=tmp_path / "wq", max_per_day=30, max_pending=40),
        clock=clock,
    )
    # A burst within one hour (past the old default hourly cap of 20) is
    # limited only by the daily cap.
    for i in range(30):
        q.admit(_rec(title=f"note {i}"))
    with pytest.raises(CapExceeded) as e:
        q.admit(_rec(title="one more"))
    assert e.value.cap == "max_per_day"
    clock.now += 86401
    q.admit(_rec(title="one more"))


def test_pending_caps_total_and_per_note(tmp_path, clock):
    q = WriteQueue(
        WriteQueueConfig(
            state_dir=tmp_path / "wq", max_pending=3, max_pending_per_note=2
        ),
        clock=clock,
    )
    q.admit(_rec(title="a"))
    q.admit(_rec(title="a"))
    with pytest.raises(CapExceeded) as e:
        q.admit(_rec(title="A"))  # same note, different case
    assert e.value.cap == "max_pending_per_note"
    q.admit(_rec(title="b"))
    with pytest.raises(CapExceeded) as e:
        q.admit(_rec(title="c"))
    assert e.value.cap == "max_pending"


def test_expire_and_purge(queue, clock):
    old = queue.admit(_rec())
    done = queue.admit(_rec(title="b"))
    queue.claim(done)
    queue.finish(done, "applied", prior_markdown="# b\nold body")
    clock.now += 48 * 3600 + 1
    assert queue.expire() == 1
    assert queue.get(old)["status"] == "expired"
    clock.now += 7 * 24 * 3600
    assert queue.purge() == 2
    assert queue.get(done)["args"] is None and queue.get(done)["prior_markdown"] is None
    assert queue.get(done)["status"] == "applied"


def test_purge_clears_post_section_text(queue, clock):
    wid = queue.admit(_rec())
    queue.claim(wid)
    queue.finish(wid, "applied", post_section_text="section body")
    clock.now += 7 * 24 * 3600 + 1
    assert queue.purge() == 1
    assert queue.get(wid)["post_section_text"] is None


def test_pause_resume_and_clear(queue):
    assert queue.paused() is False
    queue.set_paused(True)
    assert queue.paused() is True
    queue.set_paused(False)
    a = queue.admit(_rec())
    b = queue.admit(_rec(title="b"))
    queue.claim(b)
    assert queue.clear() == 1
    assert queue.get(a)["status"] == "rejected"
    assert queue.get(b)["status"] == "applying"


def test_drain_lock_is_exclusive_and_non_blocking(queue):
    with queue.drain_lock() as first:
        assert first is True
        with WriteQueue(queue.config).drain_lock() as second:
            assert second is False
    with queue.drain_lock() as again:
        assert again is True


def test_stuck_applying_becomes_failed_only_without_a_drain_lock(queue):
    wid = queue.admit(_rec())
    queue.claim(wid)
    with queue.drain_lock():
        assert WriteQueue(queue.config).fail_stuck_applying() == 0
    assert queue.fail_stuck_applying() == 1
    rec = queue.get(wid)
    assert rec["status"] == "failed" and "interrupted" in rec["reason"]


def test_list_is_newest_first_and_limited(queue, clock):
    ids = []
    for i in range(3):
        clock.now += 1
        ids.append(queue.admit(_rec(title=f"n{i}")))
    assert [r["id"] for r in queue.list(limit=2)] == [ids[2], ids[1]]


def test_claim_refuses_and_expires_an_overdue_write(queue, clock):
    wid = queue.admit(_rec())
    clock.now += 48 * 3600 + 1
    assert queue.claim(wid) is False
    assert queue.get(wid)["status"] == "expired"


def test_finish_applied_unverified_sets_applied_at(queue, clock):
    """#200: a write that landed in part is final, timestamped like applied."""
    wid = queue.admit(_rec())
    queue.claim(wid)
    clock.now += 3
    queue.finish(wid, "applied_unverified", "[[X]] not found", post_fingerprint="f")
    rec = queue.get(wid)
    assert rec["status"] == "applied_unverified"
    assert rec["applied_at"] == clock.now
    assert queue.pending() == []


def test_note_waiting_updates_only_a_still_queued_record(queue):
    """The drain worker records why a write is held (issue #199) without the
    status write `requeue` does: a record the owner cleared from another process
    mid-drain must stay rejected, not come back to life."""
    held, cleared = queue.admit(_rec()), queue.admit(_rec("Other"))
    queue.note_waiting(held, "waiting: dialog")
    assert queue.get(held)["status"] == "queued"
    assert queue.get(held)["reason"] == "waiting: dialog"
    queue.clear()
    queue.note_waiting(cleared, "waiting: dialog")
    assert queue.get(cleared)["status"] == "rejected"
    assert queue.get(cleared)["reason"] == "cleared by the owner"
