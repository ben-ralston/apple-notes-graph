"""delete_note / move_note write chains and the AppleScript move (#229, #230).

Headless: osascript and the NoteStore are faked, so this runs in CI.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from apple_notes.applescript import AppleScriptExecutor
from apple_notes.exceptions import (
    AmbiguousNoteTitleError,
    AppleScriptError,
    EncryptedNoteError,
    FolderNotFoundError,
    NoteInTrashError,
    NoteNotFoundError,
)

STORE = "D80FAC5F-287C-4FF0-A5E9-C0CA1EEED270"
NOTE_ID = f"x-coredata://{STORE}/ICNote/p10"
FOLDER_ID = f"x-coredata://{STORE}/ICFolder/p4"


# ------------------------------------------------------------ AppleScript move
class TestMoveNoteById:
    def _run(self, result=(True, "")):
        ex = AppleScriptExecutor()
        with patch.object(ex, "execute", return_value=result) as run:
            ex.move_note_by_id(NOTE_ID, FOLDER_ID)
        return run.call_args.args[0]

    def test_moves_by_id_with_trash_and_lock_checks(self):
        script = self._run()
        assert f'note id "{NOTE_ID}"' in script
        assert f'folder id "{FOLDER_ID}"' in script
        assert "move theNote to theFolder" in script
        assert "password protected of theNote" in script
        # Checked before anything moves. A trashed note's container is the
        # "Recently Deleted" folder (verified live; it only errors in some
        # reference forms), so the name is compared, with an error fallback.
        assert script.index("container of theNote") < script.index("move theNote")
        assert 'name of c is "Recently Deleted"' in script

    @pytest.mark.parametrize(
        "note_id, folder_id",
        [
            ('x" & (do shell script "id") & "', FOLDER_ID),
            (NOTE_ID, f"x-coredata://{STORE}/ICNote/p4"),  # a note as the folder
            (FOLDER_ID, FOLDER_ID),  # a folder as the note
        ],
    )
    def test_refuses_malformed_ids_before_running_anything(self, note_id, folder_id):
        ex = AppleScriptExecutor()
        with patch.object(ex, "execute") as run:
            with pytest.raises(ValueError):
                ex.move_note_by_id(note_id, folder_id)
        run.assert_not_called()

    @pytest.mark.parametrize(
        "output, exc",
        [
            ("execution error: NOTE_IN_RECENTLY_DELETED (-2700)", NoteInTrashError),
            ("execution error: NOTE_PASSWORD_PROTECTED (-2700)", EncryptedNoteError),
            (
                'execution error: Notes got an error: Can’t get note id "x". (-1728)',
                NoteNotFoundError,
            ),
            (
                'execution error: Notes got an error: Can’t get folder id "x". (-1728)',
                FolderNotFoundError,
            ),
            ("execution error: something else (-1)", AppleScriptError),
        ],
    )
    def test_maps_failures(self, output, exc):
        with pytest.raises(exc):
            self._run((False, output))


# ------------------------------------------------------------ fake NoteStore
@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "NoteStore.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, ZIDENTIFIER TEXT,"
        " ZTITLE1 TEXT, ZTITLE2 TEXT, ZFOLDER INTEGER, ZPARENT INTEGER, ZFOLDERTYPE INTEGER,"
        " ZCREATIONDATE3 REAL, ZMODIFICATIONDATE1 REAL, ZLASTOPENEDDATE REAL,"
        " ZMARKEDFORDELETION INTEGER DEFAULT 0, ZISPASSWORDPROTECTED INTEGER)"
    )
    conn.execute("CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB)")
    conn.execute("CREATE TABLE Z_METADATA (Z_UUID TEXT)")
    conn.execute("INSERT INTO Z_METADATA VALUES (?)", (STORE,))
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZPARENT, ZFOLDERTYPE) VALUES (?, ?, ?, ?)",
        [
            (1, "Recently Deleted", None, 1),
            (2, "Notes", None, 0),
            (3, "Other", None, 0),
            (4, "🗄️ Archive", 3, 0),
        ],
    )
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER, ZISPASSWORDPROTECTED)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (10, "U-10", "Groceries", 2, 0),
            (11, "U-11", "Twin", 2, 0),
            (12, "U-12", "Twin", 4, 0),
            (13, "U-13", "Trashed", 1, 0),
            (14, "U-14", "Secret", 2, 1),
            (15, "U-15", "Archived", 4, 0),
        ],
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def seams():
    """Everything with a side effect in the chains, mocked."""
    with (
        patch("apple_notes.write_service.BackupManager") as backup_cls,
        patch("apple_notes.write_service.execute_delete_note") as delete_exec,
        patch("apple_notes.write_service.execute_move_note") as move_exec,
        patch("apple_notes.write_service.UIHelperClient") as client_cls,
        patch("apple_notes.write_service.nudge_icloud_sync", return_value=False),
        patch("apple_notes.write_service.is_gui_session", return_value=True),
        patch(
            "apple_notes.write_service.note_preview",
            return_value="first line of the body",
        ),
    ):
        backup_cls.return_value.create_backup.return_value = "/tmp/backup.sqlite"
        yield MagicMock(
            backup=backup_cls, delete=delete_exec, move=move_exec, client=client_cls
        )


# ------------------------------------------------------------ delete chain
class TestDeleteGuards:
    def test_exact_match_deletes_and_reports_the_note(self, db_path, seams):
        from apple_notes.write_service import delete_note_by_name

        out = delete_note_by_name("Groceries", db_path=db_path)
        seams.delete.assert_called_once_with("Groceries")
        assert out["title"] == "Groceries"
        assert out["folder"] == "Notes"
        assert "modified" in out
        assert out["preview"] == "first line of the body"
        assert out["backup_path"] == "/tmp/backup.sqlite"

    @pytest.mark.parametrize(
        "name, exc",
        [
            ("Twin", AmbiguousNoteTitleError),
            ("Nope", NoteNotFoundError),
            ("Secret", EncryptedNoteError),
            ("Trashed", NoteInTrashError),
            ("groceries", NoteNotFoundError),
        ],
    )
    def test_refusals_touch_nothing(self, db_path, seams, name, exc):
        from apple_notes.write_service import delete_note_by_name

        with pytest.raises(exc):
            delete_note_by_name(name, db_path=db_path)
        seams.delete.assert_not_called()
        seams.client.assert_not_called()
        seams.backup.return_value.create_backup.assert_not_called()

    def test_duplicate_refusal_lists_count_folders_and_dates(self, db_path, seams):
        from apple_notes.write_service import delete_note_by_name

        with pytest.raises(AmbiguousNoteTitleError) as info:
            delete_note_by_name("Twin", db_path=db_path)
        assert "2 notes" in str(info.value)
        assert "'Notes'" in str(info.value) and "'Other/🗄️ Archive'" in str(info.value)


# ------------------------------------------------------------ move chain
class TestMoveChain:
    def test_moves_by_id_and_reports_old_and_new_folder(self, db_path, seams):
        from apple_notes.write_service import move_note_by_name

        out = move_note_by_name("Groceries", "Archive", db_path=db_path)
        seams.move.assert_called_once_with(NOTE_ID, FOLDER_ID)
        assert out["title"] == "Groceries"
        assert out["old_folder"] == "Notes"
        assert out["new_folder"] == "Other/🗄️ Archive"
        assert out["moved"] is True
        assert out["backup_path"] == "/tmp/backup.sqlite"

    def test_remote_session_routes_through_the_daemon(self, db_path, seams):
        from apple_notes.write_service import move_note_by_name

        with patch("apple_notes.write_service.is_gui_session", return_value=False):
            move_note_by_name("Groceries", "Archive", db_path=db_path)
        seams.move.assert_not_called()
        seams.client.return_value.move_note.assert_called_once_with(NOTE_ID, FOLDER_ID)

    def test_already_there_is_a_noop(self, db_path, seams):
        from apple_notes.write_service import move_note_by_name

        out = move_note_by_name("Archived", "Other/🗄️ Archive", db_path=db_path)
        assert out["moved"] is False
        assert out["old_folder"] == out["new_folder"] == "Other/🗄️ Archive"
        seams.move.assert_not_called()

    @pytest.mark.parametrize(
        "name, folder, exc",
        [
            ("Twin", "Notes", AmbiguousNoteTitleError),
            ("Nope", "Notes", NoteNotFoundError),
            ("Secret", "Archive", EncryptedNoteError),
            ("Trashed", "Notes", NoteInTrashError),
            ("Groceries", "Brand new folder", FolderNotFoundError),
            ("Groceries", "Recently Deleted", FolderNotFoundError),
        ],
    )
    def test_refusals_touch_nothing(self, db_path, seams, name, folder, exc):
        from apple_notes.write_service import move_note_by_name

        with pytest.raises(exc):
            move_note_by_name(name, folder, db_path=db_path)
        seams.move.assert_not_called()
        seams.client.assert_not_called()
        seams.backup.return_value.create_backup.assert_not_called()

    def test_missing_folder_is_never_created(self, db_path, seams):
        from apple_notes.write_service import move_note_by_name

        with pytest.raises(FolderNotFoundError):
            move_note_by_name("Groceries", "Brand new folder", db_path=db_path)
        conn = sqlite3.connect(db_path)
        titles = [
            r[0]
            for r in conn.execute(
                "SELECT ZTITLE2 FROM ZICCLOUDSYNCINGOBJECT WHERE ZTITLE2 IS NOT NULL"
            )
        ]
        assert "Brand new folder" not in titles


def test_note_preview_reads_by_pk_and_tolerates_unparseable_notes(db_path):
    from apple_notes.models import TitleMatch
    from apple_notes.write_service import note_preview

    match = TitleMatch(10, "U-10", "Groceries", "Notes", 2, None, False, False)
    content = MagicMock(resolved_text="Groceries\nmilk\neggs")
    with patch("apple_notes.write_service.NoteContentReader") as reader_cls:
        reader_cls.return_value.get_note_content_for_note.return_value = content
        assert note_preview(match, db_path=db_path) == "milk eggs"
        note = reader_cls.return_value.get_note_content_for_note.call_args.args[0]
        assert note.pk == 10
        from apple_notes.exceptions import ProtobufParseError

        reader_cls.return_value.get_note_content_for_note.side_effect = (
            ProtobufParseError("x")
        )
        assert note_preview(match, db_path=db_path) is None


# ------------------------------------------------------------ MCP tools
class TestDeleteNoteTool:
    def test_returns_the_deleted_notes_identity_and_preview(self, db_path, seams):
        from apple_notes_mcp.server import delete_note

        with patch("apple_notes.write_service.NotesDatabase.DEFAULT_PATH", db_path):
            out = delete_note(name="Groceries")
        assert out["success"] is True
        assert out["note_name"] == "Groceries"
        assert out["folder"] == "Notes"
        assert out["preview"] == "first line of the body"
        assert "modified" in out

    @pytest.mark.parametrize(
        "name, match",
        [
            ("Twin", "2 notes are titled 'Twin'"),
            ("Nope", "0 notes"),
            ("Secret", "locked"),
            ("Trashed", "already in Recently Deleted"),
        ],
    )
    def test_refusals_are_tool_errors_with_the_reason(
        self, db_path, seams, name, match
    ):
        from mcp.server.fastmcp.exceptions import ToolError

        from apple_notes_mcp.server import delete_note

        with patch("apple_notes.write_service.NotesDatabase.DEFAULT_PATH", db_path):
            with pytest.raises(ToolError, match=match):
                delete_note(name=name)
        seams.delete.assert_not_called()

    def test_description_limits_use_to_explicit_requests(self):
        from apple_notes_mcp.server import mcp

        tool = mcp._tool_manager.get_tool("delete_note")
        assert "explicitly asked" in tool.description


class TestMoveNoteTool:
    def test_moves_and_reports_folders(self, db_path, seams):
        from apple_notes_mcp.server import move_note

        with patch("apple_notes.write_service.NotesDatabase.DEFAULT_PATH", db_path):
            out = move_note(name="Groceries", folder="Archive")
        out = {k: v for k, v in out.items() if k not in ("elapsed_ms", "timing")}
        assert out == {
            "success": True,
            "note_name": "Groceries",
            "old_folder": "Notes",
            "new_folder": "Other/🗄️ Archive",
            "moved": True,
            "backup_path": "/tmp/backup.sqlite",
            "sync_nudged": False,
        }

    @pytest.mark.parametrize(
        "name, folder, match",
        [
            ("Twin", "Notes", "2 notes"),
            ("Nope", "Notes", "0 notes"),
            ("Secret", "Notes", "locked"),
            ("Trashed", "Notes", "Recently Deleted"),
            ("Groceries", "Brand new folder", "not found"),
            ("Groceries", "", "cannot be empty"),
            ("", "Notes", "cannot be empty"),
        ],
    )
    def test_refusals(self, db_path, seams, name, folder, match):
        from mcp.server.fastmcp.exceptions import ToolError

        from apple_notes_mcp.server import move_note

        with patch("apple_notes.write_service.NotesDatabase.DEFAULT_PATH", db_path):
            with pytest.raises(ToolError, match=match):
                move_note(name=name, folder=folder)
        seams.move.assert_not_called()

    def test_daemon_timeout_may_still_complete(self, db_path, seams):
        """A timed-out daemon may still be moving the note (#143, #125 NTG-012)."""
        from mcp.server.fastmcp.exceptions import ToolError

        from apple_notes.exceptions import UIHelperTimeoutError
        from apple_notes_mcp.server import move_note

        seams.client.return_value.move_note.side_effect = UIHelperTimeoutError("t/o")
        with (
            patch("apple_notes.write_service.NotesDatabase.DEFAULT_PATH", db_path),
            patch("apple_notes.write_service.is_gui_session", return_value=False),
            pytest.raises(ToolError, match="may still complete") as info,
        ):
            move_note(name="Groceries", folder="Archive")
        assert "not available" not in str(info.value)

    def test_daemon_busy_is_tool_error(self, db_path, seams):
        """The daemon's queue refusal (#143) reaches the caller verbatim."""
        from mcp.server.fastmcp.exceptions import ToolError

        from apple_notes.exceptions import EditLockTimeoutError
        from apple_notes_mcp.server import move_note

        seams.client.return_value.move_note.side_effect = EditLockTimeoutError(
            "busy; this one didn't start within 30s"
        )
        with (
            patch("apple_notes.write_service.NotesDatabase.DEFAULT_PATH", db_path),
            patch("apple_notes.write_service.is_gui_session", return_value=False),
            pytest.raises(ToolError, match="didn't start within 30s"),
        ):
            move_note(name="Groceries", folder="Archive")


class TestReviewRound1:
    def test_container_errors_are_not_reported_as_trash(self):
        ex = AppleScriptExecutor()
        timeout = "execution error: Notes got an error: AppleEvent timed out. (-1712)"
        with patch.object(ex, "execute", return_value=(False, timeout)) as run:
            with pytest.raises(AppleScriptError) as info:
                ex.move_note_by_id(NOTE_ID, FOLDER_ID)
        assert not isinstance(info.value, NoteInTrashError)
        script = run.call_args.args[0]
        # Only the name check may raise the trash sentinel.
        assert script.count("NOTE_IN_RECENTLY_DELETED") == 1
        assert "on error" not in script

    def test_note_preview_never_fails_the_delete(self, db_path):
        from apple_notes.models import TitleMatch
        from apple_notes.write_service import note_preview

        match = TitleMatch(10, "U-10", "Groceries", "Notes", 2, None, False, False)
        with patch("apple_notes.write_service.NoteContentReader") as reader_cls:
            reader_cls.return_value.get_note_content_for_note.side_effect = KeyError(
                "x"
            )
            assert note_preview(match, db_path=db_path) is None
