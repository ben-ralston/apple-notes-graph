"""Exact-one-note and folder resolution for delete_note / move_note (#229, #230).

Headless: a small sqlite NoteStore with the real column names (the trash is
ZFOLDERTYPE 1, smart folders ZFOLDERTYPE 2, locked notes ZISPASSWORDPROTECTED).
"""

import sqlite3

import pytest

from apple_notes.database import NotesDatabase
from apple_notes.exceptions import (
    AmbiguousFolderError,
    AmbiguousNoteTitleError,
    EncryptedNoteError,
    FolderNotFoundError,
    NoteInTrashError,
    NoteNotFoundError,
)
from apple_notes.note_target import (
    coredata_id,
    preview_text,
    resolve_folder,
    resolve_unique_note,
)

STORE = "D80FAC5F-287C-4FF0-A5E9-C0CA1EEED270"
# 2024-01-02T00:00:00Z as a Cocoa timestamp
MODIFIED = 1704153600 - 978307200


@pytest.fixture
def db_path(tmp_path):
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
            ZISPASSWORDPROTECTED INTEGER
        )
        """
    )
    conn.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER, Z_UUID TEXT)")
    conn.execute("INSERT INTO Z_METADATA VALUES (1, ?)", (STORE,))
    folders = [
        (1, "Recently Deleted", None, 1),
        (2, "Notes", None, 0),
        (3, "Other", None, 0),
        (4, "🗄️ Archive", 3, 0),
        (5, "🧪 Drafts", 3, 0),
        (6, "★ Daily Tasks", None, 2),  # smart folder: can't hold notes
        (7, "Archive", 8, 0),  # Work/Archive
        (8, "Work", None, 0),
        (9, "Old", 1, 0),  # a folder inside the trash
    ]
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZPARENT, ZFOLDERTYPE)"
        " VALUES (?, ?, ?, ?)",
        folders,
    )
    notes = [
        (10, "U-10", "Groceries", 2, 0),
        (11, "U-11", "Twin", 2, 0),
        (12, "U-12", "twin", 4, 0),
        (13, "U-13", "Trashed", 1, 0),
        (14, "U-14", "Secret", 2, 1),
        (15, "U-15", "Live and trashed", 2, 0),
        (16, "U-16", "Live and trashed", 1, 0),
    ]
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT"
        " (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER, ZISPASSWORDPROTECTED, ZMODIFICATIONDATE1)"
        f" VALUES (?, ?, ?, ?, ?, {MODIFIED})",
        notes,
    )
    # A purged row (marked for deletion) never counts.
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER, ZMARKEDFORDELETION)"
        " VALUES (17, 'U-17', 'Groceries', 2, 1)"
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def db(db_path):
    with NotesDatabase(db_path) as d:
        yield d


class TestResolveUniqueNote:
    def test_exact_single_match(self, db):
        m = resolve_unique_note(db, "Groceries")
        assert (m.pk, m.uuid, m.title, m.folder) == (10, "U-10", "Groceries", "Notes")
        assert m.modified == "2024-01-02T00:00:00Z"
        assert not m.in_trash and not m.locked

    def test_missing_note_refused_with_zero_count(self, db):
        with pytest.raises(NoteNotFoundError, match="0 notes"):
            resolve_unique_note(db, "Nope")

    def test_duplicate_titles_refused_with_count_folders_and_dates(self, db):
        with pytest.raises(AmbiguousNoteTitleError) as info:
            resolve_unique_note(db, "Twin")
        msg = str(info.value)
        assert "2 notes" in msg
        assert "Notes" in msg and "Other/🗄️ Archive" in msg
        assert msg.count("2024-01-02T00:00:00Z") == 2
        assert {m.folder for m in info.value.matches} == {"Notes", "Other/🗄️ Archive"}

    def test_a_trashed_copy_counts_as_a_duplicate(self, db):
        # AppleScript addresses notes by title and could hit the trashed copy
        # (deleting it again is permanent), so refuse rather than guess.
        with pytest.raises(AmbiguousNoteTitleError, match="Recently Deleted"):
            resolve_unique_note(db, "Live and trashed")

    def test_note_in_recently_deleted_refused(self, db):
        with pytest.raises(NoteInTrashError, match="Recently Deleted"):
            resolve_unique_note(db, "Trashed")

    def test_locked_note_refused(self, db):
        with pytest.raises(EncryptedNoteError, match="locked"):
            resolve_unique_note(db, "Secret")

    def test_case_mismatch_refused_naming_the_exact_title(self, db):
        with pytest.raises(NoteNotFoundError, match="'Groceries'"):
            resolve_unique_note(db, "groceries")

    def test_works_without_the_password_column(self, tmp_path, db_path):
        # Older schemas / other fixtures: no ZISPASSWORDPROTECTED means unlocked.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "ALTER TABLE ZICCLOUDSYNCINGOBJECT DROP COLUMN ZISPASSWORDPROTECTED"
        )
        conn.commit()
        conn.close()
        with NotesDatabase(db_path) as d:
            assert resolve_unique_note(d, "Secret").locked is False


class TestResolveFolder:
    @pytest.mark.parametrize(
        "query, path",
        [
            ("Other/🗄️ Archive", "Other/🗄️ Archive"),  # full path
            ("other/🗄️ archive", "Other/🗄️ Archive"),  # case-insensitive
            ("🧪 Drafts", "Other/🧪 Drafts"),  # folder name
            ("Drafts", "Other/🧪 Drafts"),  # emoji prefix not needed
            ("Work/Archive", "Work/Archive"),
            ("Notes", "Notes"),
        ],
    )
    def test_matches(self, db, query, path):
        assert resolve_folder(db, query).path == path

    def test_first_matching_tier_decides(self, db):
        # Tiers: full path, then folder name, then name without its emoji
        # prefix. "Archive" is the exact name of Work/Archive only, so the
        # emoji-stripped "🗄️ Archive" (a later tier) never competes.
        assert resolve_folder(db, "Archive").path == "Work/Archive"

    def test_ambiguous_within_a_tier_refused(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZPARENT, ZFOLDERTYPE)"
            " VALUES (30, '🧪 Drafts', 8, 0)"
        )
        conn.commit()
        conn.close()
        with NotesDatabase(db_path) as d:
            with pytest.raises(AmbiguousFolderError) as info:
                resolve_folder(d, "Drafts")
        assert "Other/🧪 Drafts" in str(info.value)
        assert "Work/🧪 Drafts" in str(info.value)

    @pytest.mark.parametrize(
        "query", ["Nope", "Recently Deleted", "★ Daily Tasks", "Old", "Arch"]
    )
    def test_missing_trash_smart_and_partial_names_refused(self, db, query):
        with pytest.raises(FolderNotFoundError):
            resolve_folder(db, query)

    def test_never_creates_a_folder(self, db, db_path):
        with pytest.raises(FolderNotFoundError):
            resolve_folder(db, "Brand new")
        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM ZICCLOUDSYNCINGOBJECT WHERE ZTITLE2 IS NOT NULL"
        ).fetchone()[0]
        assert count == 9


def test_store_uuid_and_coredata_ids(db):
    assert db.store_uuid() == STORE
    assert coredata_id(STORE, "ICNote", 10) == f"x-coredata://{STORE}/ICNote/p10"
    assert coredata_id(STORE, "ICFolder", 4) == f"x-coredata://{STORE}/ICFolder/p4"
    with pytest.raises(ValueError):
        coredata_id('x" & do shell script "', "ICNote", 1)
    with pytest.raises(ValueError):
        coredata_id(STORE, "ICAccount", 1)


def test_preview_text_drops_the_title_and_caps_length():
    assert (
        preview_text("# Title\nfirst line\n\n  second   line")
        == "first line second line"
    )
    long = "Title\n" + "x" * 500
    out = preview_text(long)
    assert out.startswith("x" * 200) and len(out) <= 201
    assert preview_text("Title only") == ""


class TestReviewRound1:
    def test_trashed_twin_refusal_says_how_to_clear_it(self, db):
        with pytest.raises(AmbiguousNoteTitleError) as info:
            resolve_unique_note(db, "Live and trashed")
        assert "empty it from Recently Deleted" in str(info.value)

    def test_nfc_and_nfd_titles_count_as_the_same_title(self, db_path):
        import unicodedata

        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER) VALUES (?, ?, ?, 2)",
            [
                (40, "U-40", unicodedata.normalize("NFC", "Café")),
                (41, "U-41", unicodedata.normalize("NFD", "Café")),
            ],
        )
        conn.commit()
        conn.close()
        with NotesDatabase(db_path) as d:
            with pytest.raises(AmbiguousNoteTitleError, match="2 notes"):
                resolve_unique_note(d, unicodedata.normalize("NFC", "Café"))
