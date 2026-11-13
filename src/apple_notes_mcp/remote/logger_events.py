"""Unified-logger events for remote mode (issue #170).

The remote server runs unattended as a LaunchAgent, so its faults must reach
the unified logger (the sentinel files error rows as GitHub issues). Every
event here has a stable literal message and metadata only - exception TYPES,
logger names, HTTP status codes, GitHub's fixed error codes - never note
titles or contents, tokens, codes or the client secret. Nothing here raises.

- ``report_config_failure`` / ``report_server_failure``: the ``serve`` process
  is about to exit. Delivered synchronously, and throttled across launchd
  restarts by a small stamp file.
- ``UnhandledExceptionHandler``: a root logging handler that turns ERROR
  records carrying a real fault into one event (same redaction as server.log).
- ``TokenExchangeMonitor``: one event for a streak of failed GitHub
  code-for-token exchanges, at most once an hour.
"""

import json
import logging
import os
import re
import time
from typing import Callable

import httpx

from apple_notes import unified_log
from apple_notes.config import CONFIG_DIR

CONFIG_FAILED = "remote config or secret failed to load"
SERVER_FAILED = "remote server exited with an error"
UNHANDLED = "remote server: unhandled exception"
EXCHANGE_FAILING = "GitHub token exchange failing repeatedly"

# launchd restarts a failed server every 10 s (KeepAlive + ThrottleInterval):
# without a stamp that survives the restart, one bad config would emit an
# event every 10 s until fixed.
RESTART_WINDOW_S = 3600
STAMP_FILE = CONFIG_DIR / "remote-logger-stamps.json"

# The same (logger, exception type) inside one process reports once per window.
UNHANDLED_WINDOW_S = 600
_MAX_UNHANDLED_KEYS = 256

EXCHANGE_FAILURE_THRESHOLD = 3
EXCHANGE_WINDOW_S = 3600
# GitHub's token endpoint answers failures with a fixed snake_case code
# (incorrect_client_credentials, bad_verification_code, ...); anything else
# is dropped rather than forwarded.
_GITHUB_ERROR_CODE = re.compile(r"[a-z_]{1,64}")


def _due(key: str, clock: Callable[[], float]) -> bool:
    """True (and stamp ``key``) unless it was reported within the window."""
    now = clock()
    try:
        stamps = json.loads(STAMP_FILE.read_text())
        if not isinstance(stamps, dict):
            stamps = {}
    except (OSError, ValueError):
        stamps = {}
    last = stamps.get(key)
    if isinstance(last, (int, float)) and 0 <= now - last < RESTART_WINDOW_S:
        return False
    stamps[key] = now
    try:
        STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STAMP_FILE.with_name(STAMP_FILE.name + ".tmp")
        tmp.write_text(json.dumps(stamps))
        os.replace(tmp, STAMP_FILE)
    except OSError:
        pass  # an unwritable stamp costs throttling, not the event
    return True


def _report_exit(key: str, message: str, extra: dict, clock) -> None:
    try:
        # Kill switch first: a suppressed event must not use up the window.
        if unified_log._enabled() and _due(key, clock):
            unified_log.log_error(message, extra, sync=True)
    except Exception:
        pass


def report_config_failure(
    exc: BaseException, clock: Callable[[], float] = time.time
) -> None:
    """remote.json or the client secret failed to load at ``serve`` (a
    RemoteConfigError, or anything else the loader raised on a malformed
    file). The error text can name paths or config values, so only its type
    goes out."""
    _report_exit("config", CONFIG_FAILED, {"error_type": type(exc).__name__}, clock)


def report_server_failure(
    phase: str, exc: BaseException, clock: Callable[[], float] = time.time
) -> None:
    """Building or running the server raised (``phase`` = build | serve).
    uvicorn answers a bind failure with ``SystemExit(1)``; a clean
    ``SystemExit(0)`` or Ctrl-C is not a fault."""
    if isinstance(exc, KeyboardInterrupt):
        return
    if isinstance(exc, SystemExit) and exc.code in (0, None):
        return
    _report_exit(
        f"server:{phase}",
        SERVER_FAILED,
        {"phase": phase, "error_type": type(exc).__name__},
        clock,
    )


class UnhandledExceptionHandler(logging.Handler):
    """ERROR records with a real fault attached become one event: logger
    name, level and exception type only.

    Must sit BEFORE the redacting stderr handler on the root logger - that
    handler's filter strips ``exc_info`` from the shared record. User-input
    errors and faults the profiler hook already reported (the marker sits on
    the root error behind FastMCP's ToolError wrapper) are skipped; a NEW
    fault raised while handling a reported one is still reported.
    """

    def __init__(
        self,
        window_s: float = UNHANDLED_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(level=logging.ERROR)
        self._window = window_s
        self._clock = clock
        self._last: dict[tuple[str, str, str | None], float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc = record.exc_info[1] if record.exc_info else None
            if exc is None or not unified_log.should_report(exc):
                return
            root = unified_log._root_error(exc)
            # The profiler hook marks whatever the tool raised - often a
            # ToolError wrapper - so check every node from the top down to
            # the root error, but nothing deeper in its context.
            for node in unified_log._chain(exc):
                if getattr(node, unified_log._LOGGED_MARKER, False):
                    return
                if node is root:
                    break
            error_type = type(root).__name__
            reason = unified_log.reason_code(exc)  # allowlisted constant (#255)
            # The reason is part of the key: two causes of one type are two
            # faults, and the second must not be throttled away.
            key = (record.name, error_type, reason)
            now = self._clock()
            last = self._last.get(key)
            if last is not None and now - last < self._window:
                return
            if len(self._last) >= _MAX_UNHANDLED_KEYS:
                self._last.clear()
            self._last[key] = now
            unified_log.mark_reported(root)
            extra = {
                "logger": record.name,
                "level": record.levelname,
                "error_type": error_type,
            }
            if reason is not None:
                extra["reason"] = reason
            unified_log.log_error(UNHANDLED, extra)
        except Exception:
            pass


class TokenExchangeMonitor:
    """Counts consecutive failed GitHub exchanges; reports when a streak
    reaches ``threshold``, at most once per ``EXCHANGE_WINDOW_S``. One failure
    is routine (a slow GitHub, an abandoned sign-in); a streak usually means a
    rotated or wrong client secret, or GitHub being down.

    ``bad_verification_code`` (a tampered, replayed or expired code) is client
    input, not a fault, and never counts. The window stops anyone who can
    reach the callback from filing issue after issue by resetting the streak
    with a sign-in of their own.
    """

    def __init__(
        self,
        threshold: int = EXCHANGE_FAILURE_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.threshold = threshold
        self._clock = clock
        self._streak = 0
        self._last_report: float | None = None

    def success(self) -> None:
        self._streak = 0

    def failure(self, exc: BaseException) -> None:
        try:
            if getattr(exc, "code", None) == "bad_verification_code":
                return
            self._streak += 1
            if self._streak < self.threshold:
                return
            # >= so an outage still failing when the window ends reports again.
            now = self._clock()
            if (
                self._last_report is not None
                and now - self._last_report < EXCHANGE_WINDOW_S
            ):
                return
            self._last_report = now
            extra: dict = {"failures": self._streak, "error_type": type(exc).__name__}
            if isinstance(exc, httpx.HTTPStatusError):
                extra["status"] = exc.response.status_code
            code = getattr(exc, "code", None)
            if isinstance(code, str) and _GITHUB_ERROR_CODE.fullmatch(code):
                extra["upstream_error"] = code
            unified_log.log_error(EXCHANGE_FAILING, extra)
        except Exception:
            pass
