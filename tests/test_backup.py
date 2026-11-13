"""Tests for the backup module."""
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from apple_notes.backup import BackupManager
from apple_notes.exceptions import BackupError, DatabaseNotFoundError


def _make_db(path: Path, rows=("committed",)) -> None:
    """A real SQLite file with a ``t(v)`` table (the backup API needs SQLite)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


def _rows(path: Path) -> list:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        conn.close()


class TestBackupManager:
    """Tests for the BackupManager class."""

    def test_create_backup_success(self, tmp_path):
        """Should create a timestamped backup of the database."""
        db_path = tmp_path / "NoteStore.sqlite"
        _make_db(db_path)

        backup_dir = tmp_path / "backups"

        manager = BackupManager(
            backup_dir=str(backup_dir),
            db_path=str(db_path)
        )

        backup_path = manager.create_backup()

        assert backup_path.exists()
        assert backup_path.name.startswith("NoteStore_backup_")
        assert backup_path.name.endswith(".sqlite")
        assert _rows(backup_path) == ["committed"]

    def test_create_backup_includes_rows_still_in_the_wal(self, tmp_path):
        """Notes keeps recent edits in NoteStore.sqlite-wal; a plain copy of the
        main file misses them (issue #169 measured 13/14). The backup must not."""
        db_path = tmp_path / "NoteStore.sqlite"
        _make_db(db_path)
        writer = sqlite3.connect(db_path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO t VALUES ('only-in-wal')")
        writer.commit()
        try:
            assert (tmp_path / "NoteStore.sqlite-wal").stat().st_size > 0
            manager = BackupManager(
                backup_dir=str(tmp_path / "backups"), db_path=str(db_path)
            )
            backup_path = manager.create_backup()
        finally:
            writer.close()

        assert _rows(backup_path) == ["committed", "only-in-wal"]
        # A self-contained single file: opening it later must not spawn
        # -wal/-shm sidecars that rotation (which globs *.sqlite) would orphan.
        conn = sqlite3.connect(backup_path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            conn.close()

    def test_create_backup_unreadable_database_raises_backup_error(self, tmp_path):
        """A source SQLite can't read aborts the write instead of passing silently."""
        db_path = tmp_path / "NoteStore.sqlite"
        db_path.write_text("not a sqlite database")

        manager = BackupManager(backup_dir=str(tmp_path / "backups"), db_path=str(db_path))

        with pytest.raises(BackupError):
            manager.create_backup()

    def test_default_keeps_twenty_backups(self):
        assert BackupManager.DEFAULT_MAX_BACKUPS == 20

    def test_create_backup_database_not_found(self, tmp_path):
        """Should raise DatabaseNotFoundError when database doesn't exist."""
        manager = BackupManager(
            backup_dir=str(tmp_path / "backups"),
            db_path=str(tmp_path / "nonexistent.sqlite")
        )

        with pytest.raises(DatabaseNotFoundError):
            manager.create_backup()

    def test_create_backup_creates_directory(self, tmp_path):
        """Should create backup directory if it doesn't exist."""
        db_path = tmp_path / "NoteStore.sqlite"
        _make_db(db_path)

        backup_dir = tmp_path / "nested" / "backup" / "dir"

        manager = BackupManager(
            backup_dir=str(backup_dir),
            db_path=str(db_path)
        )

        backup_path = manager.create_backup()

        assert backup_dir.exists()
        assert backup_path.exists()

    def test_cleanup_old_backups(self, tmp_path):
        """Should remove old backups exceeding max limit."""
        db_path = tmp_path / "NoteStore.sqlite"
        db_path.write_text("fake database content")

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # Create 5 fake backup files with different timestamps
        for i in range(5):
            backup_file = backup_dir / f"NoteStore_backup_2024010{i}_120000.sqlite"
            backup_file.write_text(f"backup {i}")
            # Set different modification times
            os.utime(backup_file, (i * 100, i * 100))

        manager = BackupManager(
            backup_dir=str(backup_dir),
            db_path=str(db_path),
            max_backups=3
        )

        removed = manager.cleanup_old_backups()

        assert removed == 2
        assert len(manager.list_backups()) == 3

    def test_cleanup_old_backups_none_to_remove(self, tmp_path):
        """Should return 0 when no backups need removal."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # Create 2 backups with max_backups=5
        for i in range(2):
            backup_file = backup_dir / f"NoteStore_backup_2024010{i}_120000.sqlite"
            backup_file.write_text(f"backup {i}")

        manager = BackupManager(
            backup_dir=str(backup_dir),
            db_path=str(tmp_path / "db.sqlite"),
            max_backups=5
        )

        removed = manager.cleanup_old_backups()

        assert removed == 0
        assert len(manager.list_backups()) == 2

    def test_list_backups_sorted_newest_first(self, tmp_path):
        """Should return backups sorted by modification time (newest first)."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # Create backups with different modification times
        old_backup = backup_dir / "NoteStore_backup_20240101_120000.sqlite"
        old_backup.write_text("old")
        os.utime(old_backup, (100, 100))

        new_backup = backup_dir / "NoteStore_backup_20240102_120000.sqlite"
        new_backup.write_text("new")
        os.utime(new_backup, (200, 200))

        manager = BackupManager(backup_dir=str(backup_dir))

        backups = manager.list_backups()

        assert len(backups) == 2
        assert backups[0].name == "NoteStore_backup_20240102_120000.sqlite"
        assert backups[1].name == "NoteStore_backup_20240101_120000.sqlite"

    def test_list_backups_empty_directory(self, tmp_path):
        """Should return empty list when no backups exist."""
        manager = BackupManager(backup_dir=str(tmp_path / "empty"))

        backups = manager.list_backups()

        assert backups == []

    def test_list_backups_ignores_non_backup_files(self, tmp_path):
        """Should only list files matching backup naming pattern."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # Create a valid backup and some other files
        (backup_dir / "NoteStore_backup_20240101_120000.sqlite").write_text("backup")
        (backup_dir / "other_file.txt").write_text("other")
        (backup_dir / "NoteStore.sqlite").write_text("db")

        manager = BackupManager(backup_dir=str(backup_dir))

        backups = manager.list_backups()

        assert len(backups) == 1
        assert backups[0].name == "NoteStore_backup_20240101_120000.sqlite"
