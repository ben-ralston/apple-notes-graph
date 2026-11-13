"""Notes in Recently Deleted must not appear in listings.

A soft delete (MCP delete_note / the UI delete button) moves the note to the
trash folder WITHOUT setting ZMARKEDFORDELETION - that flag only flips at the
30-day purge. Listings must therefore also exclude notes whose folder is the
trash (ZFOLDERTYPE = 1, locale-independent - the title 'Recently Deleted' is
localized).
"""

import sqlite3

import pytest

from apple_notes.database import NotesDatabase


@pytest.fixture
def fixture_db(tmp_path):
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
            ZMARKEDFORDELETION INTEGER DEFAULT 0
        )
        """
    )
    rows = [
        # folders: (pk, identifier, note_title, folder_title, folder, parent, type)
        (1, "TrashFolder-CloudKit", None, "Recently Deleted", None, None, 1),
        (2, "DefaultFolder-CloudKit", None, "Notes", None, None, 0),
        # notes
        (10, "UUID-ACTIVE", "Active note", None, 2, None, None),
        (11, "UUID-TRASHED", "Trashed note", None, 1, None, None),
    ]
    conn.executemany(
        """
        INSERT INTO ZICCLOUDSYNCINGOBJECT
            (Z_PK, ZIDENTIFIER, ZTITLE1, ZTITLE2, ZFOLDER, ZPARENT, ZFOLDERTYPE)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT"
        " (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER, ZMARKEDFORDELETION)"
        " VALUES (12, 'UUID-PURGED', 'Purged note', 2, 1)"
    )
    conn.commit()
    conn.close()
    return str(path)


def test_get_all_notes_excludes_trashed_and_purged(fixture_db):
    with NotesDatabase(fixture_db) as db:
        names = [n.name for n in db.get_all_notes()]
    assert names == ["Active note"]


def test_get_notes_paginated_excludes_trashed_and_purged(fixture_db):
    with NotesDatabase(fixture_db) as db:
        notes, total = db.get_notes_paginated(limit=10, offset=0)
    assert [n.name for n in notes] == ["Active note"]
    assert total == 1


def test_raw_note_links_skip_soft_deleted_rows(scoped_db):
    """A full-note paste re-creates every link and soft-deletes the old rows (#183).

    The stale rows (ZMARKEDFORDELETION = 1) must not surface as links, or a
    re-pointed link keeps showing up as a backlink to its old target.
    """
    import sqlite3

    from apple_notes.librarian import resolve_note_links

    conn = sqlite3.connect(scoped_db)
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE1, ZTYPEUTI1,"
        " ZTOKENCONTENTIDENTIFIER, ZALTTEXT, ZMARKEDFORDELETION)"
        " VALUES (30, 'L-STALE', 13, 'com.apple.notes.inlinetextattachment.link',"
        " 'applenotes:note/AAAA-0D01', 'Today', 1)"
    )
    conn.commit()
    conn.close()
    with NotesDatabase(scoped_db) as db:
        links = db.get_raw_note_links()
        notes = db.get_all_notes()
    assert all(link.source_pk != 13 for link in links)
    resolved, _broken = resolve_note_links(links, notes)
    assert all(link.source_pk != 13 for link in resolved)
