"""Backup operations for Apple Notes database."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from . import demo_mode
from .exceptions import BackupError, DatabaseNotFoundError


class BackupManager:
    """Manage backups of the Apple Notes SQLite database."""

    DEFAULT_BACKUP_DIR = "~/Library/Application Support/notes-graph-backup"
    DEFAULT_DB_PATH = "~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
    # Per-note undo for remote writes lives in the write-queue journal (#169);
    # these whole-DB copies are disaster recovery, ~37 MB each.
    DEFAULT_MAX_BACKUPS = 20

    def __init__(
        self,
        backup_dir: Optional[str] = None,
        max_backups: int = DEFAULT_MAX_BACKUPS,
        db_path: Optional[str] = None
    ):
        """
        Initialize backup manager.

        Args:
            backup_dir: Directory to store backups (default: ~/Library/Application Support/notes-graph-backup)
            max_backups: Maximum number of backups to retain (default: 20)
            db_path: Path to the Notes database (uses default if not provided)
        """
        self.backup_dir = Path(os.path.expanduser(backup_dir or self.DEFAULT_BACKUP_DIR))
        self.db_path = Path(os.path.expanduser(db_path or self.DEFAULT_DB_PATH))
        self.max_backups = max_backups

    def create_backup(self) -> Path:
        """
        Create a timestamped backup of the Notes database.

        Returns:
            Path to the created backup file

        Raises:
            DatabaseNotFoundError: If source database doesn't exist
            BackupError: If backup creation fails
        """
        # A backup only ever precedes a write; neither happens in demo mode.
        demo_mode.require_live("Backing up before a Notes write")
        if not self.db_path.exists():
            raise DatabaseNotFoundError(f"Notes database not found at {self.db_path}")

        # Create backup directory if it doesn't exist
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackupError(f"Failed to create backup directory: {e}")

        # Generate timestamped filename with microseconds to prevent collisions
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_filename = f"NoteStore_backup_{timestamp}.sqlite"
        backup_path = self.backup_dir / backup_filename

        # SQLite online backup, not a file copy: Notes keeps recent edits in
        # NoteStore.sqlite-wal, which a copy of the main file misses (#169).
        # Read-only source connection, so Notes' database is never written.
        source = dest = None
        try:
            source = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            dest = sqlite3.connect(backup_path)
            source.backup(dest)
            # The copy inherits the source's WAL mode; make it one self-contained
            # file so opening it later leaves no sidecars for rotation to orphan.
            dest.execute("PRAGMA journal_mode=DELETE")
        except (sqlite3.Error, OSError) as e:
            if dest is not None:
                dest.close()
                dest = None
            backup_path.unlink(missing_ok=True)
            raise BackupError(f"Failed to create backup: {e}")
        finally:
            if dest is not None:
                dest.close()
            if source is not None:
                source.close()

        return backup_path

    def cleanup_old_backups(self) -> int:
        """
        Remove old backups exceeding max_backups limit.

        Returns:
            Number of backups removed
        """
        backups = self.list_backups()
        if len(backups) <= self.max_backups:
            return 0

        # Remove oldest backups (list is sorted newest first)
        backups_to_remove = backups[self.max_backups:]
        removed_count = 0

        for backup_path in backups_to_remove:
            try:
                backup_path.unlink()
                removed_count += 1
            except OSError:
                # Continue removing other backups even if one fails
                pass

        return removed_count

    def list_backups(self) -> List[Path]:
        """
        List all existing backups, sorted by date (newest first).

        Returns:
            List of backup file paths
        """
        if not self.backup_dir.exists():
            return []

        # Find all backup files matching our naming pattern
        backups = list(self.backup_dir.glob("NoteStore_backup_*.sqlite"))

        # Sort by modification time (newest first)
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        return backups
