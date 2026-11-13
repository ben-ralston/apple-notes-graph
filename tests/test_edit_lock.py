"""Tests for the cross-process edit lock that serializes UI-scripting edits.

Apple Notes UI scripting is not re-entrant: overlapping edits steal focus
mid-keystroke and corrupt silently or hang (issue #96). `edit_lock` serializes
every UI-scripting edit across threads AND processes via `fcntl.flock` on a
well-known path. These tests exercise the lock directly (threads + a real
subprocess) and confirm the UI-scripting `execute_*` helpers adopt it while the
pure-AppleScript delete path stays lock-free.
"""

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from apple_notes import edit_lock as edit_lock_module
from apple_notes import session_lock, ui_operations
from apple_notes.config import CONFIG_DIR
from apple_notes.edit_lock import EDIT_LOCK_TIMEOUT, LOCK_PATH, edit_lock
from apple_notes.exceptions import (
    AppleNotesError,
    EditLockTimeoutError,
    UIScriptingError,
)


class TestEditLockBasics:
    """Acquire/release semantics for a single caller."""

    def test_acquire_then_reacquire(self, tmp_path):
        lock_path = tmp_path / "edit.lock"
        with edit_lock(lock_path=lock_path):
            pass
        # Releasing on exit means the very next acquisition succeeds at once.
        with edit_lock(lock_path=lock_path, timeout=1):
            pass

    def test_released_when_body_raises(self, tmp_path):
        lock_path = tmp_path / "edit.lock"
        with pytest.raises(ValueError):
            with edit_lock(lock_path=lock_path):
                raise ValueError("boom")
        # The lock must be free despite the exception inside the body.
        with edit_lock(lock_path=lock_path, timeout=1):
            pass

    def test_creates_lock_file(self, tmp_path):
        lock_path = tmp_path / "edit.lock"
        assert not lock_path.exists()
        with edit_lock(lock_path=lock_path):
            assert lock_path.exists()

    def test_default_path_and_timeout_match_spec(self):
        # Issue #96: a well-known per-user path (the dir the daemon socket
        # already lives in, not world-writable /tmp) and a ~30s timeout.
        assert LOCK_PATH == CONFIG_DIR / "ui-edit.lock"
        assert EDIT_LOCK_TIMEOUT == 30.0


class TestEditLockTimeout:
    """Acquisition fails loudly instead of hanging when a holder is stuck."""

    def test_timeout_raises_when_held_by_another_process(self, tmp_path):
        lock_path = tmp_path / "edit.lock"
        ready = tmp_path / "ready"
        release = tmp_path / "release"
        holder = tmp_path / "holder.py"
        holder.write_text(
            "import fcntl, os, sys, time\n"
            "lock_path, ready_path, release_path = sys.argv[1:4]\n"
            "fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(ready_path, 'w').close()\n"
            "deadline = time.monotonic() + 30\n"
            "while time.monotonic() < deadline and not os.path.exists(release_path):\n"
            "    time.sleep(0.05)\n"
            "fcntl.flock(fd, fcntl.LOCK_UN)\n"
            "os.close(fd)\n"
        )

        proc = subprocess.Popen(
            [sys.executable, str(holder), str(lock_path), str(ready), str(release)]
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert time.monotonic() < deadline, "holder never acquired the lock"
                time.sleep(0.02)

            start = time.monotonic()
            with pytest.raises(EditLockTimeoutError):
                with edit_lock(timeout=0.4, lock_path=lock_path):
                    pass
            # Timed out promptly near the deadline, did not hang.
            assert time.monotonic() - start < 5
        finally:
            release.write_text("go")
            proc.wait(timeout=10)

    def test_timeout_error_message_is_actionable(self, tmp_path):
        lock_path = tmp_path / "edit.lock"
        # Hold the lock on a separate fd in this process so a non-blocking
        # acquisition cannot succeed, then assert the timeout message is clear.
        holder_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        import fcntl

        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        try:
            with pytest.raises(EditLockTimeoutError) as exc_info:
                with edit_lock(timeout=0.2, lock_path=lock_path):
                    pass
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)
        message = str(exc_info.value)
        assert "edit lock" in message.lower()
        assert str(lock_path) in message

    def test_timeout_error_is_ui_scripting_error(self):
        # UIScriptingError (a subclass of AppleNotesError) is the right home:
        # the daemon's `except AppleNotesError` still catches it AND server.py's
        # write tools, which catch UIScriptingError specifically, surface the
        # actionable timeout message instead of letting it propagate uncaught.
        assert issubclass(EditLockTimeoutError, UIScriptingError)
        assert issubclass(EditLockTimeoutError, AppleNotesError)

    def test_remote_path_preserves_timeout_type(self):
        # The daemon serializes the exception type by name; the client must map
        # it back so a remote timeout surfaces as EditLockTimeoutError, not a
        # generic AppleNotesError.
        from apple_notes.ui_helper_client import _ERROR_TYPE_MAP

        assert _ERROR_TYPE_MAP["EditLockTimeoutError"] is EditLockTimeoutError


class TestEditLockConcurrency:
    """The concurrent-edit scenario: overlapping callers are serialized."""

    def test_concurrent_threads_never_overlap(self, tmp_path):
        lock_path = tmp_path / "edit.lock"
        n_threads = 6
        state = {"current": 0, "max": 0}
        state_lock = threading.Lock()
        errors: list[Exception] = []
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            try:
                barrier.wait()  # release all threads at once to force contention
                with edit_lock(lock_path=lock_path, timeout=20):
                    with state_lock:
                        state["current"] += 1
                        state["max"] = max(state["max"], state["current"])
                    time.sleep(0.02)  # stay in the critical section long enough
                    with state_lock:  # to expose any overlap
                        state["current"] -= 1
            except Exception as exc:  # pragma: no cover - surfaced via assert
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert state["max"] == 1, "two edits held the lock simultaneously"


class TestEditLockRecordsLockWait:
    """edit_lock feeds acquisition wait into the active profile (issue #98)."""

    def test_records_lock_wait_when_profile_active(self, tmp_path):
        from apple_notes import profiling

        lock_path = tmp_path / "edit.lock"
        with profiling.profile("test") as prof:
            with edit_lock(lock_path=lock_path):
                pass
        assert "lock_wait" in prof.timing_ms
        assert prof.timing_ms["lock_wait"] >= 0

    def test_no_error_without_active_profile(self, tmp_path):
        # record() is a no-op when no profile is active — acquisition still works.
        lock_path = tmp_path / "edit.lock"
        with edit_lock(lock_path=lock_path):
            pass

    def test_records_lock_wait_on_timeout(self, tmp_path):
        import fcntl

        from apple_notes import profiling

        lock_path = tmp_path / "edit.lock"
        holder_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        try:
            with profiling.profile("test") as prof:
                with pytest.raises(EditLockTimeoutError):
                    with edit_lock(timeout=0.2, lock_path=lock_path):
                        pass
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)
        assert "lock_wait" in prof.timing_ms


class TestUIOperationsAdoptEditLock:
    """UI-scripting writes hold the lock; the AppleScript delete path does not."""

    @contextmanager
    def _spy_lock(self, entries: list[bool]):
        @contextmanager
        def fake_lock(*args, **kwargs):
            entries.append(True)
            yield

        with patch.object(edit_lock_module, "edit_lock", side_effect=fake_lock):
            yield

    @pytest.fixture(autouse=True)
    def _mock_executor_and_clipboard(self):
        with (
            patch("apple_notes.ui_operations.UIScriptingExecutor") as executor,
            patch("apple_notes.ui_operations.ClipboardManager"),
            # Content verification (#200) is covered in test_content_verify.py;
            # these tests are about the lock, so stub it out.
            patch("apple_notes.ui_operations._verify_content"),
        ):
            executor.return_value._note_body_html.side_effect = ["before", "after"]
            executor.return_value._note_exists.return_value = True
            yield executor

    def test_execute_append_to_note_holds_edit_lock(self):
        entries: list[bool] = []
        with self._spy_lock(entries):
            ui_operations.execute_append_to_note(
                name="n", richtext_data=b"x", pending_actions=[]
            )
        assert entries == [True]

    def test_execute_create_note_holds_edit_lock(self):
        entries: list[bool] = []
        with self._spy_lock(entries):
            ui_operations.execute_create_note(
                name="n", richtext_data=b"x", folder="Notes", pending_actions=[]
            )
        assert entries == [True]

    def test_execute_delete_note_does_not_hold_edit_lock(self):
        entries: list[bool] = []
        with self._spy_lock(entries):
            ui_operations.execute_delete_note(name="n")
        assert entries == []

    def test_preflight_runs_inside_edit_lock(self):
        """The locked-session preflight must run *after* the lock is acquired.

        Otherwise a screen-lock during a up-to-30s lock wait would let the edit
        proceed on a stale preflight result and script into a locked session —
        regressing the issue #90 guard in exactly the contended case the lock
        is meant to handle.
        """
        events: list[str] = []

        @contextmanager
        def fake_lock(*args, **kwargs):
            events.append("lock_enter")
            yield

        with (
            patch.object(edit_lock_module, "edit_lock", side_effect=fake_lock),
            patch.object(
                session_lock,
                "require_ui_scripting_session",
                side_effect=lambda: events.append("preflight"),
            ),
        ):
            ui_operations.execute_append_to_note(
                name="n", richtext_data=b"x", pending_actions=[]
            )

        assert events == ["lock_enter", "preflight"]
