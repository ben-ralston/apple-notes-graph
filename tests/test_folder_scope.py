"""Remote-mode folder allowlist (issue #167, amendment 4).

The allowlist is enforced inside NotesDatabase, so every read tool - lists,
reads by name/pk/uuid, links, folder listings, note bodies - sees only the
allowed folders, whatever arguments the caller passes (``include_all`` cannot
bypass it). With no allowlist set (stdio mode) nothing changes.
"""



import pytest

from apple_notes import folder_scope
from apple_notes.database import NotesDatabase
from apple_notes.exceptions import NoteNotFoundError


@pytest.fixture(autouse=True)
def _reset_scope():
    folder_scope.reset()
    yield
    folder_scope.reset()


def _names(notes):
    return sorted(n.name for n in notes)


def test_no_allowlist_sees_everything(scoped_db):
    with NotesDatabase(scoped_db) as db:
        assert _names(db.get_all_notes()) == [
            "Archived day",
            "Last year",
            "Secret",
            "Today",
            "Today",
        ]


def test_folder_allowed_matches_exact_folder_and_subfolders_only():
    folder_scope.set_folder_allowlist(["daily"])
    assert folder_scope.folder_allowed("Daily")
    assert folder_scope.folder_allowed("Daily/Old")
    assert not folder_scope.folder_allowed("Daily Archive")
    assert not folder_scope.folder_allowed("Private")
    assert not folder_scope.folder_allowed("Other/Daily")


def test_empty_allowlist_is_rejected():
    with pytest.raises(ValueError):
        folder_scope.set_folder_allowlist([])


def test_get_all_notes_is_scoped(scoped_db):
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        assert _names(db.get_all_notes()) == ["Last year", "Today"]


def test_paginated_is_scoped_with_scoped_total(scoped_db):
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        notes, total = db.get_notes_paginated(limit=1, offset=0)
    assert total == 2
    assert [n.name for n in notes] == ["Last year"]


def test_get_note_by_name_hides_out_of_scope_and_prefers_in_scope_duplicate(scoped_db):
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        with pytest.raises(NoteNotFoundError):
            db.get_note_by_name("Secret")
        assert db.get_note_by_name("Today").pk == 10


def test_get_note_by_pk_and_uuid_hide_out_of_scope(scoped_db):
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        with pytest.raises(NoteNotFoundError):
            db.get_note_by_pk(12)
        assert db.get_note_by_uuid("AAAA-0B01") is None
        assert db.get_note_by_uuid("aaaa-0d01").pk == 10


def test_note_data_and_attachments_hidden_out_of_scope(scoped_db):
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        assert db.get_note_data(12) is None
        assert db.get_attachments_for_note(12) == {}
        assert db.get_note_data(10) == b"daily-bytes"


def test_raw_links_only_from_in_scope_sources(scoped_db):
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        sources = {link.source_pk for link in db.get_raw_note_links()}
        _notes, links = db.get_all_links()
    assert sources == {10}
    # Today -> Secret target is out of scope, so no resolved edge leaks it.
    assert links == []


def test_links_to_hidden_notes_are_not_reported_broken(scoped_db):
    # Today -> Secret points at a live note in a hidden folder: dropping it keeps
    # find_broken_links from reporting it (and leaking its title); the genuinely
    # dangling Today -> Gone link is still visible.
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        targets = [link.target_url for link in db.get_raw_note_links()]
    assert targets == ["applenotes:note/AAAA-DEAD"]


def test_folders_listing_is_scoped(scoped_db):
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        paths = sorted(f["full_path"] for f in db.get_all_folders())
    assert paths == ["Daily", "Daily/Old"]


def test_scope_parses_link_targets_exactly_like_the_librarian(scoped_db):
    # "...0B01Zjunk": the librarian's hex pattern resolves this to the hidden
    # note AAAA-0B01, so the scope filter must drop it too (no parser drift).
    import sqlite3

    conn = sqlite3.connect(scoped_db)
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE1, ZTYPEUTI1, ZTOKENCONTENTIDENTIFIER, ZALTTEXT)"
        " VALUES (23, 'L4', 10, 'com.apple.notes.inlinetextattachment.link', 'applenotes:note/AAAA-0B01Zjunk', 'Secret')"
    )
    conn.commit()
    conn.close()
    folder_scope.set_folder_allowlist(["Daily"])
    with NotesDatabase(scoped_db) as db:
        targets = [link.target_url for link in db.get_raw_note_links()]
    assert targets == ["applenotes:note/AAAA-DEAD"]



def test_remote_mode_never_serves_recently_deleted(scoped_db):
    # the owner's call: remote mode reads every folder (no allowlist) except the trash.
    folder_scope.set_remote_mode(True)
    with NotesDatabase(scoped_db) as db:
        with pytest.raises(NoteNotFoundError):
            db.get_note_by_name("Trashed secret")
        with pytest.raises(NoteNotFoundError):
            db.get_note_by_pk(15)
        assert db.get_note_by_uuid("AAAA-0E01") is None
        assert db.get_note_data(15) is None
        assert db.get_attachments_for_note(15) == {}
        assert "Recently Deleted" not in [f["full_path"] for f in db.get_all_folders()]
        assert 15 not in {link.source_pk for link in db.get_raw_note_links()}
        assert "Trashed secret" not in [n.name for n in db.get_notes_paginated()[0]]
        # hidden-by-config folders are still readable: only the trash is barred
        assert db.get_note_by_name("Secret").pk == 12


def test_stdio_mode_still_reads_trashed_notes_by_name(scoped_db):
    with NotesDatabase(scoped_db) as db:
        assert db.get_note_by_name("Trashed secret").pk == 15
        assert db.get_note_by_pk(15).name == "Trashed secret"


def test_remote_mode_hides_folders_nested_under_recently_deleted(scoped_db):
    import sqlite3

    conn = sqlite3.connect(scoped_db)
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE2, ZPARENT, ZFOLDERTYPE)"
        " VALUES (6, 'F-TRASH-SUB', 'Old stuff', 5, 0)"
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER)"
        " VALUES (16, 'AAAA-0E02', 'Nested trashed', 6)"
    )
    conn.commit()
    conn.close()
    folder_scope.set_remote_mode(True)
    with NotesDatabase(scoped_db) as db:
        assert "Nested trashed" not in [n.name for n in db.get_all_notes()]
        with pytest.raises(NoteNotFoundError):
            db.get_note_by_name("Nested trashed")
        with pytest.raises(NoteNotFoundError):
            db.get_note_by_pk(16)
        assert db.get_note_by_uuid("AAAA-0E02") is None
        assert not [f for f in db.get_all_folders() if "Recently Deleted" in f["full_path"]]
