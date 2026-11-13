"""Remote mode reports to the unified logger (issue #170).

Stable literal messages, metadata only: never note titles or contents,
tokens, codes or the client secret. Events land in a temp queue file (the
token file is missing, so the vendored client never touches the network).
"""

import io
import json
import logging
import urllib.parse

import httpx
import pytest
from starlette.testclient import TestClient

from apple_notes import config as notes_config
from apple_notes import folder_scope, unified_log
from apple_notes.database import NotesDatabase
from apple_notes.exceptions import NoteNotFoundError, ProtobufParseError
from apple_notes_mcp.remote import cli, logger_events
from apple_notes_mcp.remote.config import RemoteConfig, RemoteConfigError

SECRET = "Secret Title"


class ToolError(Exception):
    """Stand-in matching FastMCP's ToolError by name (classification is by name)."""


@pytest.fixture(autouse=True)
def isolated_logger(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGGER_QUEUE_FILE", str(tmp_path / "queue.jsonl"))
    monkeypatch.setenv("LOGGER_TOKEN_FILE", str(tmp_path / "missing-token"))
    monkeypatch.delenv("NOTES_GRAPH_UNIFIED_LOG", raising=False)
    monkeypatch.setenv("LOGGER_URL", "http://127.0.0.1:9")  # never contacted: no token
    monkeypatch.setattr(logger_events, "STAMP_FILE", tmp_path / "stamps.json")
    # Synchronous local append: no delivery threads to join.
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


# --- redaction in remote mode ---------------------------------------------


def test_redact_mode_drops_detail_and_exception_text(tmp_path):
    unified_log.configure(component="remote", redact=True)
    unified_log.report_failure(
        "read_note_content",
        f'name="{SECRET}"',
        ProtobufParseError(f"bad note {SECRET}"),
    )
    [event] = _events(tmp_path)
    assert event["message"] == "read_note_content failed: ProtobufParseError"
    assert "detail" not in _extra(event)
    assert _extra(event)["component"] == "remote"
    assert SECRET not in json.dumps(event)


def test_redact_mode_parse_error_carries_no_exception_text(tmp_path):
    unified_log.configure(redact=True)
    unified_log.log_parse_error("search_notes", 42, ProtobufParseError(SECRET))
    [event] = _events(tmp_path)
    assert SECRET not in json.dumps(event)
    assert _extra(event)["note"] == "42"


def test_redact_mode_off_keeps_stdio_behaviour(tmp_path):
    unified_log.report_failure("op", "d", ProtobufParseError("boom"))
    [event] = _events(tmp_path)
    assert event["message"] == "op failed: ProtobufParseError: boom"


# --- config / startup failure ---------------------------------------------


def test_serve_config_failure_is_reported_once_per_window(tmp_path, monkeypatch):
    def bad(path, **kw):
        raise RemoteConfigError("cannot read /x: s3cret-value")

    monkeypatch.setattr(cli, "load_remote_config", bad)
    monkeypatch.setattr(
        "apple_notes_mcp.remote.request_log.configure_remote_logging", lambda *a: None
    )
    assert cli.main(["serve"]) == 2
    assert cli.main(["serve"]) == 2  # launchd restart 10 s later: throttled
    [event] = _events(tmp_path)
    assert event["level"] == "error"
    assert event["message"] == logger_events.CONFIG_FAILED
    assert _extra(event)["error_type"] == "RemoteConfigError"
    assert _extra(event)["component"] == "remote"
    assert "s3cret" not in json.dumps(event)


def test_config_failure_reports_again_after_the_window(tmp_path):
    clock = iter([1000.0, 1000.0 + logger_events.RESTART_WINDOW_S + 1])
    for _ in range(2):
        logger_events.report_config_failure(
            RemoteConfigError("x"), clock=lambda: next(clock)
        )
    assert len(_events(tmp_path)) == 2


def test_unexpected_config_load_error_is_reported_too(tmp_path, monkeypatch):
    def bad(path, **kw):
        raise TypeError("expected str, got int")  # e.g. "state_dir": 123

    monkeypatch.setattr(cli, "load_remote_config", bad)
    monkeypatch.setattr(
        "apple_notes_mcp.remote.request_log.configure_remote_logging", lambda *a: None
    )
    with pytest.raises(TypeError):
        cli.main(["serve"])
    [event] = _events(tmp_path)
    assert event["message"] == logger_events.CONFIG_FAILED
    assert _extra(event)["error_type"] == "TypeError"


def test_kill_switch_does_not_burn_the_restart_window(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_GRAPH_UNIFIED_LOG", "0")
    logger_events.report_config_failure(RemoteConfigError("x"))
    assert not logger_events.STAMP_FILE.exists()
    monkeypatch.delenv("NOTES_GRAPH_UNIFIED_LOG")
    logger_events.report_config_failure(RemoteConfigError("x"))
    assert len(_events(tmp_path)) == 1


def test_writes_cli_reports_in_redacted_remote_mode(tmp_path, monkeypatch, capsys):
    import dataclasses

    from apple_notes_mcp.remote.write_queue import WriteQueueConfig

    cfg = RemoteConfig(
        public_url="https://n.example.com",
        allowed_github_login="owner",
        allowed_github_id=1,
        github_client_id="x",
    )
    cfg = dataclasses.replace(
        cfg, write_queue=WriteQueueConfig(state_dir=tmp_path / "wq")
    )
    monkeypatch.setattr(cli, "load_remote_config", lambda path, **kw: cfg)
    assert cli.main(["writes", "list"]) == 0
    assert unified_log._component == "remote"
    assert unified_log._redact is True


def test_check_config_failures_are_not_reported(tmp_path, monkeypatch):
    def bad(path, **kw):
        raise RemoteConfigError("nope")

    monkeypatch.setattr(cli, "load_remote_config", bad)
    assert cli.main(["check-config"]) == 2
    assert _events(tmp_path) == []


def _serve_with(monkeypatch, *, build=None, run=None):
    import uvicorn

    from apple_notes_mcp.remote import app as remote_app
    from apple_notes_mcp.remote import request_log

    cfg = RemoteConfig(
        public_url="https://n.example.com",
        allowed_github_login="owner",
        allowed_github_id=1,
        github_client_id="x",
    )
    monkeypatch.setattr(cli, "load_remote_config", lambda path: cfg)
    monkeypatch.setattr(remote_app, "build_remote_app", build or (lambda c: "APP"))
    monkeypatch.setattr(request_log, "configure_remote_logging", lambda *a: None)
    monkeypatch.setattr(uvicorn, "run", run or (lambda app, **kw: None))


def test_build_failure_is_reported_and_reraised(tmp_path, monkeypatch):
    def build(cfg):
        raise RuntimeError(f"cannot open {SECRET}")

    _serve_with(monkeypatch, build=build)
    with pytest.raises(RuntimeError):
        cli.main(["serve"])
    [event] = _events(tmp_path)
    assert event["message"] == logger_events.SERVER_FAILED
    assert _extra(event)["error_type"] == "RuntimeError"
    assert _extra(event)["phase"] == "build"
    assert SECRET not in json.dumps(event)


def test_uvicorn_bind_failure_exit_is_reported(tmp_path, monkeypatch):
    def run(app, **kw):
        raise SystemExit(1)  # uvicorn's reaction to "address already in use"

    _serve_with(monkeypatch, run=run)
    with pytest.raises(SystemExit):
        cli.main(["serve"])
    [event] = _events(tmp_path)
    assert event["message"] == logger_events.SERVER_FAILED
    assert _extra(event)["phase"] == "serve"
    assert _extra(event)["error_type"] == "SystemExit"


@pytest.mark.parametrize("exc", [SystemExit(0), KeyboardInterrupt()])
def test_clean_exits_are_not_reported(tmp_path, monkeypatch, exc):
    def run(app, **kw):
        raise exc

    _serve_with(monkeypatch, run=run)
    with pytest.raises(type(exc)):
        cli.main(["serve"])
    assert _events(tmp_path) == []


def test_serve_switches_unified_log_to_redacted_remote_mode(monkeypatch):
    _serve_with(monkeypatch)
    assert cli.main(["serve"]) == 0
    assert unified_log._component == "remote"
    assert unified_log._redact is True


# --- unhandled exceptions via the root logging handler ---------------------


def _remote_logging():
    from apple_notes_mcp.remote.request_log import configure_remote_logging

    stream = io.StringIO()
    configure_remote_logging(stream)
    return stream


def test_unhandled_exception_is_reported_with_type_only(tmp_path):
    stream = _remote_logging()
    try:
        raise ProtobufParseError(f"note {SECRET}")
    except ProtobufParseError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    [event] = _events(tmp_path)
    assert event["message"] == logger_events.UNHANDLED
    assert _extra(event) == {
        "logger": "uvicorn.error",
        "level": "ERROR",
        "error_type": "ProtobufParseError",
        "component": "test",
    }
    assert SECRET not in json.dumps(event)
    # server.log redaction still applies after our handler saw the record
    assert SECRET not in stream.getvalue() and "ProtobufParseError" in stream.getvalue()


def test_unhandled_user_errors_and_already_reported_faults_are_skipped(tmp_path):
    _remote_logging()
    log = logging.getLogger("mcp.server.lowlevel.server")
    try:
        raise NoteNotFoundError(SECRET)
    except NoteNotFoundError:
        log.exception("tool failed")
    exc = ProtobufParseError("x")
    unified_log.mark_reported(exc)  # the profiler hook already emitted it
    try:
        raise ToolError("Error executing tool") from exc  # FastMCP's wrapper
    except ToolError:
        log.exception("tool failed")
    log.error("no exception attached")
    try:
        raise ValueError("w")
    except ValueError:
        log.warning("only a warning", exc_info=True)
    assert _events(tmp_path) == []


def test_fault_reported_on_a_profiled_tools_toolerror_is_not_reported_again(tmp_path):
    # A profiled tool raises ToolError from the fault; the profiler hook marks
    # that wrapper. FastMCP then wraps it in another ToolError and logs it.
    _remote_logging()
    try:
        try:
            try:
                raise ProtobufParseError("x")
            except ProtobufParseError as e:
                raise ToolError("Error reading note") from e
        except ToolError as tool_exc:
            unified_log.mark_reported(tool_exc)
            raise ToolError("Error executing tool read_note_content") from tool_exc
    except ToolError:
        logging.getLogger("mcp.server.lowlevel.server").exception("tool failed")
    assert _events(tmp_path) == []


def test_new_fault_raised_while_handling_a_reported_one_is_reported(tmp_path):
    _remote_logging()
    reported = ProtobufParseError("x")
    unified_log.mark_reported(reported)
    try:
        try:
            raise reported
        except ProtobufParseError:
            raise OSError("disk full")  # a separate fault during cleanup
    except OSError:
        logging.getLogger("apple_notes_mcp.server").exception("cleanup failed")
    [event] = _events(tmp_path)
    assert _extra(event)["error_type"] == "OSError"


def test_repeated_unhandled_exception_is_throttled(tmp_path):
    _remote_logging()
    for _ in range(5):
        try:
            raise ValueError("same")
        except ValueError:
            logging.getLogger("asyncio").exception("Task exception was never retrieved")
    assert len(_events(tmp_path)) == 1


# --- repeated GitHub token-exchange failures -------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_exchange_monitor_reports_once_per_failure_streak(tmp_path):
    clock = _Clock()
    monitor = logger_events.TokenExchangeMonitor(threshold=3, clock=clock)
    for _ in range(2):
        monitor.failure(httpx.ConnectError("down"))
    assert _events(tmp_path) == []
    monitor.failure(httpx.ConnectError("down"))
    monitor.failure(httpx.ConnectError("down"))
    [event] = _events(tmp_path)
    assert event["message"] == logger_events.EXCHANGE_FAILING
    assert _extra(event)["failures"] == 3
    assert _extra(event)["error_type"] == "ConnectError"
    # A success then a fresh streak inside the hour: still one event (a
    # signed-in stranger resetting the streak can't file issue after issue).
    monitor.success()
    for _ in range(3):
        monitor.failure(httpx.ConnectError("down"))
    assert len(_events(tmp_path)) == 1
    clock.now += logger_events.EXCHANGE_WINDOW_S + 1
    monitor.success()
    for _ in range(3):
        monitor.failure(httpx.ConnectError("down"))
    assert len(_events(tmp_path)) == 2


def test_exchange_monitor_reports_a_continuing_outage_after_the_window(tmp_path):
    clock = _Clock()
    monitor = logger_events.TokenExchangeMonitor(threshold=3, clock=clock)
    for _ in range(3):
        monitor.failure(httpx.ConnectError("down"))
    monitor.success()
    clock.now += 60
    for _ in range(3):  # streak hits the threshold inside the window: skipped
        monitor.failure(httpx.ConnectError("down"))
    assert len(_events(tmp_path)) == 1
    clock.now += logger_events.EXCHANGE_WINDOW_S
    monitor.failure(httpx.ConnectError("down"))  # still failing, no success
    events = _events(tmp_path)
    assert len(events) == 2
    assert _extra(events[1])["failures"] == 4


def test_exchange_monitor_ignores_bad_verification_codes(tmp_path):
    from apple_notes_mcp.remote.auth import GitHubTokenError

    monitor = logger_events.TokenExchangeMonitor(threshold=3)
    for _ in range(5):  # tampered / replayed codes: client input, not a fault
        monitor.failure(GitHubTokenError("bad_verification_code"))
    assert _events(tmp_path) == []


def test_exchange_monitor_records_github_error_code_and_status(tmp_path):
    from apple_notes_mcp.remote.auth import GitHubTokenError

    clock = _Clock()
    monitor = logger_events.TokenExchangeMonitor(threshold=1, clock=clock)
    monitor.failure(GitHubTokenError("incorrect_client_credentials"))
    request = httpx.Request("POST", "https://github.test/x")
    response = httpx.Response(503, request=request)
    monitor.success()
    clock.now += logger_events.EXCHANGE_WINDOW_S + 1
    monitor.failure(httpx.HTTPStatusError("bad", request=request, response=response))
    first, second = (_extra(e) for e in _events(tmp_path))
    assert first["upstream_error"] == "incorrect_client_credentials"
    assert second["status"] == 503


def test_exchange_monitor_drops_unexpected_github_error_text(tmp_path):
    from apple_notes_mcp.remote.auth import GitHubTokenError

    logger_events.TokenExchangeMonitor(threshold=1).failure(
        GitHubTokenError("gho_abc123 Secret Title!")
    )
    [event] = _events(tmp_path)
    assert "upstream_error" not in _extra(event)
    assert "gho_abc123" not in json.dumps(event)


def test_github_callback_reports_repeated_upstream_failures(
    tmp_path, monkeypatch, scoped_db
):
    from test_remote_oauth import PUBLIC, _register, _start_sign_in

    from apple_notes_mcp.remote.app import build_remote_app

    monkeypatch.setattr(NotesDatabase, "DEFAULT_PATH", scoped_db)
    monkeypatch.setattr(notes_config, "CONFIG_PATH", tmp_path / "no-config.json")
    cfg = RemoteConfig(
        public_url=PUBLIC,
        allowed_github_login="octo-owner",
        allowed_github_id=1001,
        github_client_id="gh-client-id",
        github_client_secret="gh-client-secret",
        state_dir=tmp_path / "state",
        log_path=tmp_path / "logs" / "requests.log",
        github_authorize_url="https://github.test/login/oauth/authorize",
        github_token_url="https://github.test/login/oauth/access_token",
        github_user_url="https://api.github.test/user",
    )

    def handler(request):
        return httpx.Response(200, json={"error": "incorrect_client_credentials"})

    app = build_remote_app(
        cfg,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    with TestClient(app, base_url=PUBLIC) as c:
        client_id = _register(c)
        for _ in range(logger_events.EXCHANGE_FAILURE_THRESHOLD):
            gh_state, _ = _start_sign_in(c, client_id)
            r = c.get(
                "/oauth/github/callback",
                params={"code": "owner-code", "state": gh_state},
                follow_redirects=False,
            )
            assert r.status_code == 502
    folder_scope.reset()
    [event] = _events(tmp_path)
    assert event["message"] == logger_events.EXCHANGE_FAILING
    assert _extra(event)["upstream_error"] == "incorrect_client_credentials"
    dumped = json.dumps(event)
    assert "gh-client-secret" not in dumped and "owner-code" not in dumped
    assert urllib.parse.quote("owner-code") not in dumped
