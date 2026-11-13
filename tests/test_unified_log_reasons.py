"""Content-free reason codes on error events (issue #255).

Remote mode logs the exception TYPE only, so one cause (a 90s pasteboard
timeout, an unreadable NoteStore) split into several sentinel issues: the
local event carried its message, the remote one didn't, and the sentinel
fingerprints the message. A fixed allowlist maps known messages to constant
codes; both modes then log the same message. Anything off the list stays
type-only in remote mode, and no live exception text is ever echoed.
"""

import json
import logging
import re
import sqlite3
from pathlib import Path

import pytest

from apple_notes import unified_log
from apple_notes.exceptions import ClipboardError, ProtobufParseError
from apple_notes_mcp.remote import logger_events

SECRET = "Diary 2026 Secret Title"


class ToolError(Exception):
    """Stand-in matching FastMCP's ToolError by name (classification is by name)."""


@pytest.fixture(autouse=True)
def isolated_logger(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGGER_QUEUE_FILE", str(tmp_path / "queue.jsonl"))
    monkeypatch.setenv("LOGGER_TOKEN_FILE", str(tmp_path / "missing-token"))
    monkeypatch.delenv("NOTES_GRAPH_UNIFIED_LOG", raising=False)
    monkeypatch.setenv("LOGGER_URL", "http://127.0.0.1:9")  # never contacted: no token
    unified_log.configure(component="test", enqueue_only=True, redact=False)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    unified_log.configure(component="mcp", enqueue_only=False, redact=False)


def _events(tmp_path):
    queue = tmp_path / "queue.jsonl"
    if not queue.exists():
        return []
    return [json.loads(line) for line in queue.read_text().splitlines() if line]


def _extra(event):
    extra = event.get("json_extra")
    return json.loads(extra) if isinstance(extra, str) else extra


def _wrapped(inner: BaseException) -> ToolError:
    """A ToolError raised from ``inner``, the way MCP tools re-raise."""
    try:
        try:
            raise inner
        except BaseException as exc:
            raise ToolError("Error updating note") from exc
    except ToolError as top:
        return top


def _fresh(exc: BaseException) -> BaseException:
    """A new instance: report_failure marks the one it reports, so a shared
    parametrize instance would be skipped as already reported."""
    return type(exc)(*exc.args)


# The real messages behind #157/#190 (sqlite3) and #251/#253 (ClipboardError).
KNOWN = [
    (sqlite3.OperationalError("unable to open database file"), "db_unable_to_open"),
    (sqlite3.OperationalError("database is locked"), "db_locked"),
    (sqlite3.OperationalError("disk I/O error"), "db_disk_io_error"),
    (
        sqlite3.OperationalError("attempt to write a readonly database"),
        "db_readonly",
    ),
    (sqlite3.DatabaseError("database disk image is malformed"), "db_malformed"),
    (ClipboardError("Pasteboard read timed out after 90s"), "pasteboard_read_timeout"),
]


@pytest.mark.parametrize("exc,code", KNOWN)
def test_remote_mode_logs_the_allowlisted_reason_code(tmp_path, exc, code):
    exc = _fresh(exc)
    unified_log.configure(component="remote", redact=True)
    unified_log.report_failure("edit_note_section", f'name="{SECRET}"', exc)
    [event] = _events(tmp_path)
    error_type = type(exc).__name__
    assert event["message"] == f"edit_note_section failed: {error_type} ({code})"
    extra = _extra(event)
    assert extra["reason"] == code
    assert extra["error_type"] == error_type
    assert "error_text" not in extra and "detail" not in extra
    assert SECRET not in json.dumps(event)


def test_reason_is_found_behind_a_toolerror_wrapper(tmp_path):
    unified_log.configure(component="remote", redact=True)
    exc = _wrapped(ClipboardError("Pasteboard read timed out after 90s"))
    unified_log.report_failure("update_note_content", exc=exc)
    [event] = _events(tmp_path)
    assert _extra(event)["reason"] == "pasteboard_read_timeout"
    assert event["message"] == (
        "update_note_content failed: ClipboardError (pasteboard_read_timeout)"
    )


@pytest.mark.parametrize(
    "exc",
    [
        # Arbitrary messages: nothing on the allowlist.
        sqlite3.OperationalError(f"no such table: {SECRET}"),
        ProtobufParseError(f"bad field in {SECRET}"),
        # An allowlisted phrase inside other text (e.g. a title) is not a match.
        sqlite3.OperationalError(f'near "{SECRET} database is locked": syntax error'),
        # Right phrase, wrong type: the allowlist is scoped by exception type.
        ClipboardError("database is locked"),
        ProtobufParseError("Pasteboard read timed out after 90s"),
    ],
)
def test_arbitrary_message_is_not_logged_in_remote_mode(tmp_path, exc):
    exc = _fresh(exc)
    unified_log.configure(component="remote", redact=True)
    unified_log.report_failure("write_queue", "drain pass", exc)
    [event] = _events(tmp_path)
    error_type = type(exc).__name__
    assert event["message"] == f"write_queue failed: {error_type}"
    extra = _extra(event)
    assert "reason" not in extra and "error_text" not in extra
    assert SECRET not in json.dumps(event)
    assert str(exc) not in json.dumps(event)


def test_reason_codes_are_constant_slugs():
    codes = unified_log.reason_codes()
    assert codes, "allowlist must not be empty"
    for code in codes:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", code), code


@pytest.mark.parametrize("exc,code", KNOWN)
def test_local_mode_keeps_the_text_out_of_the_message(tmp_path, exc, code):
    exc = _fresh(exc)
    unified_log.configure(component="daemon", redact=False)
    unified_log.report_failure("edit_note_section", 'name="Scratch"', exc)
    [event] = _events(tmp_path)
    extra = _extra(event)
    assert extra["reason"] == code
    # The raw text stays available locally, outside the fingerprinted message.
    assert extra["error_text"] == str(exc)
    assert extra["detail"] == 'name="Scratch"'
    assert event["message"] == (
        f"edit_note_section failed: {type(exc).__name__} ({code})"
    )


def test_local_mode_without_a_reason_is_unchanged(tmp_path):
    unified_log.configure(component="mcp", redact=False)
    unified_log.report_failure("search_notes", exc=ProtobufParseError("bad varint"))
    [event] = _events(tmp_path)
    assert event["message"] == "search_notes failed: ProtobufParseError: bad varint"
    assert "reason" not in _extra(event) and "error_text" not in _extra(event)


def _report(tmp_path, component, redact, exc):
    queue = tmp_path / "queue.jsonl"
    if queue.exists():
        queue.unlink()
    unified_log.configure(component=component, redact=redact)
    unified_log.report_failure("edit_note_section", f'name="{SECRET}"', exc)
    [event] = _events(tmp_path)
    return event


@pytest.mark.parametrize(
    "exc_factory,code",
    [
        (
            lambda: sqlite3.OperationalError("unable to open database file"),
            "db_unable_to_open",
        ),
        (
            lambda: ClipboardError("Pasteboard read timed out after 90s"),
            "pasteboard_read_timeout",
        ),
    ],
)
def test_local_and_remote_forms_share_a_message(tmp_path, exc_factory, code):
    local = _report(tmp_path, "daemon", False, exc_factory())
    remote = _report(tmp_path, "remote", True, exc_factory())
    # The sentinel fingerprints app + level + message (+ the row's month).
    assert local["message"] == remote["message"]
    assert local["app"] == remote["app"] and local["level"] == remote["level"]



def test_an_earlier_handled_error_does_not_lend_its_code(tmp_path):
    """Only explicit causes (``raise X from Y``) count, never the implicit
    context of an error handled earlier, or the code would misname the fault."""
    unified_log.configure(component="remote", redact=True)
    try:
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError:
            raise ProtobufParseError(f"bad field in {SECRET}")
    except ProtobufParseError as exc:
        unified_log.report_failure("read_note_content", exc=exc)
    [event] = _events(tmp_path)
    assert event["message"] == "read_note_content failed: ProtobufParseError"
    assert "reason" not in _extra(event)


def test_explicit_cause_sets_the_code_and_keeps_both_texts_locally(tmp_path):
    from apple_notes.exceptions import PartialWriteError

    unified_log.configure(component="daemon", redact=False)
    try:
        try:
            raise ClipboardError("Pasteboard read timed out after 90s")
        except ClipboardError as inner:
            raise PartialWriteError("write landed but not confirmed") from inner
    except PartialWriteError as exc:
        unified_log.report_failure("update_note_content", exc=exc)
    [event] = _events(tmp_path)
    extra = _extra(event)
    assert extra["reason"] == "pasteboard_read_timeout"
    assert extra["error_text"] == "write landed but not confirmed"
    assert extra["cause_text"] == "Pasteboard read timed out after 90s"


def test_cause_text_is_never_logged_in_remote_mode(tmp_path):
    from apple_notes.exceptions import PartialWriteError

    unified_log.configure(component="remote", redact=True)
    try:
        try:
            raise ClipboardError("Pasteboard read timed out after 90s")
        except ClipboardError as inner:
            raise PartialWriteError(f"note {SECRET} not confirmed") from inner
    except PartialWriteError as exc:
        unified_log.report_failure("update_note_content", exc=exc)
    [event] = _events(tmp_path)
    extra = _extra(event)
    assert extra["reason"] == "pasteboard_read_timeout"
    assert "cause_text" not in extra and "error_text" not in extra
    assert SECRET not in json.dumps(event)


def _real_sqlite_error(path: Path) -> sqlite3.Error:
    """A sqlite3 error raised by the library itself, carrying sqlite_errorname."""
    path.write_bytes(b"Diary Secret Title " * 100)  # not a database
    try:
        sqlite3.connect(path).execute("select 1 from sqlite_master")
    except sqlite3.Error as exc:
        return exc
    raise AssertionError("expected sqlite to refuse a non-database file")


def test_sqlite_errorname_is_the_fallback_code(tmp_path):
    exc = _real_sqlite_error(tmp_path / f"{SECRET}.sqlite")
    assert exc.sqlite_errorname == "SQLITE_NOTADB"
    unified_log.configure(component="remote", redact=True)
    unified_log.report_failure("list_notes", exc=exc)
    [event] = _events(tmp_path)
    error_type = type(exc).__name__
    assert event["message"] == f"list_notes failed: {error_type} (sqlite_notadb)"
    assert _extra(event)["reason"] == "sqlite_notadb"
    assert SECRET not in json.dumps(event)


def test_message_table_wins_over_sqlite_errorname(tmp_path):
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorname = "SQLITE_BUSY"
    assert unified_log.reason_code(exc) == "db_locked"


@pytest.mark.parametrize("bogus", [f"{SECRET}", "SQLITE_BUSY; drop", "x" * 300, 7])
def test_a_malformed_sqlite_errorname_is_ignored(bogus):
    exc = sqlite3.OperationalError(f"weird {SECRET}")
    exc.sqlite_errorname = bogus
    assert unified_log.reason_code(exc) is None


def test_the_real_pasteboard_timeout_maps_to_its_code(monkeypatch):
    """Pins the allowlist prefix to clipboard.py's actual raise site."""
    from apple_notes import clipboard

    monkeypatch.setattr(clipboard, "PASTEBOARD_READ_TIMEOUT", 0.05)
    manager = clipboard.ClipboardManager.__new__(clipboard.ClipboardManager)
    with pytest.raises(ClipboardError) as caught:
        manager._read_via_fresh_thread(lambda: __import__("time").sleep(0.5))
    assert unified_log.reason_code(caught.value) == "pasteboard_read_timeout"


def _unhandled(exc: BaseException) -> None:
    try:
        raise exc
    except BaseException:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")


def test_unhandled_exception_carries_the_reason_code(tmp_path):
    from apple_notes_mcp.remote.request_log import configure_remote_logging
    import io

    unified_log.configure(component="remote", redact=True)
    configure_remote_logging(io.StringIO())
    _unhandled(sqlite3.OperationalError("database is locked"))
    [event] = _events(tmp_path)
    assert event["message"] == logger_events.UNHANDLED
    assert _extra(event)["reason"] == "db_locked"


def test_unhandled_throttle_keeps_different_causes_apart(tmp_path):
    from apple_notes_mcp.remote.request_log import configure_remote_logging
    import io

    unified_log.configure(component="remote", redact=True)
    configure_remote_logging(io.StringIO())
    _unhandled(sqlite3.OperationalError("database is locked"))
    _unhandled(sqlite3.OperationalError("database is locked"))  # throttled
    _unhandled(sqlite3.OperationalError("unable to open database file"))
    reasons = [_extra(e)["reason"] for e in _events(tmp_path)]
    assert reasons == ["db_locked", "db_unable_to_open"]


def test_unhandled_exception_without_a_reason_stays_type_only(tmp_path):
    from apple_notes_mcp.remote.request_log import configure_remote_logging
    import io

    unified_log.configure(component="remote", redact=True)
    configure_remote_logging(io.StringIO())
    _unhandled(sqlite3.OperationalError(f"no such column: {SECRET}"))
    [event] = _events(tmp_path)
    assert "reason" not in _extra(event)
    assert SECRET not in json.dumps(event)
