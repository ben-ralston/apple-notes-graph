"""Library helpers for the remote write queue (issue #169).

- session_lock.ui_session_available: non-raising lock probe for the drain loop.
- NotesDatabase.count_notes_titled: how many notes `show note "<title>"` could
  open (case-insensitive, Recently Deleted included, folder scope ignored).
- NotesDatabase.note_fingerprint: change detector for queued writes.
"""

import sqlite3

import pytest

from apple_notes import folder_scope, session_lock
from apple_notes.database import NotesDatabase

UNLOCKED = {
    "kCGSSessionOnConsoleKey": True,
    "kCGSessionLoginDoneKey": True,
    "CGSSessionScreenIsLocked": False,
}


@pytest.mark.parametrize(
    "info, expected",
    [
        (UNLOCKED, True),
        ({**UNLOCKED, "CGSSessionScreenIsLocked": True}, False),
        ({**UNLOCKED, "kCGSSessionOnConsoleKey": False}, False),
        ({**UNLOCKED, "kCGSessionLoginDoneKey": False}, False),
        (None, False),
    ],
)
def test_ui_session_available(monkeypatch, info, expected):
    monkeypatch.setattr(session_lock, "CGSessionCopyCurrentDictionary", lambda: info)
    assert session_lock.ui_session_available() is expected


@pytest.fixture
def db(scoped_db):
    conn = sqlite3.connect(scoped_db)
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER)"
        " VALUES (16, 'AAAA-0S01', 'Straße', 1)"
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER, ZMARKEDFORDELETION)"
        " VALUES (17, 'AAAA-0P01', 'Purged', 1, 1)"
    )
    conn.execute(
        "UPDATE ZICCLOUDSYNCINGOBJECT SET ZMODIFICATIONDATE1 = 100.0 WHERE Z_PK = 10"
    )
    conn.commit()
    conn.close()
    yield scoped_db
    folder_scope.reset()


def test_count_notes_titled_is_case_insensitive_and_ignores_folder_scope(db):
    folder_scope.set_folder_allowlist(["Daily"])  # "Today" #14 lives in Private
    with NotesDatabase(db) as d:
        assert d.count_notes_titled("today") == 2
        assert d.count_notes_titled("Nope") == 0


def test_count_notes_titled_includes_recently_deleted_even_in_remote_mode(db):
    folder_scope.set_remote_mode(True)
    with NotesDatabase(db) as d:
        assert d.count_notes_titled("TRASHED SECRET") == 1


def test_count_notes_titled_folds_unicode_and_skips_purged(db):
    with NotesDatabase(db) as d:
        assert d.count_notes_titled("STRASSE") == 1
        assert d.count_notes_titled("purged") == 0


def test_note_fingerprint_tracks_data_and_modification_date(db):
    with NotesDatabase(db) as d:
        first = d.note_fingerprint(10)
        assert first and first == d.note_fingerprint(10)
        assert d.note_fingerprint(999) is None
    conn = sqlite3.connect(db)
    conn.execute("UPDATE ZICNOTEDATA SET ZDATA = X'01' WHERE ZNOTE = 10")
    conn.commit()
    with NotesDatabase(db) as d:
        second = d.note_fingerprint(10)
    conn.execute(
        "UPDATE ZICCLOUDSYNCINGOBJECT SET ZMODIFICATIONDATE1 = 200.0 WHERE Z_PK = 10"
    )
    conn.commit()
    conn.close()
    with NotesDatabase(db) as d:
        third = d.note_fingerprint(10)
    assert len({first, second, third}) == 3
