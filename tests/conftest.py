"""Shared pytest fixtures for the apple_notes test suite."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def frontmost_clock():
    """Deterministic wall clock for the frontmost settle-poll (issue #141).

    `_ensure_notes_frontmost` bounds itself by `time.monotonic()` rather than
    by an attempt count, so a test that only patches `time.sleep` to a no-op
    would busy-spin against the real clock for the whole timeout. This swaps
    the `time` module inside `apple_notes.ui_scripting` for a fake whose
    `sleep()` advances a virtual clock: the poll advances exactly as far as it
    sleeps, and a test can charge per-probe latency with `clock.advance()`.

    Only `sleep`/`monotonic` are provided — if `ui_scripting` starts using
    another `time` function, those tests fail loudly rather than silently
    reading the real clock.
    """
    from apple_notes import ui_scripting

    yield from _fake_clock(ui_scripting)


@pytest.fixture
def verify_clock():
    """Deterministic wall clock for the verify-after-write read-back poll
    (issue #177) — `frontmost_clock`, but for `apple_notes.ui_operations`.

    `_verify_content` re-reads the note on a `time.monotonic()` deadline, so a
    test whose read-back never changes would otherwise really sleep for the
    whole budget.
    """
    from apple_notes import ui_operations

    yield from _fake_clock(ui_operations)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def _fake_clock(module):
    """Swap `module.time` for a FakeClock exposing only sleep/monotonic."""
    clock = FakeClock()
    fake_time = SimpleNamespace(sleep=clock.sleep, monotonic=clock.monotonic)
    with patch.object(module, "time", fake_time):
        yield clock


@pytest.fixture(autouse=True)
def bypass_session_lock_preflight():
    """Default the session-lock preflight to a no-op for every test.

    Write-path tests throughout the suite mock out `_is_gui_session` and the
    `execute_*` helpers but do not know about the preflight the daemon and
    `ui_operations` run before any UI work. Without this autouse fixture, any
    test that reaches the real preflight would depend on the live machine
    state (locked vs unlocked). Tests that specifically exercise the locked
    case override this fixture inline with their own patch.

    Relies on callers using `from . import session_lock` + attribute access
    (`session_lock.require_ui_scripting_session()`). If anyone refactors
    `ui_operations` or `ui_helper_daemon` to `from .session_lock import
    require_ui_scripting_session`, this patch silently stops neutralizing the
    preflight and tests start depending on live machine state.
    """
    with patch(
        "apple_notes.session_lock.require_ui_scripting_session",
        return_value=None,
    ):
        yield


@pytest.fixture(autouse=True)
def isolate_edit_lock(tmp_path_factory):
    """Redirect the cross-process edit lock to a per-test temp file.

    Write-path tests reach the real ``edit_lock`` through ``ui_operations``.
    Pointing ``LOCK_PATH`` at an isolated temp file keeps the suite off the
    shared /tmp lock — no pollution, and no contention with a real running
    daemon or parallel test runs — while still exercising the real flock
    mechanism. The edit_lock unit tests pass an explicit ``lock_path``, and the
    default-path assertion reads the imported constant directly, so neither is
    affected by this patch.
    """
    from apple_notes import edit_lock

    lock_path = tmp_path_factory.mktemp("edit-lock") / "apple-notes-edit.lock"
    with patch.object(edit_lock, "LOCK_PATH", lock_path):
        yield


@pytest.fixture(autouse=True)
def isolate_timing_log(tmp_path_factory):
    """Redirect the profiler's timing log to a per-session temp file.

    Server/daemon tool tests now run through the profiler, which appends to
    ``timing.log``. Without this, the suite would scribble hundreds of lines
    into the real ~/Library log. Profiler unit tests that assert on content
    set their own path, which overrides this within their scope.
    """
    from apple_notes import profiling

    log_path = tmp_path_factory.mktemp("timing-log") / "timing.log"
    with patch.object(profiling, "TIMING_LOG_PATH", log_path):
        yield


@pytest.fixture(autouse=True)
def disable_unified_log(request, monkeypatch):
    """Keep the unified-log bridge (#147) inert for every test that doesn't
    opt in. Tool/daemon tests drive real error outcomes through the profiler,
    whose hook would otherwise spawn delivery threads against the real shared
    queue at ~/Library/Application Support/notes-graph/logger-queue.jsonl. ``tests/test_unified_log.py``
    opts back in via its own autouse fixture (which also redirects the queue).
    The process-level configure() state is restored after every test: the
    remote ``serve`` path switches it to redacted remote mode (#170).
    """
    from apple_notes import unified_log

    saved = (unified_log._component, unified_log._enqueue_only, unified_log._redact)
    if request.node.fspath.basename != "test_unified_log.py":
        monkeypatch.setenv("NOTES_GRAPH_UNIFIED_LOG", "0")
    yield
    unified_log.configure(component=saved[0], enqueue_only=saved[1], redact=saved[2])


@pytest.fixture(autouse=True)
def disable_write_indicator(monkeypatch):
    """Keep unit tests from spawning real overlay subprocesses (issue #108).

    Write-path tests reach the real ``write_indicator`` through
    ``ui_operations``; without this kill-switch each one would spawn an AppKit
    overlay helper. ``tests/test_write_indicator.py`` re-enables it with its
    own file-scoped autouse fixture.
    """
    monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR", "0")


@pytest.fixture(autouse=True)
def isolate_daemon_pid_file(monkeypatch, tmp_path):
    """Test daemons get a temp socket; without this they share the live PID file."""
    from apple_notes import ui_helper_daemon

    monkeypatch.setattr(ui_helper_daemon, "PID_FILE", tmp_path / "ui-helper.pid")


@pytest.fixture
def scoped_db(tmp_path):
    """Notes DB with Daily, Daily/Old, Private and "Daily Archive" folders (#167)."""
    import sqlite3

    path = tmp_path / "NoteStore.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY,
            ZIDENTIFIER TEXT,
            ZTITLE1 TEXT,
            ZTITLE2 TEXT,
            ZFOLDER INTEGER,
            ZPARENT INTEGER,
            ZFOLDERTYPE INTEGER,
            ZCREATIONDATE3 REAL,
            ZMODIFICATIONDATE1 REAL,
            ZLASTOPENEDDATE REAL,
            ZMARKEDFORDELETION INTEGER DEFAULT 0,
            ZNOTE1 INTEGER,
            ZTYPEUTI1 TEXT,
            ZTOKENCONTENTIDENTIFIER TEXT,
            ZALTTEXT TEXT
        )
        """
    )
    conn.execute("CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB)")
    folders = [
        (1, "F-DAILY", "Daily", None),
        (2, "F-DAILY-OLD", "Old", 1),  # Daily/Old
        (3, "F-PRIVATE", "Private", None),
        (4, "F-DAILYISH", "Daily Archive", None),  # must NOT match "Daily"
    ]
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE2, ZPARENT, ZFOLDERTYPE)"
        " VALUES (?, ?, ?, ?, 0)",
        folders,
    )
    notes = [
        (10, "AAAA-0D01", "Today", 1),
        (11, "AAAA-0D02", "Last year", 2),
        (12, "AAAA-0B01", "Secret", 3),
        (13, "AAAA-0A01", "Archived day", 4),
        (14, "AAAA-0B02", "Today", 3),  # duplicate title in a hidden folder
    ]
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER) VALUES (?, ?, ?, ?)",
        notes,
    )
    conn.executemany(
        "INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (?, ?)",
        [(10, b"daily-bytes"), (12, b"secret-bytes")],
    )
    # links: Today -> Secret, Secret -> Today
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE1, ZTYPEUTI1, ZTOKENCONTENTIDENTIFIER, ZALTTEXT)"
        " VALUES (?, ?, ?, 'com.apple.notes.inlinetextattachment.link', ?, ?)",
        [
            (20, "L1", 10, "applenotes:note/AAAA-0B01", "Secret"),
            (21, "L2", 12, "applenotes:note/AAAA-0D01", "Today"),
            (22, "L3", 10, "applenotes:note/AAAA-DEAD", "Gone"),  # dangling
        ],
    )
    # Recently Deleted (ZFOLDERTYPE 1) holding a trashed note that links out
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE2, ZFOLDERTYPE)"
        " VALUES (5, 'F-TRASH', 'Recently Deleted', 1)"
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER)"
        " VALUES (15, 'AAAA-0E01', 'Trashed secret', 5)"
    )
    conn.execute("INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (15, X'00')")
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE1, ZTYPEUTI1, ZTOKENCONTENTIDENTIFIER, ZALTTEXT)"
        " VALUES (24, 'L5', 15, 'com.apple.notes.inlinetextattachment.link', 'applenotes:note/AAAA-0D01', 'Today')"
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def stub_note_resolution():
    """Bypass the server's title-or-id resolution (#83) in tool tests that mock
    everything below the tool. Resolution itself is covered against a real
    sqlite NoteStore in test_duplicate_titles.py; here a title resolves to a
    unique note of that title, so these tests keep testing their own layer."""
    from apple_notes.models import Note
    from apple_notes_mcp import server

    def resolve_read(ref, **_):
        return Note(name=ref, uuid="UUID-STUB", folder="Notes", pk=1), []

    with patch.object(server, "_resolve_read", side_effect=resolve_read), patch.object(
        server, "_write_title", side_effect=lambda ref, **_: ref
    ):
        yield


@pytest.fixture(autouse=True)
def _no_trashed_twin_lookup(request):
    """ui_scripting looks a write's title up in NoteStore to address the live
    note by id beside a trashed twin (#83). Mocked keystroke tests must never
    read the real NoteStore, so the lookup reports "no twin" (address by
    title, as before) unless a test patches it itself. The lookup's logic is
    tested on a fake NoteStore in test_duplicate_titles.py."""
    from apple_notes import ui_scripting

    with patch.object(ui_scripting, "_live_note_id", return_value=None):
        yield
