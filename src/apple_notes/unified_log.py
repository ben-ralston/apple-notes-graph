"""Best-effort bridge to the unified logger (issue #147).

The ONLY module that touches the vendored client (``_vendor/logger.py``).
Nothing here may ever raise into a caller or block a response path on
network I/O: long-lived processes (MCP server, UI helper daemon) deliver on a
fire-and-forget daemon thread; short-lived IPC scripts switch to a synchronous
local queue append (``configure(enqueue_only=True)``) — the shared queue at
``~/Library/Application Support/notes-graph/logger-queue.jsonl`` is drained by any later ``log()`` call
from any process on the machine.

Classification: user-input errors (note/folder not found, encrypted note,
invalid parameters, bare ToolError validation) are never logged; system
faults (parse errors, the UIScriptingError family, OSError, unexpected
exceptions) log at error level. Off unless ``LOGGER_URL`` names a collector
endpoint; kill switch: ``NOTES_GRAPH_UNIFIED_LOG=0``.
"""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import threading
import time

from ._vendor import logger as _client

APP = "notes-graph"

# Non-faults: expected outcomes of bad input, note state, or routine user
# environment (locked screen, user stole focus) — never bugs. Logging them
# would let the log sentinel auto-file bogus issues.
_NON_FAULT_TYPES = {
    "NoteNotFoundError",
    "FolderNotFoundError",
    "EncryptedNoteError",
    # delete_note / move_note guards (#229, #230): caller-facing refusals.
    "AmbiguousNoteTitleError",
    "AmbiguousFolderError",
    "NoteInTrashError",
    "InvalidGraphParameterError",
    "SessionLockedError",
    "NotesNotFrontmostError",
    # Section edits (#184): caller-facing refusals, not system faults.
    "SectionError",
    "MediaCapExceeded",
    "NoteBusyError",
    "FragmentError",
    # A dialog left open in Notes (issue #199) is screen state, not a bug.
    "NotesModalOpenError",
    # Apple Reminders (#171): no grant yet is setup, not a bug.
    "RemindersAccessError",
}

# Content-free reason codes (issue #255): (exception type name, message
# prefix, code). Remote mode logs the exception TYPE only, so one cause split
# into several sentinel issues (#251 vs #253). A known message maps to a
# constant code that both modes put in the message, so the local and remote
# forms of one failure fingerprint alike. Only the constant is ever logged,
# never a slice of the exception text. Matching is scoped by type and anchored
# at the start of the text, so the phrase inside a title doesn't match.
_REASON_CODES: tuple[tuple[str, str, str], ...] = (
    ("OperationalError", "unable to open database file", "db_unable_to_open"),
    ("OperationalError", "database is locked", "db_locked"),
    ("OperationalError", "disk i/o error", "db_disk_io_error"),
    ("OperationalError", "attempt to write a readonly database", "db_readonly"),
    ("DatabaseError", "database disk image is malformed", "db_malformed"),
    ("ClipboardError", "pasteboard read timed out", "pasteboard_read_timeout"),
)


# Fallback for any other sqlite3 error: Python 3.11+ sets ``sqlite_errorname``
# to SQLite's own result-code name (SQLITE_BUSY, SQLITE_NOTADB, ...), a
# constant, never message text. Validated before use; logged lowercased.
_SQLITE_ERRORNAME = re.compile(r"SQLITE_[A-Z0-9_]{1,40}")


def reason_codes() -> tuple[str, ...]:
    """Every code the message table can emit (``sqlite_*`` codes come from
    ``sqlite_errorname`` on top of these)."""
    return tuple(code for _, _, code in _REASON_CODES)


def _reason_match(exc: BaseException) -> tuple[str | None, BaseException | None]:
    """(code, the exception that set it) or (None, None). Starts at the root
    error (the real fault behind a ToolError wrapper) and follows explicit
    causes only: the implicit context of an error handled earlier is a
    different failure and must not lend it its code."""
    node: BaseException | None = _root_error(exc)
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        name = type(node).__name__
        text = str(node).lstrip().lower()
        for type_name, prefix, code in _REASON_CODES:
            if name == type_name and text.startswith(prefix):
                return code, node
        errorname = getattr(node, "sqlite_errorname", None)
        if (
            isinstance(node, sqlite3.Error)
            and isinstance(errorname, str)
            and _SQLITE_ERRORNAME.fullmatch(errorname)
        ):
            return errorname.lower(), node
        node = node.__cause__
    return None, None


def reason_code(exc: BaseException) -> str | None:
    """The content-free reason code for ``exc``, else None. Never raises."""
    try:
        return _reason_match(exc)[0]
    except Exception:
        return None


# Marker set on exceptions we've emitted, so a failure that crosses several
# wire points in one process (profiler hook, then a script's catch-all)
# produces one event, not two.
_LOGGED_MARKER = "_notes_graph_unified_logged"

_component = "mcp"
_enqueue_only = False
# Remote mode (#170): events carry the action and exception TYPE only — the
# exception text and the profiler's detail (``name="<note title>"``) can hold
# note titles or contents, which must never leave the Mac from that server.
_redact = False
_seen_parse_failures: set[tuple[str, object]] = set()

# A systemic fault (e.g. an OS update breaking the parser for every note)
# must not turn into a thread/fd storm or unbounded queue spam: delivery
# threads are capped (excess events fall back to the local enqueue), and
# parse-error events are capped per process (the sentinel's times_seen still
# counts recurrence across processes).
_MAX_LIVE_THREADS = 8
_MAX_PARSE_EVENTS = 20
_live_threads: list[threading.Thread] = []
_threads_guard = threading.Lock()

# The queue flock is machine-global and the vendored client holds it across
# network replay (~15s worst case). A synchronous enqueue must never inherit
# that stall — bounded non-blocking attempts, then drop (best-effort).
_ENQUEUE_LOCK_ATTEMPTS = 5
_ENQUEUE_LOCK_WAIT_S = 0.05


def configure(
    component: str | None = None,
    enqueue_only: bool | None = None,
    redact: bool | None = None,
) -> None:
    """Set process-level emission context. Called once at entry points:
    the daemon passes ``component="daemon"``; short-lived IPC scripts pass
    ``component="script:<name>", enqueue_only=True``; the remote server
    passes ``component="remote", redact=True``. The MCP server keeps the
    defaults."""
    global _component, _enqueue_only, _redact
    if component is not None:
        _component = component
    if enqueue_only is not None:
        _enqueue_only = enqueue_only
    if redact is not None:
        _redact = redact


def _enabled() -> bool:
    # Opt-in: with no collector endpoint configured nothing is reported or queued.
    if not os.environ.get("LOGGER_URL"):
        return False
    return os.environ.get("NOTES_GRAPH_UNIFIED_LOG", "1").lower() not in (
        "0",
        "false",
        "no",
    )


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception chain starting at ``exc``, following ``__cause__`` and
    (unless suppressed with ``raise ... from None``) ``__context__``."""
    chain = []
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        if node.__cause__ is not None:
            node = node.__cause__
        elif not node.__suppress_context__:
            node = node.__context__
        else:
            node = None
    return chain


def should_report(exc: BaseException) -> bool:
    """True when ``exc`` is a system fault worth an error-level event.

    The top exception classifies itself; only a ``ToolError`` (the wrapper
    MCP tools re-raise everything as) is unwrapped through its chain: a
    ToolError wrapping a NoteNotFoundError is user input, one wrapping a
    ProtobufParseError is a real fault, and a bare ToolError is parameter
    validation. A real fault raised WHILE handling a user error (top is not
    ToolError) is never suppressed by the user-error name in its context.
    """
    if not isinstance(exc, Exception):
        return False  # KeyboardInterrupt / CancelledError / SystemExit
    top = type(exc).__name__
    if top in _NON_FAULT_TYPES:
        return False
    if top != "ToolError":
        return True
    wrapped = [type(node).__name__ for node in _chain(exc)[1:]]
    if any(name in _NON_FAULT_TYPES for name in wrapped):
        return False
    if all(name == "ToolError" for name in wrapped):  # bare / validation
        return False
    return True


def _root_error(exc: BaseException) -> BaseException:
    """The first non-ToolError exception in the chain — the real fault behind
    the generic wrapper — else ``exc`` itself."""
    for node in _chain(exc):
        if type(node).__name__ != "ToolError":
            return node
    return exc


def mark_reported(exc: BaseException) -> None:
    """Mark ``exc`` as already reported so later wire points skip it (used by
    the daemon client: the daemon side already emitted the event)."""
    try:
        setattr(exc, _LOGGED_MARKER, True)
    except Exception:
        pass


def _send(level: str, message: str, json_extra: dict | None) -> None:
    try:
        _client.log(APP, level, message, json_extra=json_extra)
    except Exception:
        pass  # observability must never matter


def _enqueue(level: str, message: str, json_extra: dict | None) -> None:
    """Synchronous local append (no network), safe in a process that exits
    immediately after. Takes the client's queue lock non-blocking with a few
    short retries — the vendored client holds it across network replay, and a
    best-effort event is worth dropping over a user-visible stall."""
    event = _client._build_event(APP, level, message, json_extra, None)
    queue_file = _client._queue_file()
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = queue_file.with_name(queue_file.name + ".lock")
    with open(lock_path, "w") as lock:
        for _ in range(_ENQUEUE_LOCK_ATTEMPTS):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(_ENQUEUE_LOCK_WAIT_S)
        else:
            return  # contended the whole window: drop the event
        _client._append_queue(queue_file, event)


def _emit(
    level: str, message: str, json_extra: dict | None, enqueue: bool = False
) -> threading.Thread | None:
    if _enqueue_only or enqueue:
        _enqueue(level, message, json_extra)
        return None
    with _threads_guard:
        _live_threads[:] = [t for t in _live_threads if t.is_alive()]
        spill = len(_live_threads) >= _MAX_LIVE_THREADS
        if not spill:
            thread = threading.Thread(
                target=_send,
                args=(level, message, json_extra),
                daemon=True,
                name="unified-log",
            )
            thread.start()
            _live_threads.append(thread)
            return thread
    _enqueue(level, message, json_extra)
    return None


def report_failure(
    action: str,
    detail: str = "",
    exc: BaseException | None = None,
    elapsed_ms: int | None = None,
) -> threading.Thread | None:
    """Emit one error event for a failed action. Never raises; returns the
    delivery thread when one was spawned (test seam), else None."""
    try:
        if not _enabled() or exc is None or not should_report(exc):
            return None
        if getattr(exc, _LOGGED_MARKER, False):
            return None
        mark_reported(exc)  # __slots__ exceptions just lose dedup, not the event
        # error_type is the real fault behind an MCP ToolError wrapper — the
        # field the sentinel groups on must not read "ToolError" for every
        # tool failure.
        root = _root_error(exc)
        error_type = type(root).__name__
        extra: dict = {
            "action": action,
            "error_type": error_type,
            "component": _component,
        }
        if detail and not _redact:
            extra["detail"] = detail
        if elapsed_ms is not None:
            extra["elapsed_ms"] = elapsed_ms
        message = f"{action} failed: {error_type}"
        try:
            reason, source = _reason_match(exc)
        except Exception:
            reason, source = None, None
        if reason is not None:
            # Same message in both modes (#255): the sentinel fingerprints
            # the message, so the text goes in json_extra, locally only.
            extra["reason"] = reason
            message += f" ({reason})"
            if not _redact:
                extra["error_text"] = str(root)
                if source is not root:
                    extra["cause_text"] = str(source)
        elif not _redact:
            message += f": {root}"
        return _emit("error", message, extra)
    except Exception:
        return None


def log_parse_error(site: str, note_ref, exc: Exception) -> threading.Thread | None:
    """Error event for a swallowed per-note parse failure, once per
    (site, note) per process — two known-bad notes would otherwise fire on
    every graph fetch. Cross-process recurrence still shows up upstream via
    the sentinel's fingerprint times_seen."""
    try:
        if not _enabled() or not should_report(exc):
            return None
        key = (site, note_ref)
        if key in _seen_parse_failures:
            return None
        if len(_seen_parse_failures) >= _MAX_PARSE_EVENTS:
            return None  # systemic fault: enough signal, don't spam
        _seen_parse_failures.add(key)
        extra = {
            "site": site,
            "note": str(note_ref),
            "error_type": type(exc).__name__,
            "component": _component,
        }
        message = f"{site}: parse failed for note {note_ref}: {type(exc).__name__}"
        if not _redact:
            message += f": {exc}"
        return _emit("error", message, extra)
    except Exception:
        return None


def log_error(
    message: str, json_extra: dict | None = None, sync: bool = False
) -> threading.Thread | None:
    """Error event with a caller-chosen stable message (remote mode, #170).
    ``sync=True`` delivers in the caller's thread (the vendored client queues
    locally on failure) — used right before process exit, where the event
    must not wait for some later process to drain the queue."""
    try:
        if not _enabled():
            return None
        extra = dict(json_extra or {})
        extra.setdefault("component", _component)
        if sync and not _enqueue_only:
            _send("error", message, extra)
            return None
        return _emit("error", message, extra)
    except Exception:
        return None


def log_info(
    message: str, json_extra: dict | None = None, enqueue: bool = False
) -> threading.Thread | None:
    """Info-level event (daemon lifecycle). ``enqueue=True`` forces the
    synchronous local append — used right before process exit, where a
    delivery thread would be killed mid-flight."""
    try:
        if not _enabled():
            return None
        extra = dict(json_extra or {})
        extra.setdefault("component", _component)
        return _emit("info", message, extra, enqueue=enqueue)
    except Exception:
        return None
