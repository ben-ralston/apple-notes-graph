"""Duplicate note titles (#83).

the owner's decision: when a title matches several notes a read returns the match
plus the other matches (folder, id, modified); a note id is accepted wherever a
title is; local writes refuse an ambiguous title as remote writes already do;
a [[link]] to a title several notes share is refused and names the matches.

Headless: a small sqlite NoteStore with the real column names and real-format
note ids (uuids), so CI sees every case.
"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from apple_notes import folder_scope, write_service
from apple_notes.database import NotesDatabase
from apple_notes.exceptions import AmbiguousNoteTitleError, NoteNotFoundError
from apple_notes.models import GraphLink
from apple_notes.note_target import (
    is_note_id,
    live_note_id_beside_trashed_twins,
    require_unique_title,
    resolve_read_note,
    title_for_ref,
)
from apple_notes.richtext.section_fragment import FragmentError

COCOA = 978307200
OLDER = 1704153600 - COCOA  # 2024-01-02
NEWER = 1706745600 - COCOA  # 2024-02-01

U_SOLO = "1A2B3C4D-0000-4000-8000-000000000010"
U_TWIN_NOTES = "1A2B3C4D-0000-4000-8000-000000000011"
U_TWIN_WORK = "1A2B3C4D-0000-4000-8000-000000000012"
U_TWIN_CASE = "1A2B3C4D-0000-4000-8000-000000000013"
U_LT_LIVE = "1A2B3C4D-0000-4000-8000-000000000015"
U_LT_TRASH = "1A2B3C4D-0000-4000-8000-000000000016"
U_CAFE_NFC = "1A2B3C4D-0000-4000-8000-000000000021"
U_CAFE_NFD = "1A2B3C4D-0000-4000-8000-000000000022"
U_NOT_A_NOTE = "1A2B3C4D-0000-4000-8000-000000000030"
U_TRASH_A = "1A2B3C4D-0000-4000-8000-000000000041"
U_TRASH_B = "1A2B3C4D-0000-4000-8000-000000000042"
STORE = "D80FAC5F-287C-4FF0-A5E9-C0CA1EEED270"


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
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZPARENT, ZFOLDERTYPE)"
        " VALUES (?, ?, ?, ?)",
        [(1, "Recently Deleted", None, 1), (2, "Notes", None, 0), (3, "Work", None, 0)],
    )
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT"
        " (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER, ZMODIFICATIONDATE1)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (10, U_SOLO, "Solo", 2, OLDER),
            (11, U_TWIN_NOTES, "Twin", 2, OLDER),
            (12, U_TWIN_WORK, "Twin", 3, NEWER),
            (13, U_TWIN_CASE, "twin", 2, OLDER),
            (15, U_LT_LIVE, "Live and trashed", 2, OLDER),
            (16, U_LT_TRASH, "Live and trashed", 1, NEWER),
            # "Café" typed composed and decomposed: the same title to a person.
            (21, U_CAFE_NFC, "Café", 2, OLDER),
            (22, U_CAFE_NFD, "Café", 3, OLDER),
            # Two copies, both in Recently Deleted.
            (41, U_TRASH_A, "Gone twice", 1, OLDER),
            (42, U_TRASH_B, "Gone twice", 1, NEWER),
        ],
    )
    conn.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER, Z_UUID TEXT)")
    conn.execute("INSERT INTO Z_METADATA VALUES (1, ?)", (STORE,))
    # A non-note row with a uuid-shaped identifier and no note title (an
    # attachment or folder row lives in the same table).
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE2) VALUES (30, ?, 'Pics')",
        (U_NOT_A_NOTE,),
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def db(db_path):
    with NotesDatabase(db_path) as d:
        yield d


@pytest.fixture
def scoped():
    """Remote-mode folder scope: only 'Notes' is visible."""
    folder_scope.set_folder_allowlist(["Notes"])
    folder_scope.set_remote_mode(True)
    yield
    folder_scope.reset()


# --------------------------------------------------------------- note ids


def test_note_ids_are_uuids_only():
    assert is_note_id(U_SOLO)
    assert is_note_id(U_SOLO.lower())
    assert not is_note_id("Solo")
    assert not is_note_id("U-10")
    assert not is_note_id(" " + U_SOLO)


def test_title_for_ref_maps_an_id_to_its_title_and_leaves_titles_alone(db):
    assert title_for_ref(db, U_TWIN_WORK) == "Twin"
    assert title_for_ref(db, U_SOLO.lower()) == "Solo"
    assert title_for_ref(db, "Solo") == "Solo"
    # An id-shaped string that names no note is just a (missing) title.
    missing = "FFFFFFFF-0000-4000-8000-000000000099"
    assert title_for_ref(db, missing) == missing


# ------------------------------------------------------------------ reads


class TestResolveReadNote:
    def test_unique_title_has_no_other_matches(self, db):
        note, others = resolve_read_note(db, "Solo")
        assert note.pk == 10 and note.uuid == U_SOLO and others == []

    def test_shared_title_returns_the_newest_and_lists_the_rest(self, db):
        note, others = resolve_read_note(db, "Twin")
        assert note.pk == 12 and note.folder == "Work"
        # Case-differing titles are listed too: AppleScript and links match
        # titles case-insensitively.
        assert sorted(m.pk for m in others) == [11, 13]
        listed = {m.pk: m for m in others}
        assert listed[11].folder == "Notes" and listed[11].uuid == U_TWIN_NOTES
        assert listed[11].modified.startswith("2024-01-02")

    def test_a_live_note_beats_a_newer_copy_in_recently_deleted(self, db):
        note, others = resolve_read_note(db, "Live and trashed")
        assert note.pk == 15
        assert [m.pk for m in others] == [16]
        assert others[0].folder == "Recently Deleted"

    def test_an_id_reads_exactly_that_note_and_lists_its_twins(self, db):
        note, others = resolve_read_note(db, U_TWIN_NOTES)
        assert note.pk == 11 and note.folder == "Notes"
        assert sorted(m.pk for m in others) == [12, 13]

    def test_the_full_note_comes_back(self, db):
        note, _ = resolve_read_note(db, "Solo")
        assert note.name == "Solo" and note.folder == "Notes"
        assert note.modified.startswith("2024-01-02")

    def test_an_id_of_a_row_that_is_not_a_note_is_not_found(self, db):
        with pytest.raises(NoteNotFoundError):
            resolve_read_note(db, U_NOT_A_NOTE)
        assert title_for_ref(db, U_NOT_A_NOTE) == U_NOT_A_NOTE

    def test_case_insensitive_fallback_when_asked(self, db):
        note, others = resolve_read_note(db, "solo", case_insensitive=True)
        assert note.pk == 10 and others == []
        # An exact-case match still wins over the case-differing twin.
        note, _ = resolve_read_note(db, "twin", case_insensitive=True)
        assert note.pk == 13

    def test_missing_title_and_case_only_match_are_not_found(self, db):
        with pytest.raises(NoteNotFoundError):
            resolve_read_note(db, "Nope")
        with pytest.raises(NoteNotFoundError):
            resolve_read_note(db, "solo")

    def test_the_folder_scope_hides_twins_and_the_trash(self, db, scoped):
        note, others = resolve_read_note(db, "Twin")
        assert note.pk == 11  # the newer Work twin is out of scope
        assert [m.pk for m in others] == [13]
        note, others = resolve_read_note(db, "Live and trashed")
        assert note.pk == 15 and others == []
        with pytest.raises(NoteNotFoundError):
            resolve_read_note(db, U_TWIN_WORK)


# ----------------------------------------------------------------- writes


class TestRequireUniqueTitle:
    def test_unique_title_passes(self, db):
        require_unique_title(db, "Solo")

    def test_shared_title_refused_naming_each_match(self, db):
        with pytest.raises(AmbiguousNoteTitleError) as exc:
            require_unique_title(db, "Twin")
        message = str(exc.value)
        assert "3 notes are titled 'Twin'" in message
        for uuid in (U_TWIN_NOTES, U_TWIN_WORK, U_TWIN_CASE):
            assert uuid in message
        assert "folder 'Work'" in message
        assert "rename" in message.lower()

    def test_one_live_note_beside_a_trashed_copy_is_writable(self, db):
        # Not ambiguous: the write path addresses the live note by id (below).
        require_unique_title(db, "Live and trashed")

    def test_two_trashed_copies_and_no_live_note_stay_ambiguous(self, db):
        with pytest.raises(AmbiguousNoteTitleError, match="2 notes are titled 'Gone twice'"):
            require_unique_title(db, "Gone twice")


class TestLiveNoteIdBesideTrashedTwins:
    """The write path's AppleScript target when a trashed copy shares the title."""

    def test_the_live_note_is_addressed_by_its_applescript_id(self, db):
        assert (
            live_note_id_beside_trashed_twins(db, "Live and trashed")
            == f"x-coredata://{STORE}/ICNote/p15"
        )

    def test_no_trashed_twin_keeps_the_title(self, db):
        assert live_note_id_beside_trashed_twins(db, "Solo") is None
        assert live_note_id_beside_trashed_twins(db, "Nope") is None

    def test_live_twins_are_left_to_the_ambiguity_guard(self, db):
        assert live_note_id_beside_trashed_twins(db, "Twin") is None

    def test_missing_is_not_found(self, db):
        with pytest.raises(NoteNotFoundError):
            require_unique_title(db, "Nope")

    def test_a_case_only_mismatch_is_not_ambiguous(self, db):
        # One note, other case: AppleScript opens it by title as before #83.
        require_unique_title(db, "SOLO")

    def test_composed_and_decomposed_accents_are_one_title(self, db):
        with pytest.raises(AmbiguousNoteTitleError):
            require_unique_title(db, "Caf\u00e9")


# ------------------------------------------------------------------ links


class TestLinkTargets:
    def test_a_link_to_a_shared_title_is_refused_naming_the_matches(self, db_path):
        with pytest.raises(FragmentError) as exc:
            write_service.link_targets("see [[Twin]]", db_path=db_path)
        message = str(exc.value)
        assert "[[Twin]]" in message
        assert U_TWIN_NOTES in message and U_TWIN_WORK in message
        assert U_TWIN_CASE in message  # "twin" collides case-insensitively

    def test_a_link_to_a_unique_title_still_resolves(self, db_path):
        with patch.object(NotesDatabase, "link_owner_identifier", return_value="OWN"):
            links = write_service.link_targets("see [[Solo]]", db_path=db_path)
        assert links["solo"].uuid == U_SOLO

    def test_accent_twins_make_a_link_ambiguous(self, db_path):
        with pytest.raises(FragmentError) as exc:
            write_service.link_targets("[[Caf\u00e9]]", db_path=db_path)
        assert U_CAFE_NFC in str(exc.value) and U_CAFE_NFD in str(exc.value)

    def test_a_trashed_copy_does_not_make_a_link_ambiguous(self, db_path):
        with patch.object(NotesDatabase, "link_owner_identifier", return_value="OWN"):
            links = write_service.link_targets("[[Live and trashed]]", db_path=db_path)
        assert links["live and trashed"].uuid == U_LT_LIVE


# ---------------------------------------------------------- the MCP tools


@pytest.fixture
def server_db(db_path):
    def fixture_db(*_args, **_kwargs):
        return NotesDatabase(db_path)

    with (
        patch("apple_notes_mcp.server.NotesDatabase", fixture_db),
        patch("apple_notes.NotesDatabase", fixture_db),
        patch("apple_notes.write_service.NotesDatabase", fixture_db),
    ):
        yield db_path


def _content(name, folder="Notes"):
    return SimpleNamespace(
        name=name,
        folder=folder,
        created=None,
        modified=None,
        last_opened=None,
        resolved_text=f"{name}\nbody",
        raw_text=f"{name}\nbody",
        note_links=[],
        external_links=[],
        attachments=[],
    )


class TestReadTools:
    def test_read_note_content_lists_the_other_matches(self, server_db):
        from apple_notes_mcp import server

        with patch.object(
            server, "read_note", return_value=_content("Twin", "Work")
        ) as rn:
            result = server.read_note_content("Twin")
        assert rn.call_args.kwargs["note"].pk == 12
        assert result["id"] == U_TWIN_WORK
        others = {m["id"]: m for m in result["other_matches"]}
        assert set(others) == {U_TWIN_NOTES, U_TWIN_CASE}
        assert others[U_TWIN_NOTES] == {
            "id": U_TWIN_NOTES,
            "title": "Twin",
            "folder": "Notes",
            "modified": others[U_TWIN_NOTES]["modified"],
        }
        assert others[U_TWIN_NOTES]["modified"].startswith("2024-01-02")

    def test_read_note_content_by_id(self, server_db):
        from apple_notes_mcp import server

        with patch.object(server, "read_note", return_value=_content("Twin")) as rn:
            result = server.read_note_content(U_TWIN_NOTES)
        assert rn.call_args.kwargs["note"].pk == 11
        assert result["id"] == U_TWIN_NOTES

    def test_a_unique_title_has_an_empty_other_matches(self, server_db):
        from apple_notes_mcp import server

        with patch.object(server, "read_note", return_value=_content("Solo")):
            result = server.read_note_content("Solo")
        assert result["other_matches"] == [] and result["id"] == U_SOLO

    def test_get_backlinks_counts_links_to_that_note_only(self, server_db):
        from apple_notes_mcp import server

        links = [
            GraphLink("Solo", "Twin", 10, 11),  # to the Notes twin
            GraphLink("Live and trashed", "Twin", 15, 12),  # to the Work twin
        ]
        notes = [
            SimpleNamespace(name="Solo", folder="Notes", pk=10),
            SimpleNamespace(name="Live and trashed", folder="Notes", pk=15),
        ]
        with patch.object(NotesDatabase, "get_all_links", return_value=(notes, links)):
            result = server.get_backlinks(U_TWIN_NOTES)
        assert result["backlinks"] == ["Solo"]
        assert result["id"] == U_TWIN_NOTES
        assert {m["id"] for m in result["other_matches"]} == {U_TWIN_WORK, U_TWIN_CASE}

    def test_suggest_links_still_finds_a_title_case_insensitively(self, server_db):
        from apple_notes_mcp import server

        with patch.object(server, "get_link_suggestions", return_value={}) as gs:
            server.suggest_links("solo")
        assert gs.call_args.kwargs["note_pk"] == 10

    def test_suggest_links_addresses_the_resolved_note_by_pk(self, server_db):
        from apple_notes_mcp import server

        with patch.object(
            server, "get_link_suggestions", return_value={"note": {}}
        ) as gs:
            result = server.suggest_links(U_TWIN_NOTES)
        assert gs.call_args.kwargs["note_pk"] == 11
        assert {m["id"] for m in result["other_matches"]} == {U_TWIN_WORK, U_TWIN_CASE}

    def test_export_note_by_id(self, server_db, tmp_path):
        from apple_notes_mcp import server

        out = tmp_path / "out"
        with (
            patch.object(server, "read_note", return_value=_content("Twin")) as rn,
            patch.object(server, "note_to_markdown", return_value="# Twin\n"),
        ):
            result = server.export_note(U_TWIN_NOTES, str(out))
        assert rn.call_args.kwargs["note"].pk == 11
        assert result["path"].endswith("Twin.md")
        assert {m["id"] for m in result["other_matches"]} == {U_TWIN_WORK, U_TWIN_CASE}


@pytest.fixture
def no_side_effects():
    from apple_notes_mcp import server

    with (
        patch.object(server, "_perform_backup", return_value="/b") as backup,
        patch.object(server, "_is_gui_session", return_value=True),
        patch.object(server, "nudge_icloud_sync", return_value=False),
        patch.object(server, "execute_replace_note") as replace,
        patch.object(server, "execute_toggle_checklist_item") as toggle,
        patch.object(write_service, "execute_append_to_note") as append,
        patch.object(write_service, "is_gui_session", return_value=True),
    ):
        yield SimpleNamespace(
            backup=backup, replace=replace, toggle=toggle, append=append
        )


class TestWriteTools:
    def test_update_refuses_a_shared_title_before_any_backup(
        self, server_db, no_side_effects
    ):
        from apple_notes_mcp import server

        with pytest.raises(ToolError) as exc:
            server.update_note_content("Twin", "more", "append")
        assert U_TWIN_WORK in str(exc.value)
        no_side_effects.backup.assert_not_called()
        no_side_effects.append.assert_not_called()

    def test_update_by_id_of_a_shared_title_is_refused_too(
        self, server_db, no_side_effects
    ):
        from apple_notes_mcp import server

        # Writes open a note by its title, so an id can't pick one twin.
        with pytest.raises(ToolError, match="3 notes are titled 'Twin'"):
            server.update_note_content(U_TWIN_NOTES, "more", "replace")
        no_side_effects.replace.assert_not_called()

    def test_update_by_id_writes_to_that_notes_title(self, server_db, no_side_effects):
        from apple_notes_mcp import server

        result = server.update_note_content(U_SOLO, "new body", "replace")
        assert no_side_effects.replace.call_args.args[0] == "Solo"
        assert result["note_name"] == "Solo"

    def test_toggle_refuses_a_shared_title(self, server_db, no_side_effects):
        from apple_notes_mcp import server

        with pytest.raises(ToolError, match="3 notes are titled 'Twin'"):
            server.toggle_checklist_item("Twin", "milk")
        no_side_effects.toggle.assert_not_called()

    def test_update_with_a_trashed_twin_proceeds(self, server_db, no_side_effects):
        from apple_notes_mcp import server

        result = server.update_note_content("Live and trashed", "more", "append")
        assert result["success"] and no_side_effects.append.call_args.args[0] == "Live and trashed"

    def test_find_replace_checks_the_live_note_not_the_trashed_copy(
        self, server_db, no_side_effects
    ):
        from apple_notes_mcp import server

        with patch.object(
            server, "read_note", return_value=_content("Live and trashed")
        ) as rn, patch.object(server, "execute_find_replace_multiline"):
            server.update_note_content(
                "Live and trashed", "new", "find_replace", old_text="body"
            )
        assert rn.call_args.kwargs["note"].pk == 15

    def test_toggle_with_a_trashed_twin_proceeds(self, server_db, no_side_effects):
        from apple_notes_mcp import server

        server.toggle_checklist_item("Live and trashed", "milk")
        assert no_side_effects.toggle.call_args.args == ("Live and trashed", "milk")

    def test_toggle_by_id(self, server_db, no_side_effects):
        from apple_notes_mcp import server

        server.toggle_checklist_item(U_SOLO, "milk")
        assert no_side_effects.toggle.call_args.args == ("Solo", "milk")

    def test_delete_by_id_addresses_that_title(self, server_db):
        from apple_notes_mcp import server

        with patch.object(
            write_service,
            "delete_note_by_name",
            return_value=dict.fromkeys(
                ("title", "folder", "modified", "preview", "backup_path", "sync_nudged")
            ),
        ) as dn:
            server.delete_note(U_SOLO)
        assert dn.call_args.args[0] == "Solo"

    def test_move_by_id_addresses_that_title(self, server_db):
        from apple_notes_mcp import server

        with patch.object(
            write_service,
            "move_note_by_name",
            return_value=dict.fromkeys(
                (
                    "title",
                    "old_folder",
                    "new_folder",
                    "moved",
                    "backup_path",
                    "sync_nudged",
                )
            ),
        ) as mn:
            server.move_note(U_SOLO, "Work")
        assert mn.call_args.args[0] == "Solo"


# ------------------------------------------------------- the remote queue


def test_remote_accept_takes_an_id_and_queues_the_title(tmp_path):
    from apple_notes_mcp.remote.write_apply import WriteApplier
    from apple_notes_mcp.remote.write_queue import WriteQueue, WriteQueueConfig

    note = SimpleNamespace(pk=10, name="Solo", uuid=U_SOLO, folder="Notes")

    class DB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_note_by_uuid(self, uuid):
            return note if uuid.upper() == U_SOLO else None

        def find_title_matches(self, name):
            return [SimpleNamespace(pk=10, in_trash=False)]

        def count_notes_titled(self, name):
            return int(name.casefold() == "solo")

        count_live_notes_titled = count_notes_titled

        def get_note_by_name(self, name):
            if name == "Solo":
                return note
            raise NoteNotFoundError(name)

        def note_fingerprint(self, pk):
            return "fp"

    reader = MagicMock()
    reader.get_note_content_for_note.return_value = SimpleNamespace(
        resolved_text="Solo\nbody", raw_text="Solo\nbody", attachments=[]
    )
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"))
    applier = WriteApplier(
        queue,
        db_factory=DB,
        reader_factory=lambda: reader,
        checklist_state=lambda *a: None,
        stdio=MagicMock(),
        delete_note=lambda name: None,
        sleep=lambda s: None,
    )
    out = applier.accept(
        "update_note_content", {"name": U_SOLO, "body": "more", "operation": "append"}
    )
    rec = queue.get(out["write_id"])
    assert rec["note_title"] == "Solo" and rec["note_uuid"] == U_SOLO
    assert rec["args"]["name"] == "Solo"


# ------------------------------------------- AppleScript target (ui_scripting)


class TestScriptsAddressTheLiveNote:
    LIVE_ID = f"x-coredata://{STORE}/ICNote/p15"

    @pytest.fixture
    def executor(self):
        from apple_notes import ui_scripting

        with patch.object(ui_scripting, "AppleScriptExecutor") as cls, patch.object(
            ui_scripting, "ClipboardManager"
        ), patch.object(ui_scripting, "_live_note_id", return_value=self.LIVE_ID):
            cls._escape_applescript_string.side_effect = lambda s: s
            ex = ui_scripting.UIScriptingExecutor()
            ex._applescript.execute.return_value = (True, "<div>x</div>")
            yield ex

    def _scripts(self, ex):
        return [c.args[0] for c in ex._applescript.execute.call_args_list]

    def test_open_note_shows_the_live_note_by_id(self, executor):
        with patch.object(executor, "_ensure_notes_frontmost"):
            executor.open_note("Live and trashed")
        script = self._scripts(executor)[0]
        assert f'show note id "{self.LIVE_ID}"' in script
        assert 'note "Live and trashed"' not in script

    def test_verify_reads_the_live_note_by_id(self, executor):
        executor._note_body_html("Live and trashed")
        script = self._scripts(executor)[0]
        assert f'set theNote to note id "{self.LIVE_ID}"' in script
        assert "whose name" not in script

    def test_no_trashed_twin_keeps_the_title_script(self, executor):
        from apple_notes import ui_scripting

        with patch.object(ui_scripting, "_live_note_id", return_value=None), patch.object(
            executor, "_ensure_notes_frontmost"
        ):
            executor.open_note("Solo")
        assert 'show note "Solo"' in self._scripts(executor)[0]


# ------------------------------------------------ remote queue, trashed twin


def _remote_applier(tmp_path, live, trashed):
    from apple_notes_mcp.remote.write_apply import WriteApplier
    from apple_notes_mcp.remote.write_queue import WriteQueue, WriteQueueConfig

    note = SimpleNamespace(pk=15, name="Daily", uuid=U_LT_LIVE, folder="Notes")

    class DB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_note_by_uuid(self, uuid):
            return None

        def count_notes_titled(self, name):
            return (live + trashed) if name.casefold() == "daily" else 0

        def count_live_notes_titled(self, name):
            return live if name.casefold() == "daily" else 0

        def get_note_by_name(self, name):
            if name == "Daily":
                return note
            raise NoteNotFoundError(name)

        def note_fingerprint(self, pk):
            return "fp"

    reader = MagicMock()
    reader.get_note_content_for_note.return_value = SimpleNamespace(
        resolved_text="Daily\nbody", raw_text="Daily\nbody", attachments=[]
    )
    queue = WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq"))
    return WriteApplier(
        queue,
        db_factory=DB,
        reader_factory=lambda: reader,
        checklist_state=lambda *a: (0, False),
        stdio=MagicMock(),
        delete_note=lambda name: None,
        sleep=lambda s: None,
    )


@pytest.mark.parametrize(
    "tool,args",
    [
        ("update_note_content", {"name": "Daily", "body": "more", "operation": "append"}),
        ("toggle_checklist_item", {"name": "Daily", "item_text": "milk"}),
    ],
)
def test_remote_accept_writes_past_a_trashed_twin(tmp_path, tool, args):
    applier = _remote_applier(tmp_path, live=1, trashed=1)
    out = applier.accept(tool, dict(args))
    assert out["write_id"]


def test_remote_accept_still_refuses_two_live_twins(tmp_path):
    from apple_notes_mcp.remote.write_apply import WriteRefused

    applier = _remote_applier(tmp_path, live=2, trashed=0)
    with pytest.raises(WriteRefused, match="2 notes are titled"):
        applier.accept(
            "update_note_content", {"name": "Daily", "body": "x", "operation": "append"}
        )


# ------------------------------------------------- review round 3 (fail closed)

# The real lookup, captured before conftest's autouse stub replaces it.
from apple_notes import ui_scripting as _ui_scripting  # noqa: E402

_REAL_LIVE_NOTE_ID = _ui_scripting._live_note_id


def test_a_trashed_twin_with_no_store_id_refuses_instead_of_using_the_title(db):
    from apple_notes.exceptions import UIScriptingError

    with patch.object(db, "store_uuid", return_value=None):
        with pytest.raises(UIScriptingError, match="store id"):
            live_note_id_beside_trashed_twins(db, "Live and trashed")
        # No trashed twin: nothing to resolve, the title is safe as before.
        assert live_note_id_beside_trashed_twins(db, "Solo") is None


def test_a_failed_notestore_lookup_refuses_the_write(monkeypatch):
    from apple_notes import database
    from apple_notes.exceptions import UIScriptingError

    def broken(*a, **k):
        raise database.DatabaseNotFoundError("gone")

    monkeypatch.setattr(database, "NotesDatabase", broken)
    with pytest.raises(UIScriptingError, match="Recently Deleted"):
        _REAL_LIVE_NOTE_ID("Live and trashed")


def test_the_real_lookup_finds_the_live_id(db_path, monkeypatch):
    from apple_notes import database

    monkeypatch.setattr(database, "NotesDatabase", lambda *a, **k: NotesDatabase(db_path))
    assert _REAL_LIVE_NOTE_ID("Live and trashed") == f"x-coredata://{STORE}/ICNote/p15"


class TestAnIdOfATrashedNote:
    """An id naming a note in Recently Deleted must not write to its live twin."""

    def test_update_refuses(self, server_db, no_side_effects):
        from apple_notes_mcp import server

        with pytest.raises(ToolError, match="Recently Deleted"):
            server.update_note_content(U_LT_TRASH, "x", "append")
        no_side_effects.append.assert_not_called()
        no_side_effects.backup.assert_not_called()

    def test_toggle_refuses(self, server_db, no_side_effects):
        from apple_notes_mcp import server

        with pytest.raises(ToolError, match="Recently Deleted"):
            server.toggle_checklist_item(U_LT_TRASH, "milk")
        no_side_effects.toggle.assert_not_called()

    def test_the_live_notes_id_still_writes(self, server_db, no_side_effects):
        from apple_notes_mcp import server

        server.update_note_content(U_LT_LIVE, "x", "append")
        assert no_side_effects.append.call_args.args[0] == "Live and trashed"

    def test_remote_accept_refuses(self, db_path, tmp_path):
        from apple_notes_mcp.remote.write_apply import WriteApplier, WriteRefused
        from apple_notes_mcp.remote.write_queue import WriteQueue, WriteQueueConfig

        applier = WriteApplier(
            WriteQueue(WriteQueueConfig(state_dir=tmp_path / "wq")),
            db_factory=lambda: NotesDatabase(db_path),
            reader_factory=MagicMock,
            checklist_state=lambda *a: None,
            stdio=MagicMock(),
            delete_note=lambda name: None,
            sleep=lambda s: None,
        )
        with pytest.raises(WriteRefused, match="Recently Deleted"):
            applier.accept(
                "update_note_content", {"name": U_LT_TRASH, "body": "x", "operation": "append"}
            )
