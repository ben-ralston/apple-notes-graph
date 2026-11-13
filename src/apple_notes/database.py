"""Database operations for Apple Notes SQLite database."""

import sqlite3
import hashlib
import unicodedata
import os
import re
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple

from . import folder_scope
from .link_tokens import note_link_uuid
from .models import FolderMatch, GraphLink, Note, RawNoteLink, TitleMatch
from .exceptions import DatabaseNotFoundError, NoteNotFoundError


# Apple Cocoa timestamp epoch: Jan 1, 2001 00:00:00 UTC
# This is 978307200 seconds after Unix epoch (Jan 1, 1970)
COCOA_EPOCH_OFFSET = 978307200


def _cocoa_timestamp_to_iso(timestamp: Optional[float]) -> Optional[str]:
    """
    Convert Apple Cocoa timestamp to ISO 8601 string.

    Args:
        timestamp: Seconds since Jan 1, 2001 (Cocoa epoch), or None.

    Returns:
        ISO 8601 formatted string in UTC (e.g., "2024-03-15T10:30:00Z"), or None.
    """
    if timestamp is None:
        return None
    unix_timestamp = timestamp + COCOA_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class NotesDatabase:
    """Handle all SQLite database operations for Apple Notes."""

    DEFAULT_PATH = "~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"

    # Soft-deleted notes move to the trash folder WITHOUT setting
    # ZMARKEDFORDELETION (that flips only at the 30-day purge), so listings
    # must also exclude notes whose folder is the trash. ZFOLDERTYPE = 1 is
    # the trash folder, locale-independent (its title is localized).
    _NOT_IN_TRASH = """
            AND NOT EXISTS (
                SELECT 1 FROM ZICCLOUDSYNCINGOBJECT f
                WHERE f.Z_PK = n.ZFOLDER AND f.ZFOLDERTYPE = 1
            )
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize database connection.

        Args:
            db_path: Path to the SQLite database. Uses default macOS location if not provided.
        """
        self.db_path = os.path.expanduser(db_path or self.DEFAULT_PATH)
        self._conn: Optional[sqlite3.Connection] = None
        self._folder_hierarchy_cache: Optional[Dict[int, dict]] = None

    def connect(self) -> sqlite3.Connection:
        """Open database connection."""
        if not os.path.exists(self.db_path):
            raise DatabaseNotFoundError(f"Notes database not found at {self.db_path}")

        if self._conn is None:
            self._conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=10
            )
            self._conn.row_factory = sqlite3.Row

        return self._conn

    def close(self):
        """Close database connection and clear caches."""
        if self._conn:
            self._conn.close()
            self._conn = None
        self._folder_hierarchy_cache = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _build_folder_hierarchy(self) -> Dict[int, dict]:
        """Build folder hierarchy lookup for nested folders (cached)."""
        if self._folder_hierarchy_cache is not None:
            return self._folder_hierarchy_cache

        conn = self.connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT Z_PK, ZTITLE2, ZPARENT
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE ZTITLE2 IS NOT NULL
        """)

        folder_info = {}
        for row in cur.fetchall():
            folder_info[row[0]] = {"name": row[1], "parent": row[2]}

        self._folder_hierarchy_cache = folder_info
        return folder_info

    def _get_folder_path(
        self, folder_pk: Optional[int], folder_info: Dict[int, dict]
    ) -> str:
        """Get full folder path including parents (e.g., 'Other/Archive')."""
        if folder_pk is None or folder_pk not in folder_info:
            return "Notes"

        parts = []
        current = folder_pk
        seen = set()  # Prevent infinite loops

        while current and current in folder_info and current not in seen:
            seen.add(current)
            parts.append(folder_info[current]["name"])
            current = folder_info[current]["parent"]

        return "/".join(reversed(parts)) if parts else "Notes"

    def _trash_folder_pks(self) -> set:
        """Pks of trash folders (ZFOLDERTYPE = 1, locale-independent)."""
        cur = self.connect().cursor()
        cur.execute("SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT WHERE ZFOLDERTYPE = 1")
        return {row[0] for row in cur.fetchall()}

    def _visible(self, folder_pk, folder_path: str) -> bool:
        """Folder-scope check for a single note. With no scope active (stdio)
        this is always True, so by-name/pk/uuid lookups behave as before; when
        scoped (remote mode) the trash is never visible either."""
        if not folder_scope.is_active():
            return True
        if not folder_scope.folder_allowed(folder_path):
            return False
        return not self._in_trash(folder_pk)

    def is_visible(self, folder_pk, folder_path: str) -> bool:
        """Public folder-scope check, for filtering scope-exempt reads such as
        find_title_matches before showing them (#83)."""
        return self._visible(folder_pk, folder_path)

    def _in_trash(self, folder_pk, trash_pks: Optional[set] = None) -> bool:
        """Whether a folder is a trash folder or nested anywhere under one."""
        trash = self._trash_folder_pks() if trash_pks is None else trash_pks
        folder_info = self._build_folder_hierarchy()
        seen = set()
        current = folder_pk
        while current is not None and current not in seen:
            if current in trash:
                return True
            seen.add(current)
            current = folder_info.get(current, {}).get("parent")
        return False

    def _pk_in_scope(self, note_pk: int) -> bool:
        """Whether a note pk is visible under the remote folder scope."""
        try:
            self.get_note_by_pk(note_pk)
        except NoteNotFoundError:
            return False
        return True

    def get_all_notes(self) -> List[Note]:
        """Get all non-deleted notes."""
        conn = self.connect()
        cur = conn.cursor()

        # Build folder hierarchy for nested folder paths
        folder_info = self._build_folder_hierarchy()

        cur.execute(f"""
            SELECT
                n.Z_PK as pk,
                n.ZIDENTIFIER as uuid,
                n.ZTITLE1 as title,
                n.ZFOLDER as folder_pk,
                n.ZCREATIONDATE3 as created,
                n.ZMODIFICATIONDATE1 as modified,
                n.ZLASTOPENEDDATE as last_opened
            FROM ZICCLOUDSYNCINGOBJECT n
            WHERE n.ZTITLE1 IS NOT NULL
            AND n.ZMARKEDFORDELETION != 1
            {self._NOT_IN_TRASH}
        """)

        notes = []
        trash_pks = self._trash_folder_pks() if folder_scope.is_active() else None
        for row in cur.fetchall():
            if trash_pks is not None and self._in_trash(row["folder_pk"], trash_pks):
                continue
            folder_path = self._get_folder_path(row["folder_pk"], folder_info)
            notes.append(
                Note(
                    name=row["title"],
                    uuid=row["uuid"] or "",
                    folder=folder_path,
                    pk=row["pk"],
                    created=_cocoa_timestamp_to_iso(row["created"]),
                    modified=_cocoa_timestamp_to_iso(row["modified"]),
                    last_opened=_cocoa_timestamp_to_iso(row["last_opened"]),
                )
            )

        if folder_scope.is_active():
            notes = [n for n in notes if folder_scope.folder_allowed(n.folder)]
        return notes

    def get_notes_paginated(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Tuple[List[Note], int]:
        """
        Get notes with pagination support.

        Args:
            limit: Maximum number of notes to return
            offset: Number of notes to skip

        Returns:
            Tuple of (notes, total_count)
        """
        if folder_scope.is_active():
            # Folder paths are resolved in Python, so page the scoped list the
            # same way the SQL below orders it (title, then pk).
            scoped = sorted(self.get_all_notes(), key=lambda n: (n.name, n.pk))
            start = offset or 0
            end = None if limit is None else start + limit
            return scoped[start:end], len(scoped)

        conn = self.connect()
        cur = conn.cursor()

        # Build folder hierarchy for nested folder paths
        folder_info = self._build_folder_hierarchy()

        # Get total count
        cur.execute(f"""
            SELECT COUNT(*)
            FROM ZICCLOUDSYNCINGOBJECT n
            WHERE n.ZTITLE1 IS NOT NULL
            AND n.ZMARKEDFORDELETION != 1
            {self._NOT_IN_TRASH}
        """)
        total = cur.fetchone()[0]

        # Build paginated query with deterministic ordering (title + pk for tiebreaker)
        query = f"""
            SELECT
                n.Z_PK as pk,
                n.ZIDENTIFIER as uuid,
                n.ZTITLE1 as title,
                n.ZFOLDER as folder_pk,
                n.ZCREATIONDATE3 as created,
                n.ZMODIFICATIONDATE1 as modified,
                n.ZLASTOPENEDDATE as last_opened
            FROM ZICCLOUDSYNCINGOBJECT n
            WHERE n.ZTITLE1 IS NOT NULL
            AND n.ZMARKEDFORDELETION != 1
            {self._NOT_IN_TRASH}
            ORDER BY n.ZTITLE1, n.Z_PK
        """
        params: list = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
            if offset is not None:
                query += " OFFSET ?"
                params.append(offset)
        elif offset is not None:
            # SQLite requires LIMIT with OFFSET, use -1 for unlimited
            query += " LIMIT -1 OFFSET ?"
            params.append(offset)

        cur.execute(query, params)

        notes = []
        for row in cur.fetchall():
            folder_path = self._get_folder_path(row["folder_pk"], folder_info)
            notes.append(
                Note(
                    name=row["title"],
                    uuid=row["uuid"] or "",
                    folder=folder_path,
                    pk=row["pk"],
                    created=_cocoa_timestamp_to_iso(row["created"]),
                    modified=_cocoa_timestamp_to_iso(row["modified"]),
                    last_opened=_cocoa_timestamp_to_iso(row["last_opened"]),
                )
            )

        return notes, total

    def get_note_by_name(self, name: str) -> Note:
        """
        Find a note by its title.

        Raises:
            NoteNotFoundError: If note with given name doesn't exist.
        """
        conn = self.connect()
        cur = conn.cursor()

        # Build folder hierarchy for nested folder paths
        folder_info = self._build_folder_hierarchy()

        cur.execute(
            """
            SELECT
                n.Z_PK as pk,
                n.ZIDENTIFIER as uuid,
                n.ZTITLE1 as title,
                n.ZFOLDER as folder_pk,
                n.ZCREATIONDATE3 as created,
                n.ZMODIFICATIONDATE1 as modified,
                n.ZLASTOPENEDDATE as last_opened
            FROM ZICCLOUDSYNCINGOBJECT n
            WHERE n.ZTITLE1 = ?
            AND n.ZMARKEDFORDELETION != 1
        """,
            (name,),
        )

        # First matching row, skipping any outside the remote folder scope
        # (with no scope this is exactly the first row, as before).
        for row in cur.fetchall():
            folder_path = self._get_folder_path(row["folder_pk"], folder_info)
            if not self._visible(row["folder_pk"], folder_path):
                continue
            return Note(
                name=row["title"],
                uuid=row["uuid"] or "",
                folder=folder_path,
                pk=row["pk"],
                created=_cocoa_timestamp_to_iso(row["created"]),
                modified=_cocoa_timestamp_to_iso(row["modified"]),
                last_opened=_cocoa_timestamp_to_iso(row["last_opened"]),
            )
        raise NoteNotFoundError(f"Note '{name}' not found")

    def get_note_by_pk(self, pk: int) -> Note:
        """
        Find a note by its primary key.

        Unlike get_note_by_name, this is unambiguous even when multiple
        notes share the same title (see ZTITLE1 duplicates).

        Raises:
            NoteNotFoundError: If note with given pk doesn't exist.
        """
        conn = self.connect()
        cur = conn.cursor()

        # Build folder hierarchy for nested folder paths
        folder_info = self._build_folder_hierarchy()

        cur.execute(
            """
            SELECT
                n.Z_PK as pk,
                n.ZIDENTIFIER as uuid,
                n.ZTITLE1 as title,
                n.ZFOLDER as folder_pk,
                n.ZCREATIONDATE3 as created,
                n.ZMODIFICATIONDATE1 as modified,
                n.ZLASTOPENEDDATE as last_opened
            FROM ZICCLOUDSYNCINGOBJECT n
            WHERE n.Z_PK = ?
            AND n.ZMARKEDFORDELETION != 1
        """,
            (pk,),
        )

        row = cur.fetchone()
        if not row:
            raise NoteNotFoundError(f"Note with pk '{pk}' not found")

        folder_path = self._get_folder_path(row["folder_pk"], folder_info)
        if not self._visible(row["folder_pk"], folder_path):
            raise NoteNotFoundError(f"Note with pk '{pk}' not found")
        return Note(
            name=row["title"],
            uuid=row["uuid"] or "",
            folder=folder_path,
            pk=row["pk"],
            created=_cocoa_timestamp_to_iso(row["created"]),
            modified=_cocoa_timestamp_to_iso(row["modified"]),
            last_opened=_cocoa_timestamp_to_iso(row["last_opened"]),
        )

    def get_note_by_uuid(self, uuid: str) -> Optional[Note]:
        """Find a note by its UUID."""
        conn = self.connect()
        cur = conn.cursor()

        # Build folder hierarchy for nested folder paths
        folder_info = self._build_folder_hierarchy()

        cur.execute(
            """
            SELECT
                n.Z_PK as pk,
                n.ZIDENTIFIER as uuid,
                n.ZTITLE1 as title,
                n.ZFOLDER as folder_pk,
                n.ZCREATIONDATE3 as created,
                n.ZMODIFICATIONDATE1 as modified,
                n.ZLASTOPENEDDATE as last_opened
            FROM ZICCLOUDSYNCINGOBJECT n
            WHERE UPPER(n.ZIDENTIFIER) = UPPER(?)
            AND n.ZMARKEDFORDELETION != 1
        """,
            (uuid,),
        )

        row = cur.fetchone()
        if not row:
            return None

        folder_path = self._get_folder_path(row["folder_pk"], folder_info)
        if not self._visible(row["folder_pk"], folder_path):
            return None
        return Note(
            name=row["title"],
            uuid=row["uuid"] or "",
            folder=folder_path,
            pk=row["pk"],
            created=_cocoa_timestamp_to_iso(row["created"]),
            modified=_cocoa_timestamp_to_iso(row["modified"]),
            last_opened=_cocoa_timestamp_to_iso(row["last_opened"]),
        )

    def count_notes_titled(self, name: str) -> int:
        """How many notes AppleScript's `show note "<name>"` could open.

        Remote write-queue safety check (issue #169): the UI write path opens
        notes by title, so a write is only safe when exactly one note carries
        it. Deliberately ignores the folder scope and INCLUDES Recently Deleted
        (a trashed note keeps ZMARKEDFORDELETION = 0 and is still reachable by
        title). Returns only a count, never a note; and remote writes can't be
        combined with a folder allowlist (RemoteConfig refuses it), so this
        reveals nothing a scoped server hides. Compares with casefold() because
        SQL LOWER() folds ASCII only.
        """
        wanted = name.casefold()
        cur = self.connect().cursor()
        cur.execute(
            "SELECT ZTITLE1 FROM ZICCLOUDSYNCINGOBJECT"
            " WHERE ZTITLE1 IS NOT NULL AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION != 1)"
        )
        return sum(1 for (title,) in cur.fetchall() if title.casefold() == wanted)

    def count_live_notes_titled(self, name: str) -> int:
        """How many notes OUTSIDE Recently Deleted carry ``name`` (#83).

        The remote write queue's title check for the tools that address a
        note beside trashed copies by id (update_note_content,
        toggle_checklist_item). Scope-exempt like count_notes_titled and
        compared the same way as find_title_matches; returns only a count.
        """
        return sum(1 for m in self.find_title_matches(name) if not m.in_trash)

    def _has_column(self, column: str) -> bool:
        cur = self.connect().cursor()
        cur.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")
        return any(row[1] == column for row in cur.fetchall())

    def find_title_matches(self, name: str) -> List[TitleMatch]:
        """Every note whose title equals ``name`` case-insensitively (#229).

        The delete/move guards' one read: the AppleScript delete addresses
        notes by title (and AppleScript compares titles case-insensitively),
        so a delete or move is only safe when exactly one note could be hit.
        Like count_notes_titled it deliberately ignores the folder scope and
        INCLUDES Recently Deleted (``in_trash``), so a trashed same-titled
        copy is reported rather than silently hit; it returns only titles,
        folders, dates and flags, and remote writes can't be combined with a
        folder allowlist. Purged rows (ZMARKEDFORDELETION = 1) never count.
        """
        # NFC first: casefold() alone leaves composed and decomposed accents
        # ("Café" typed two ways) distinct, while AppleScript may not.
        wanted = unicodedata.normalize("NFC", name).casefold()
        locked_col = (
            "ZISPASSWORDPROTECTED" if self._has_column("ZISPASSWORDPROTECTED") else "0"
        )
        cur = self.connect().cursor()
        cur.execute(
            "SELECT Z_PK AS pk, ZIDENTIFIER AS uuid, ZTITLE1 AS title,"
            " ZFOLDER AS folder_pk, ZMODIFICATIONDATE1 AS modified,"
            f" {locked_col} AS locked"
            " FROM ZICCLOUDSYNCINGOBJECT"
            " WHERE ZTITLE1 IS NOT NULL"
            " AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION != 1)"
            " ORDER BY Z_PK"
        )
        rows = [
            r
            for r in cur.fetchall()
            if unicodedata.normalize("NFC", r["title"]).casefold() == wanted
        ]
        if not rows:
            return []
        folder_info = self._build_folder_hierarchy()
        trash = self._trash_folder_pks()
        return [
            TitleMatch(
                pk=r["pk"],
                uuid=r["uuid"] or "",
                title=r["title"],
                folder=self._get_folder_path(r["folder_pk"], folder_info),
                folder_pk=r["folder_pk"],
                modified=_cocoa_timestamp_to_iso(r["modified"]),
                in_trash=self._in_trash(r["folder_pk"], trash),
                locked=bool(r["locked"]),
            )
            for r in rows
        ]

    def get_move_folders(self) -> List[FolderMatch]:
        """Folders a note can be moved into (#230): ordinary folders only.

        Excludes the trash (ZFOLDERTYPE 1) and anything nested under it, smart
        folders (ZFOLDERTYPE 2, which can't hold notes) and purged folders.
        Honours the remote folder scope.
        """
        cur = self.connect().cursor()
        cur.execute(
            "SELECT Z_PK AS pk FROM ZICCLOUDSYNCINGOBJECT"
            " WHERE ZTITLE2 IS NOT NULL"
            " AND (ZFOLDERTYPE IS NULL OR ZFOLDERTYPE = 0)"
            " AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION != 1)"
        )
        pks = [r["pk"] for r in cur.fetchall()]
        folder_info = self._build_folder_hierarchy()
        trash = self._trash_folder_pks()
        folders = []
        for pk in pks:
            if self._in_trash(pk, trash):
                continue
            path = self._get_folder_path(pk, folder_info)
            if folder_scope.is_active() and not folder_scope.folder_allowed(path):
                continue
            folders.append(FolderMatch(pk=pk, name=folder_info[pk]["name"], path=path))
        return folders

    def store_uuid(self) -> Optional[str]:
        """The NoteStore's Core Data store id (Z_METADATA.Z_UUID).

        AppleScript ids are ``x-coredata://<store>/ICNote/p<pk>`` (and
        ``/ICFolder/p<pk>``), so this addresses a note or folder by pk (#230).
        """
        cur = self.connect().cursor()
        try:
            cur.execute("SELECT Z_UUID FROM Z_METADATA")
        except sqlite3.OperationalError:
            return None
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def link_owner_identifier(self, note_uuid: str) -> Optional[str]:
        """The ownerIdentifier a new link to `note_uuid` must carry (issue #184).

        Write-safety read for synthesized links: returns only an owner id, never
        a note, and deliberately ignores the folder scope (like
        count_notes_titled). A shared note's zone owner (ZZONEOWNERNAME) wins;
        otherwise the ZUSERRECORDNAME of the single iCloud account row
        (ZACCOUNTTYPE = 1); None when neither exists or the account is ambiguous.
        """
        cur = self.connect().cursor()
        cur.execute(
            "SELECT ZZONEOWNERNAME FROM ZICCLOUDSYNCINGOBJECT"
            " WHERE UPPER(ZIDENTIFIER) = UPPER(?) AND ZTITLE1 IS NOT NULL",
            (note_uuid,),
        )
        row = cur.fetchone()
        if row is not None and row[0]:
            return row[0]
        cur.execute(
            "SELECT DISTINCT ZUSERRECORDNAME FROM ZICCLOUDSYNCINGOBJECT"
            " WHERE ZACCOUNTTYPE = 1 AND ZUSERRECORDNAME IS NOT NULL"
            " AND ZUSERRECORDNAME != ''"
        )
        accounts = cur.fetchall()
        return accounts[0][0] if len(accounts) == 1 else None

    def note_fingerprint(self, note_pk: int) -> Optional[str]:
        """SHA-256 over a note's ZDATA and modification date, or None if absent.

        Change detector for queued remote writes (issue #169): conflict checks,
        retry safety and undo compare fingerprints. Ignores the folder scope
        for the same reason as count_notes_titled.
        """
        cur = self.connect().cursor()
        cur.execute(
            "SELECT n.ZMODIFICATIONDATE1 AS modified, d.ZDATA AS data"
            " FROM ZICCLOUDSYNCINGOBJECT n LEFT JOIN ZICNOTEDATA d ON d.ZNOTE = n.Z_PK"
            " WHERE n.Z_PK = ? AND n.ZTITLE1 IS NOT NULL",
            (note_pk,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        digest = hashlib.sha256()
        digest.update(repr(row["modified"]).encode())
        digest.update(b"\0")
        digest.update(row["data"] or b"")
        return digest.hexdigest()

    def get_note_data(self, note_pk: int) -> Optional[bytes]:
        """Get the raw ZDATA blob for a note."""
        if folder_scope.is_active() and not self._pk_in_scope(note_pk):
            return None
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ZDATA
            FROM ZICNOTEDATA
            WHERE ZNOTE = ?
        """,
            (note_pk,),
        )

        row = cur.fetchone()
        if row and row["ZDATA"]:
            return row["ZDATA"]
        return None

    def get_table_data(self, identifier: str) -> Optional[bytes]:
        """Get the CRDT data for a table attachment.

        Args:
            identifier: The table attachment's ZIDENTIFIER

        Returns:
            Raw ZMERGEABLEDATA1 bytes (gzip-compressed protobuf), or None
        """
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ZMERGEABLEDATA1
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE ZIDENTIFIER = ?
            AND ZTYPEUTI = 'com.apple.notes.table'
            """,
            (identifier,),
        )
        row = cur.fetchone()
        if row and row["ZMERGEABLEDATA1"]:
            return row["ZMERGEABLEDATA1"]
        return None

    def get_block_attachment_sizes(self, note_pk: int) -> Dict[str, Optional[int]]:
        """File sizes of a note's live block attachments (images, files, scans,
        tables), keyed by identifier. Block attachments link by ZNOTE; inline
        ones (links, tags) by ZNOTE1 and aren't here."""
        if folder_scope.is_active() and not self._pk_in_scope(note_pk):
            return {}
        cur = self.connect().cursor()
        cur.execute(
            "SELECT ZIDENTIFIER AS identifier, ZFILESIZE AS size"
            " FROM ZICCLOUDSYNCINGOBJECT"
            " WHERE ZNOTE = ? AND ZTYPEUTI IS NOT NULL"
            " AND COALESCE(ZMARKEDFORDELETION, 0) = 0",
            (note_pk,),
        )
        return {row["identifier"]: row["size"] for row in cur.fetchall()}

    def note_media_bytes(self, note_pk: int) -> int:
        """Total file size of a note's live block attachments, in bytes."""
        return sum(
            size or 0 for size in self.get_block_attachment_sizes(note_pk).values()
        )

    def get_attachments_for_note(self, note_pk: int) -> Dict[str, dict]:
        """Get all attachments (including links) for a note, keyed by identifier."""
        if folder_scope.is_active() and not self._pk_in_scope(note_pk):
            return {}
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                ZIDENTIFIER,
                ZTYPEUTI1,
                ZTOKENCONTENTIDENTIFIER,
                ZALTTEXT
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE ZNOTE1 = ?
        """,
            (note_pk,),
        )

        attachments = {}
        for row in cur.fetchall():
            if row["ZIDENTIFIER"]:
                attachments[row["ZIDENTIFIER"]] = {
                    "type_uti": row["ZTYPEUTI1"],
                    "target_url": row["ZTOKENCONTENTIDENTIFIER"],
                    "alt_text": row["ZALTTEXT"],
                }

        return attachments

    def get_note_link_targets(self, note_pk: int) -> List[str]:
        """Target uuids (upper-case) of a note's top-level note links, in text order.

        Reads the note's own ZDATA attachment runs and pairs each link run
        with its attachment row by identifier, so stale soft-deleted rows
        from earlier pastes never match. Unlike NoteContent.note_links this
        keeps links whose target note is gone. It is a write-safety read
        (#183): it only returns uuids that already appear in this note. A
        link whose row has no parseable target yields "".

        Raises:
            EncryptedNoteError / ProtobufParseError: as NoteProtobufParser.parse
        """
        from .models import AttachmentType
        from .protobuf_parser import NoteProtobufParser

        data = self.get_note_data(note_pk)
        if not data:
            return []
        parser = NoteProtobufParser()
        _text, runs = parser.parse(data)
        rows = self.get_attachments_for_note(note_pk)
        targets = []
        for info in parser.extract_attachment_infos(runs):
            if info.type_uti != AttachmentType.LINK.value:
                continue
            url = (rows.get(info.identifier) or {}).get("target_url")
            targets.append(note_link_uuid(url) or "")
        return targets

    def build_uuid_lookup(self) -> Dict[str, Note]:
        """Build a UUID -> Note lookup dictionary for resolving links."""
        notes = self.get_all_notes()
        lookup = {}

        for note in notes:
            if note.uuid:
                # Store both uppercase and lowercase for case-insensitive matching
                lookup[note.uuid.upper()] = note
                lookup[note.uuid.lower()] = note

        return lookup

    def get_all_folders(self) -> List[dict]:
        """
        Get all folders with note counts and hierarchy info.

        Returns:
            List of folder dicts with: name, note_count, parent, full_path
        """
        conn = self.connect()
        cur = conn.cursor()

        # Build folder hierarchy
        folder_info = self._build_folder_hierarchy()

        # Count notes per folder
        cur.execute("""
            SELECT ZFOLDER, COUNT(*) as count
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE ZTITLE1 IS NOT NULL
            AND ZMARKEDFORDELETION != 1
            GROUP BY ZFOLDER
        """)

        note_counts: Dict[Optional[int], int] = {}
        for row in cur.fetchall():
            note_counts[row["ZFOLDER"]] = row["count"]

        folders = []

        # Handle the default "Notes" folder (folder_pk = None)
        default_count = note_counts.get(None, 0)
        if default_count > 0:
            folders.append(
                {
                    "name": "Notes",
                    "note_count": default_count,
                    "parent": None,
                    "full_path": "Notes",
                }
            )

        # Process all other folders
        trash_pks = self._trash_folder_pks() if folder_scope.is_active() else set()
        for folder_pk, info in folder_info.items():
            if folder_scope.is_active() and self._in_trash(folder_pk, trash_pks):
                continue
            folder_name = info["name"]
            parent_pk = info["parent"]
            full_path = self._get_folder_path(folder_pk, folder_info)

            # Get parent name if exists
            parent_name = None
            if parent_pk and parent_pk in folder_info:
                parent_name = folder_info[parent_pk]["name"]

            folders.append(
                {
                    "name": folder_name,
                    "note_count": note_counts.get(folder_pk, 0),
                    "parent": parent_name,
                    "full_path": full_path,
                }
            )

        if folder_scope.is_active():
            folders = [
                f for f in folders if folder_scope.folder_allowed(f["full_path"])
            ]

        # Sort by full_path for consistent output
        folders.sort(key=lambda f: f["full_path"].lower())

        return folders

    def get_raw_note_links(self) -> List[RawNoteLink]:
        """
        Get every note-link attachment row, unclassified.

        Unlike get_all_links, this keeps rows whose target does not resolve
        to an active note, so callers can detect broken links. ZALTTEXT
        carries the target's display title at link-creation time.

        Soft-deleted rows (ZMARKEDFORDELETION = 1) are left out: every
        full-note paste re-creates the note's links and keeps the old rows
        until Notes purges them, so a re-pointed link would otherwise still
        show up as a link to its old target (#183).
        """
        conn = self.connect()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                ZNOTE1 as source_note_pk,
                ZTOKENCONTENTIDENTIFIER as target_url,
                ZALTTEXT as alt_text
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE ZTYPEUTI1 = 'com.apple.notes.inlinetextattachment.link'
            AND ZNOTE1 IS NOT NULL
            AND ZTOKENCONTENTIDENTIFIER IS NOT NULL
            AND (ZMARKEDFORDELETION IS NULL OR ZMARKEDFORDELETION != 1)
        """)

        links = [
            RawNoteLink(
                source_pk=row["source_note_pk"],
                target_url=row["target_url"],
                alt_text=row["alt_text"],
            )
            for row in cur.fetchall()
        ]
        if folder_scope.is_active():
            links = self._scope_links(links)
        return links

    def _scope_links(self, links: List[RawNoteLink]) -> List[RawNoteLink]:
        """Keep links from visible notes, minus those to live notes that are hidden.

        A link into a hidden folder is not broken, so it must not surface in
        find_broken_links (which would also leak the hidden note's title);
        links whose target no longer exists anywhere stay, so real breakage
        is still reported.
        """
        visible = self.get_all_notes()
        visible_pks = {n.pk for n in visible}
        hidden = self.hidden_note_uuids(visible)
        kept = []
        for link in links:
            if link.source_pk not in visible_pks:
                continue
            if note_link_uuid(link.target_url) in hidden:
                continue
            kept.append(link)
        return kept

    def hidden_note_uuids(self, visible: Optional[List[Note]] = None) -> set:
        """Upper-case uuids of live notes the folder scope hides.

        Notes under the trash, however deeply, are gone rather than hidden. The
        result is out of scope by design: use it to withhold, never to show.
        """
        if visible is None:
            visible = self.get_all_notes()
        visible_uuids = {n.uuid.upper() for n in visible if n.uuid}
        cur = self.connect().cursor()
        cur.execute(f"""
            SELECT n.ZIDENTIFIER, n.ZFOLDER FROM ZICCLOUDSYNCINGOBJECT n
            WHERE n.ZTITLE1 IS NOT NULL
            AND n.ZMARKEDFORDELETION != 1
            {self._NOT_IN_TRASH}
        """)
        trash_pks = self._trash_folder_pks()
        return {
            r[0].upper()
            for r in cur.fetchall()
            if r[0]
            and r[0].upper() not in visible_uuids
            and not self._in_trash(r[1], trash_pks)
        }

    def get_all_links(self) -> Tuple[List[Note], List[GraphLink]]:
        """
        Get all notes and links between them efficiently.

        This queries links directly from the database without parsing protobuf,
        making it much faster for graph visualization. Links whose target note
        no longer exists are dropped; use get_raw_note_links + resolve_note_links
        to see them.

        Returns:
            Tuple of (notes, links) for graph rendering
        """
        # Deferred import: librarian imports NotesDatabase at module level.
        from .librarian import resolve_note_links

        notes = self.get_all_notes()
        links, _broken = resolve_note_links(self.get_raw_note_links(), notes)
        return notes, links
