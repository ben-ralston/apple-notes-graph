"""unified_log wrapper: classification, emission modes, never-raise (#147)."""

import json

import pytest

from apple_notes import unified_log
from apple_notes.exceptions import (
    EditLockTimeoutError,
    EncryptedNoteError,
    NoteNotFoundError,
    ProtobufParseError,
    WriteVerificationError,
)


class ToolError(Exception):
    """Stand-in matching fastmcp's ToolError by name (classification is by name)."""


@pytest.fixture(autouse=True)
def isolated_logger_env(tmp_path, monkeypatch):
    """No real network, no real queue: token file missing -> the vendored client
    appends to the queue and returns False without any HTTP request."""
    monkeypatch.setenv("LOGGER_QUEUE_FILE", str(tmp_path / "queue.jsonl"))
    monkeypatch.setenv("LOGGER_TOKEN_FILE", str(tmp_path / "missing-token"))
    monkeypatch.delenv("NOTES_GRAPH_UNIFIED_LOG", raising=False)
    monkeypatch.setenv("LOGGER_URL", "http://127.0.0.1:9")  # never contacted: no token
    unified_log.configure(component="test", enqueue_only=False)
    unified_log._seen_parse_failures.clear()
    yield
    unified_log.configure(component="mcp", enqueue_only=False)


def _queue_events(tmp_path):
    queue = tmp_path / "queue.jsonl"
    if not queue.exists():
        return []
    return [json.loads(line) for line in queue.read_text().splitlines() if line]


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        NoteNotFoundError("no note"),
        EncryptedNoteError("locked"),
        ToolError("limit must be a positive integer"),
        KeyboardInterrupt(),
    ],
)
def test_should_report_skips_user_errors_and_signals(exc):
    assert unified_log.should_report(exc) is False


def test_should_report_skips_toolerror_wrapping_user_error():
    try:
        try:
            raise NoteNotFoundError("no note")
        except NoteNotFoundError:
            raise ToolError("Note not found")
    except ToolError as exc:
        assert unified_log.should_report(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        ProtobufParseError("bad varint"),
        WriteVerificationError("content unchanged"),
        EditLockTimeoutError("lock timeout"),
        OSError("disk full"),
        RuntimeError("unexpected"),
    ],
)
def test_should_report_logs_system_faults(exc):
    assert unified_log.should_report(exc) is True


def test_should_report_logs_toolerror_wrapping_system_fault():
    try:
        try:
            raise ProtobufParseError("bad varint")
        except ProtobufParseError:
            raise ToolError("Failed to parse note content")
    except ToolError as exc:
        assert unified_log.should_report(exc) is True


# --- report_failure -------------------------------------------------------


def test_report_failure_delivers_event(tmp_path):
    thread = unified_log.report_failure(
        "update_note_content",
        detail='name="My Note"',
        exc=WriteVerificationError("content unchanged"),
        elapsed_ms=1234,
    )
    assert thread is not None
    thread.join(timeout=5)
    events = _queue_events(tmp_path)
    assert len(events) == 1
    event = events[0]
    assert event["app"] == "notes-graph"
    assert event["level"] == "error"
    assert "update_note_content failed: WriteVerificationError" in event["message"]
    assert event["json_extra"]["action"] == "update_note_content"
    assert event["json_extra"]["error_type"] == "WriteVerificationError"
    assert event["json_extra"]["elapsed_ms"] == 1234
    assert event["json_extra"]["component"] == "test"


def test_report_failure_skips_user_error(tmp_path):
    assert unified_log.report_failure("read_note", exc=NoteNotFoundError("x")) is None
    assert _queue_events(tmp_path) == []


def test_report_failure_marks_exception_and_skips_duplicate(tmp_path):
    exc = WriteVerificationError("content unchanged")
    first = unified_log.report_failure("append", exc=exc)
    first.join(timeout=5)
    assert unified_log.report_failure("append", exc=exc) is None
    assert len(_queue_events(tmp_path)) == 1


def test_report_failure_none_exc_is_noop(tmp_path):
    assert unified_log.report_failure("action", exc=None) is None
    assert _queue_events(tmp_path) == []


# --- enqueue-only mode (short-lived scripts) ------------------------------


def test_enqueue_only_mode_appends_synchronously(tmp_path):
    unified_log.configure(enqueue_only=True)
    result = unified_log.report_failure(
        "add_link", exc=WriteVerificationError("content unchanged")
    )
    assert result is None  # no thread: synchronous local append
    events = _queue_events(tmp_path)
    assert len(events) == 1
    assert events[0]["level"] == "error"


# --- log_parse_error ------------------------------------------------------


def test_log_parse_error_dedups_per_process(tmp_path):
    exc = ProtobufParseError("bad varint")
    first = unified_log.log_parse_error("search_notes", 42, exc)
    first.join(timeout=5)
    assert unified_log.log_parse_error("search_notes", 42, exc) is None
    other_site = unified_log.log_parse_error("export_folder", 42, exc)
    assert other_site is not None
    other_site.join(timeout=5)
    events = _queue_events(tmp_path)
    assert any(e["json_extra"]["site"] == "search_notes" for e in events)
    assert len([e for e in events if e["json_extra"]["site"] == "search_notes"]) == 1


def test_log_parse_error_skips_encrypted(tmp_path):
    assert (
        unified_log.log_parse_error("search_notes", 7, EncryptedNoteError("x")) is None
    )
    assert _queue_events(tmp_path) == []


# --- log_info -------------------------------------------------------------


def test_log_info_enqueue_variant(tmp_path):
    assert unified_log.log_info("daemon stopped", enqueue=True) is None
    events = _queue_events(tmp_path)
    assert events[0]["level"] == "info"
    assert events[0]["message"] == "daemon stopped"


# --- kill switch and never-raise ------------------------------------------


def test_kill_switch_disables_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_GRAPH_UNIFIED_LOG", "0")
    assert unified_log.report_failure("a", exc=WriteVerificationError("x")) is None
    assert unified_log.log_info("hi") is None
    assert _queue_events(tmp_path) == []


def test_no_endpoint_configured_disables_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("LOGGER_URL")
    assert unified_log.report_failure("a", exc=WriteVerificationError("x")) is None
    assert unified_log.log_info("hi") is None
    assert _queue_events(tmp_path) == []


def test_wrapper_never_raises_even_when_client_is_broken(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("client exploded")

    monkeypatch.setattr(unified_log._client, "log", boom)
    monkeypatch.setattr(unified_log._client, "_build_event", boom)
    thread = unified_log.report_failure("a", exc=WriteVerificationError("x"))
    if thread is not None:
        thread.join(timeout=5)  # the thread body must swallow the error too
    unified_log.configure(enqueue_only=True)
    assert unified_log.report_failure("a", exc=OSError("y")) is None
    assert unified_log.log_info("hi") is None


# --- profiler hook (Task 3) ----------------------------------------------

from apple_notes import profiling  # noqa: E402


class TestProfilerHook:
    def test_error_outcome_reports_failure(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            unified_log,
            "report_failure",
            lambda *a, **kw: calls.append((a, kw)) or None,
        )
        with pytest.raises(ProtobufParseError):
            with profiling.profile("read_note_content", 'name="X"'):
                raise ProtobufParseError("bad varint")
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert kwargs["action"] == "read_note_content"
        assert kwargs["detail"] == 'name="X"'
        assert isinstance(kwargs["exc"], ProtobufParseError)
        assert isinstance(kwargs["elapsed_ms"], int)

    def test_ok_outcome_does_not_report(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            unified_log, "report_failure", lambda *a, **kw: calls.append(1)
        )
        with profiling.profile("list_notes"):
            pass
        assert calls == []

    def test_attach_error_covers_daemon_style_outcome(self, monkeypatch):
        """The daemon converts exceptions to error responses (nothing
        propagates through profile()); attach_error hands the hook the real
        exception and prof.outcome carries the error outcome."""
        calls = []
        monkeypatch.setattr(
            unified_log,
            "report_failure",
            lambda *a, **kw: calls.append(kw) or None,
        )
        exc = WriteVerificationError("content unchanged")
        with profiling.profile("append_note") as prof:
            profiling.attach_error(exc)
            prof.outcome = profiling.outcome_for_error_type(type(exc).__name__)
        assert len(calls) == 1
        assert calls[0]["exc"] is exc

    def test_hook_failure_never_breaks_the_operation(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("hook exploded")

        monkeypatch.setattr(unified_log, "report_failure", boom)
        with pytest.raises(ProtobufParseError):  # original error, not RuntimeError
            with profiling.profile("read_note_content"):
                raise ProtobufParseError("bad varint")


# --- daemon wiring (Task 4) ----------------------------------------------

import asyncio  # noqa: E402

from apple_notes.ui_helper_daemon import UIHelperDaemon  # noqa: E402


class TestDaemonWiring:
    def test_dispatch_error_reaches_unified_log(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            unified_log,
            "report_failure",
            lambda *a, **kw: calls.append(kw) or None,
        )
        daemon = UIHelperDaemon()

        async def failing_dispatch(command, params, request_id):
            profiling.attach_error(WriteVerificationError("content unchanged"))
            return {
                "id": request_id,
                "success": False,
                "error": {
                    "type": "WriteVerificationError",
                    "message": "content unchanged",
                },
            }

        monkeypatch.setattr(daemon, "_dispatch", failing_dispatch)
        response = asyncio.run(
            daemon._handle_command(
                {"id": "1", "command": "append_note", "params": {}}
            )
        )
        assert response["success"] is False
        assert len(calls) == 1
        assert isinstance(calls[0]["exc"], WriteVerificationError)


# --- swallowed parse-error sites (Task 5) --------------------------------

from pathlib import Path  # noqa: E402

SRC = Path(__file__).parent.parent / "src"


def test_all_parse_error_swallow_sites_report():
    """Every deliberate per-note (EncryptedNoteError, ProtobufParseError)
    swallow must report through unified_log.log_parse_error. Structural
    guard: the four known sites each name their site label."""
    expectations = {
        SRC / "apple_notes_mcp" / "server.py": [
            'log_parse_error("search_notes"',
            'log_parse_error("export_folder"',
        ],
        SRC / "apple_notes" / "librarian.py": [
            'log_parse_error("suggest_links"',
        ],
        SRC / "apple_notes" / "embeddings.py": [
            'log_parse_error("build_embeddings"',
        ],
        SRC / "apple_notes" / "table_parser.py": [
            'log_parse_error("parse_table"',
        ],
    }
    for path, needles in expectations.items():
        text = path.read_text()
        for needle in needles:
            assert needle in text, f"{path.name} missing {needle}"


# --- review-fix wave (multi-review findings, #147) ------------------------

import time  # noqa: E402

from apple_notes.exceptions import (  # noqa: E402
    ClipboardError,
    NotesNotFrontmostError,
    SessionLockedError,
)


def test_should_report_skips_expected_environment_states():
    assert unified_log.should_report(SessionLockedError("locked")) is False
    assert unified_log.should_report(NotesNotFrontmostError("focus stolen")) is False
    try:
        try:
            raise SessionLockedError("locked")
        except SessionLockedError:
            raise ToolError("session locked")
    except ToolError as exc:
        assert unified_log.should_report(exc) is False


def test_should_report_top_level_fault_not_suppressed_by_user_error_context():
    """A real fault raised while handling a user error (e.g. clipboard restore
    failing during NoteNotFoundError cleanup) must still report."""
    try:
        try:
            raise NoteNotFoundError("no note")
        except NoteNotFoundError:
            raise ClipboardError("clipboard restore failed")
    except ClipboardError as exc:
        assert unified_log.should_report(exc) is True


def test_should_report_respects_suppressed_context():
    try:
        try:
            raise ProtobufParseError("bad varint")
        except ProtobufParseError:
            raise ToolError("validation message") from None
    except ToolError as exc:
        assert unified_log.should_report(exc) is False


def test_report_failure_extracts_root_error_type_through_toolerror(tmp_path):
    try:
        try:
            raise WriteVerificationError("content unchanged")
        except WriteVerificationError:
            raise ToolError("content unchanged after paste")
    except ToolError as exc:
        thread = unified_log.report_failure("update_note_content", exc=exc)
    thread.join(timeout=5)
    event = _queue_events(tmp_path)[0]
    assert event["json_extra"]["error_type"] == "WriteVerificationError"
    assert "WriteVerificationError" in event["message"]


def test_enqueue_drops_event_when_lock_contended(tmp_path):
    import fcntl as _fcntl

    unified_log.configure(enqueue_only=True)
    lock_path = tmp_path / "queue.jsonl.lock"
    holder = open(lock_path, "w")
    _fcntl.flock(holder, _fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        assert unified_log.report_failure("a", exc=OSError("x")) is None
        assert time.monotonic() - start < 2.0  # bounded, never a long stall
        assert _queue_events(tmp_path) == []  # dropped, not blocked
    finally:
        holder.close()


def test_parse_error_emission_cap(tmp_path):
    unified_log.configure(enqueue_only=True)
    exc = ProtobufParseError("bad")
    for pk in range(unified_log._MAX_PARSE_EVENTS + 5):
        unified_log.log_parse_error("search_notes", pk, exc)
    assert len(_queue_events(tmp_path)) == unified_log._MAX_PARSE_EVENTS


def test_emit_falls_back_to_enqueue_when_thread_cap_reached(tmp_path, monkeypatch):
    import threading as _threading

    release = _threading.Event()
    monkeypatch.setattr(unified_log, "_send", lambda *a: release.wait(5))
    threads = []
    try:
        for i in range(unified_log._MAX_LIVE_THREADS):
            t = unified_log.log_info(f"event {i}")
            assert t is not None
            threads.append(t)
        assert unified_log.log_info("spillover") is None  # enqueued instead
        events = _queue_events(tmp_path)
        assert [e["message"] for e in events] == ["spillover"]
    finally:
        release.set()
        for t in threads:
            t.join(timeout=5)


def test_daemon_reconstructed_error_marked_and_not_rereported(tmp_path):
    from apple_notes.ui_helper_client import _raise_error_response

    with pytest.raises(WriteVerificationError) as excinfo:
        _raise_error_response(
            {"type": "WriteVerificationError", "message": "content unchanged"}
        )
    assert (
        unified_log.report_failure("update_note_content", exc=excinfo.value) is None
    )
    assert _queue_events(tmp_path) == []


def test_error_response_attaches_exception_to_active_profile():
    from apple_notes.ui_helper_daemon import _error_response

    exc = WriteVerificationError("x")
    with profiling.profile("append_note") as prof:
        _error_response("1", exc)
        assert prof.error is exc


def test_table_parser_swallow_reports_canary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        unified_log, "log_parse_error", lambda *a, **kw: calls.append(a) or None
    )
    from apple_notes import table_parser

    assert table_parser.parse_table_crdt(b"not gzip") is None
    assert calls and calls[0][0] == "parse_table"


def test_should_report_skips_an_open_notes_dialog():
    """A dialog left open in Notes is the user's screen state, not a bug
    (issue #199): it must not file sentinel issues."""
    from apple_notes.exceptions import NotesModalOpenError

    assert unified_log.should_report(NotesModalOpenError("dialog open")) is False
