"""Unit tests for the shared append write path (mocked executors)."""

from unittest.mock import MagicMock, patch

import pytest


def _fake_conversion():
    result = MagicMock()
    result.data = b"richtext"
    result.pending_actions = ["pending"]
    return result


class TestAppendMarkdownToNote:
    def _call(self, gui=True, skip_backup=False, links=None):
        with (
            patch("apple_notes.write_service.BackupManager") as backup_cls,
            patch(
                "apple_notes.write_service.link_targets",
                return_value={"target": "TARGET-LINK"},
            ) as targets,
            patch(
                "apple_notes.write_service.markdown_to_richtext_new_linked",
                return_value=_fake_conversion(),
            ) as convert,
            patch("apple_notes.write_service.execute_append_to_note") as local_exec,
            patch("apple_notes.write_service.UIHelperClient") as client_cls,
            patch("apple_notes.write_service.is_gui_session", return_value=gui),
        ):
            backup_cls.return_value.create_backup.return_value = "/tmp/backup.sqlite"
            from apple_notes.write_service import append_markdown_to_note

            out = append_markdown_to_note(
                "Note",
                "[[Target]]",
                skip_backup=skip_backup,
                links=links,
            )
            self.targets = targets
            return out, backup_cls, convert, local_exec, client_cls

    def test_gui_session_routes_locally_and_backs_up_first(self):
        out, backup_cls, convert, local_exec, client_cls = self._call(gui=True)
        assert out["backup_path"] == "/tmp/backup.sqlite"
        backup_cls.return_value.create_backup.assert_called_once()
        local_exec.assert_called_once_with("Note", b"richtext", ["pending"])
        client_cls.assert_not_called()

    def test_remote_session_routes_through_daemon(self):
        out, _, _, local_exec, client_cls = self._call(gui=False)
        local_exec.assert_not_called()
        client_cls.return_value.append_to_note.assert_called_once_with(
            "Note", b"richtext", ["pending"]
        )

    def test_skip_backup_skips_backup(self):
        out, backup_cls, _, _, _ = self._call(skip_backup=True)
        assert out["backup_path"] is None
        backup_cls.return_value.create_backup.assert_not_called()

    def test_default_links_come_from_the_database(self):
        _, _, convert, _, _ = self._call()
        self.targets.assert_called_once_with("[[Target]]", db_path=None)
        assert convert.call_args.args == ("[[Target]]", {"target": "TARGET-LINK"})

    def test_injected_links_win(self):
        injected = {"x": "INJECTED"}
        _, _, convert, _, _ = self._call(links=injected)
        self.targets.assert_not_called()
        assert convert.call_args.args[1] is injected


class TestDeleteNoteByName:
    @pytest.fixture(autouse=True)
    def _stub_delete_guard(self):
        """The #229 guard reads NoteStore; these tests cover the routing."""
        from apple_notes.models import TitleMatch

        match = TitleMatch(1, "U-1", "Note", "Notes", 2, None, False, False)
        with (
            patch("apple_notes.write_service.NotesDatabase"),
            patch("apple_notes.write_service.resolve_unique_note", return_value=match),
            patch("apple_notes.write_service.note_preview", return_value="preview"),
        ):
            yield

    def _call(self, gui=True, skip_backup=False):
        with (
            patch("apple_notes.write_service.BackupManager") as backup_cls,
            patch("apple_notes.write_service.execute_delete_note") as local_exec,
            patch("apple_notes.write_service.UIHelperClient") as client_cls,
            patch(
                "apple_notes.write_service.nudge_icloud_sync", return_value=True
            ) as nudge,
            patch("apple_notes.write_service.is_gui_session", return_value=gui),
        ):
            backup_cls.return_value.create_backup.return_value = "/tmp/backup.sqlite"
            from apple_notes.write_service import delete_note_by_name

            out = delete_note_by_name("Note", skip_backup=skip_backup)
            return out, backup_cls, local_exec, client_cls, nudge

    def test_gui_session_routes_locally_and_backs_up_first(self):
        out, backup_cls, local_exec, client_cls, nudge = self._call(gui=True)
        assert out["backup_path"] == "/tmp/backup.sqlite"
        assert out["sync_nudged"] is True
        backup_cls.return_value.create_backup.assert_called_once()
        local_exec.assert_called_once_with("Note")
        client_cls.assert_not_called()
        nudge.assert_called_once()

    def test_remote_session_routes_through_daemon(self):
        _, _, local_exec, client_cls, _ = self._call(gui=False)
        local_exec.assert_not_called()
        client_cls.return_value.delete_note.assert_called_once_with("Note")

    def test_skip_backup_skips_backup(self):
        out, backup_cls, _, _, _ = self._call(skip_backup=True)
        assert out["backup_path"] is None
        backup_cls.return_value.create_backup.assert_not_called()

    def test_backup_failure_aborts_before_delete(self):
        from apple_notes.exceptions import BackupError

        with (
            patch("apple_notes.write_service.BackupManager") as backup_cls,
            patch("apple_notes.write_service.execute_delete_note") as local_exec,
            patch("apple_notes.write_service.is_gui_session", return_value=True),
        ):
            backup_cls.return_value.create_backup.side_effect = BackupError("nope")
            from apple_notes.write_service import delete_note_by_name

            import pytest

            with pytest.raises(BackupError):
                delete_note_by_name("Note")
            local_exec.assert_not_called()


class TestIsGuiSession:
    def test_ssh_env_means_remote(self, monkeypatch):
        from apple_notes import write_service

        monkeypatch.delenv("NOTES_GRAPH_USE_DAEMON", raising=False)
        monkeypatch.setenv("SSH_CLIENT", "1.2.3.4")
        assert write_service.is_gui_session() is False

    def test_daemon_override(self, monkeypatch):
        from apple_notes import write_service

        monkeypatch.delenv("SSH_CLIENT", raising=False)
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
        monkeypatch.setenv("NOTES_GRAPH_USE_DAEMON", "1")
        assert write_service.is_gui_session() is False

    def test_plain_gui(self, monkeypatch):
        from apple_notes import write_service

        for var in ("NOTES_GRAPH_USE_DAEMON", "SSH_CLIENT", "SSH_CONNECTION"):
            monkeypatch.delenv(var, raising=False)
        assert write_service.is_gui_session() is True
