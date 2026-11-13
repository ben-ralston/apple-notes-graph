"""Persistent queue for remote write tools (issue #169).

Remote writes are accepted into ``queue.sqlite`` and applied later through the
unchanged stdio write pipeline (write_apply / write_worker). This module is
only the store: records, status transitions, caps, expiry and the drain lock.

Concurrency: HTTP worker threads, the drain thread and the ``writes`` CLI (a
separate process) all touch the database, so every call opens its own
connection (WAL, busy_timeout). ``claim`` is a single conditional UPDATE, so a
record is applied at most once; the ``drain.lock`` flock keeps two UI writes
from ever running at the same time.

The file holds write arguments (note text), hence the 0700 dir / 0600 file.
"""

import fcntl
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from apple_notes.config import CONFIG_DIR

DEFAULT_STATE_DIR = CONFIG_DIR / "write-queue"

ALL_OPERATIONS = frozenset(
    {
        "add_note",
        "append",
        "find_replace",
        "replace",
        "toggle_checklist_item",
        "replace_note_link",
        "edit_note_section",
        "section_delete",
        "delete_note",
        "move_note",
    }
)
# applied_unverified (#200): the write changed the note, but part of it isn't
# confirmed there. Final, never retried, undoable like applied.
APPLIED_STATUSES = frozenset({"applied", "applied_unverified"})
FINAL_STATUSES = APPLIED_STATUSES | {"conflict", "failed", "expired", "rejected", "undone"}
STATUSES = FINAL_STATUSES | {"queued", "applying"}

_COLUMNS = (
    "id",
    "seq",
    "tool",
    "operation",
    "args_json",
    "note_title",
    "note_key",
    "note_uuid",
    "base_fingerprint",
    "claim_fingerprint",
    "post_fingerprint",
    "desired_checked",
    "item_position",
    "status",
    "reason",
    "attempts",
    "base_changed",
    "noop",
    "prior_has_attachments",
    "prior_markdown",
    "created_at",
    "applied_at",
    "expires_at",
    "subject",
    "relink_prior_new_count",
    "section_fingerprint",
    "post_section_fingerprint",
    "post_section_text",
)
_UPDATABLE = frozenset(_COLUMNS) - {"id", "seq", "created_at"}


@dataclass(frozen=True)
class WriteQueueConfig:
    """The ``write_queue`` block of remote.json (all optional)."""

    state_dir: Path = DEFAULT_STATE_DIR
    auto_apply: bool = True
    apply_delay_after_unlock_seconds: int = 60
    max_body_chars: int = 20_000
    max_old_text_chars: int = 2_000
    max_per_day: int = 100
    max_pending: int = 50
    max_pending_per_note: int = 10
    pending_ttl_seconds: int = 48 * 3600
    purge_after_seconds: int = 7 * 24 * 3600
    allowed_operations: frozenset = field(default_factory=lambda: ALL_OPERATIONS)


class CapExceeded(Exception):
    """A remote write was refused by a configured cap."""

    def __init__(self, cap: str, message: str):
        super().__init__(message)
        self.cap = cap


class WriteQueue:
    def __init__(
        self, config: WriteQueueConfig, *, clock: Callable[[], float] = time.time
    ):
        self.config = config
        self.clock = clock
        self.state_dir = Path(config.state_dir)
        self.path = self.state_dir / "queue.sqlite"
        self._lock_path = self.state_dir / "drain.lock"
        self._ensure()

    # ------------------------------------------------------------ storage
    def _ensure(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        os.chmod(self.path, 0o600)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS writes ("
                " id TEXT PRIMARY KEY, seq INTEGER NOT NULL, tool TEXT NOT NULL,"
                " operation TEXT NOT NULL, args_json TEXT, note_title TEXT NOT NULL,"
                " note_key TEXT NOT NULL, note_uuid TEXT, base_fingerprint TEXT,"
                " claim_fingerprint TEXT, post_fingerprint TEXT, desired_checked INTEGER,"
                " item_position INTEGER, status TEXT NOT NULL, reason TEXT,"
                " attempts INTEGER NOT NULL DEFAULT 0, base_changed INTEGER NOT NULL DEFAULT 0,"
                " noop INTEGER NOT NULL DEFAULT 0, prior_has_attachments INTEGER NOT NULL DEFAULT 0,"
                " prior_markdown TEXT, created_at REAL NOT NULL, applied_at REAL,"
                " expires_at REAL NOT NULL, subject TEXT,"
                " relink_prior_new_count INTEGER NOT NULL DEFAULT 0,"
                " section_fingerprint TEXT, post_section_fingerprint TEXT,"
                " post_section_text TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            have = {row[1] for row in conn.execute("PRAGMA table_info(writes)")}
            for column in ("noop", "prior_has_attachments", "relink_prior_new_count"):
                if column not in have:  # a queue file from an older release
                    conn.execute(f"ALTER TABLE writes ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            for column in (
                "section_fingerprint",
                "post_section_fingerprint",
                "post_section_text",
            ):
                if column not in have:
                    conn.execute(f"ALTER TABLE writes ADD COLUMN {column} TEXT")
        for suffix in ("-wal", "-shm"):
            side = self.path.with_name(self.path.name + suffix)
            if side.exists():
                os.chmod(side, 0o600)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        rec = dict(row)
        rec["args"] = json.loads(rec.pop("args_json")) if rec.get("args_json") else None
        for flag in ("desired_checked", "base_changed", "noop", "prior_has_attachments"):
            if rec.get(flag) is not None:
                rec[flag] = bool(rec[flag])
        return rec

    # ------------------------------------------------------------ accept
    def check_static_caps(self, record: dict) -> None:
        """The caps that need no database: operation allowlist and sizes.

        accept() calls this before resolving the note, so oversized or
        disallowed calls are refused before any NoteStore scan or note parse.
        """
        cfg = self.config
        args = record.get("args") or {}
        if record["operation"] not in cfg.allowed_operations:
            raise CapExceeded(
                "allowed_operations",
                f"{record['operation']} is not enabled for remote writes",
            )
        body = args.get("body")
        if isinstance(body, str) and len(body) > cfg.max_body_chars:
            raise CapExceeded(
                "max_body_chars",
                f"body is {len(body)} characters; the remote limit is {cfg.max_body_chars}",
            )
        old_text = args.get("old_text")
        if isinstance(old_text, str) and len(old_text) > cfg.max_old_text_chars:
            raise CapExceeded(
                "max_old_text_chars",
                f"old_text is {len(old_text)} characters; the remote limit is {cfg.max_old_text_chars}",
            )

    def admit(self, record: dict) -> str:
        """Check the caps and insert a queued record; return its id.

        Raises CapExceeded. The counts and the insert run in one IMMEDIATE
        transaction, so concurrent submits can't both slip under a cap.
        """
        self.check_static_caps(record)
        cfg = self.config
        args = record.get("args") or {}
        now = self.clock()
        note_key = record["note_title"].casefold()
        wid = "w_" + secrets.token_hex(6)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:

                def count(sql: str, *params) -> int:
                    return conn.execute(sql, params).fetchone()[0]

                if (
                    count(
                        "SELECT COUNT(*) FROM writes WHERE created_at > ?", now - 86400
                    )
                    >= cfg.max_per_day
                ):
                    raise CapExceeded(
                        "max_per_day",
                        f"more than {cfg.max_per_day} remote writes in the last day",
                    )
                pending = "status IN ('queued', 'applying')"
                if (
                    count(
                        f"SELECT COUNT(*) FROM writes WHERE {pending} AND note_key = ?",
                        note_key,
                    )
                    >= cfg.max_pending_per_note
                ):
                    raise CapExceeded(
                        "max_pending_per_note",
                        f"{cfg.max_pending_per_note} writes are already pending for this note",
                    )
                if (
                    count(f"SELECT COUNT(*) FROM writes WHERE {pending}")
                    >= cfg.max_pending
                ):
                    raise CapExceeded(
                        "max_pending",
                        f"{cfg.max_pending} remote writes are already pending",
                    )
                seq = count("SELECT COALESCE(MAX(seq), 0) FROM writes") + 1
                conn.execute(
                    "INSERT INTO writes (id, seq, tool, operation, args_json, note_title, note_key,"
                    " note_uuid, base_fingerprint, desired_checked, item_position, status,"
                    " created_at, expires_at, subject, section_fingerprint)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                    (
                        wid,
                        seq,
                        record["tool"],
                        record["operation"],
                        json.dumps(args),
                        record["note_title"],
                        note_key,
                        record.get("note_uuid"),
                        record.get("base_fingerprint"),
                        None
                        if record.get("desired_checked") is None
                        else int(record["desired_checked"]),
                        record.get("item_position"),
                        now,
                        now + cfg.pending_ttl_seconds,
                        record.get("subject"),
                        record.get("section_fingerprint"),
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return wid

    # ------------------------------------------------------------ reads
    def get(self, wid: str) -> Optional[dict]:
        with self._conn() as conn:
            return self._row(
                conn.execute("SELECT * FROM writes WHERE id = ?", (wid,)).fetchone()
            )

    def list(self, limit: Optional[int] = 20) -> list[dict]:
        sql = "SELECT * FROM writes ORDER BY seq DESC"
        with self._conn() as conn:
            rows = conn.execute(
                sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ()
            ).fetchall()
        return [self._row(r) for r in rows]

    def pending(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM writes WHERE status = 'queued' ORDER BY seq"
            ).fetchall()
        return [self._row(r) for r in rows]

    def queued_ahead(self, wid: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM writes WHERE status IN ('queued', 'applying')"
                " AND seq < (SELECT seq FROM writes WHERE id = ?)",
                (wid,),
            ).fetchone()[0]

    # ------------------------------------------------------------ transitions
    def claim(self, wid: str) -> bool:
        """queued -> applying, atomically. True only for the one winner.

        An overdue record is never claimed: it becomes 'expired' here, so the
        pending limit holds even when only the CLI drains (server stopped).
        """
        now = self.clock()
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE writes SET status = 'applying'"
                " WHERE id = ? AND status = 'queued' AND expires_at > ?",
                (wid, now),
            )
            if cur.rowcount == 1:
                return True
            conn.execute(
                "UPDATE writes SET status = 'expired', reason = 'not applied within the pending limit'"
                " WHERE id = ? AND status = 'queued' AND expires_at <= ?",
                (wid, now),
            )
            return False

    def update(self, wid: str, **fields) -> None:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown status {fields['status']!r}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        with self._conn() as conn:
            conn.execute(f"UPDATE writes SET {sets} WHERE id = ?", (*values, wid))

    def finish(
        self, wid: str, status: str, reason: Optional[str] = None, **fields
    ) -> None:
        if status not in FINAL_STATUSES:
            raise ValueError(f"{status!r} is not a final status")
        if status in APPLIED_STATUSES:
            fields.setdefault("applied_at", self.clock())
        self.update(wid, status=status, reason=reason, **fields)

    def requeue(
        self, wid: str, *, count_attempt: bool = False, reason: Optional[str] = None
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE writes SET status = 'queued', reason = ?, attempts = attempts + ?"
                " WHERE id = ?",
                (reason, 1 if count_attempt else 0, wid),
            )

    def note_waiting(self, wid: str, reason: str) -> None:
        """Set why a still-queued write is held, without touching its status.

        Unlike ``requeue`` (which follows a claim), this can race a ``clear``
        from another process, so it must never move a final record back to
        queued.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE writes SET reason = ? WHERE id = ? AND status = 'queued'",
                (reason, wid),
            )

    def expire(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "UPDATE writes SET status = 'expired', reason = 'not applied within the pending limit'"
                " WHERE status = 'queued' AND expires_at <= ?",
                (self.clock(),),
            ).rowcount

    def purge(self) -> int:
        """Drop payloads (args, journal) of final records past the purge age."""
        final = ", ".join(f"'{s}'" for s in sorted(FINAL_STATUSES))
        with self._conn() as conn:
            return conn.execute(
                f"UPDATE writes SET args_json = NULL, prior_markdown = NULL,"
                f" post_section_text = NULL"
                f" WHERE status IN ({final}) AND created_at <= ?"
                f" AND (args_json IS NOT NULL OR prior_markdown IS NOT NULL"
                f" OR post_section_text IS NOT NULL)",
                (self.clock() - self.config.purge_after_seconds,),
            ).rowcount

    def clear(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "UPDATE writes SET status = 'rejected', reason = 'cleared by the owner'"
                " WHERE status = 'queued'"
            ).rowcount

    def fail_stuck_applying(self) -> int:
        """Mark records left 'applying' by a dead drainer as failed (never retried).

        Only when nobody holds the drain lock: a live drainer's record is not stuck.
        """
        with self.drain_lock() as acquired:
            if not acquired:
                return 0
            with self._conn() as conn:
                return conn.execute(
                    "UPDATE writes SET status = 'failed',"
                    " reason = 'interrupted while applying; check the note'"
                    " WHERE status = 'applying'"
                ).rowcount

    # ------------------------------------------------------------ pause + lock
    def paused(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'paused'").fetchone()
        return bool(row and row[0] == "1")

    def set_paused(self, paused: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('paused', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if paused else "0",),
            )

    @contextmanager
    def drain_lock(self) -> Iterator[bool]:
        """Non-blocking exclusive flock; yields whether it was acquired."""
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
