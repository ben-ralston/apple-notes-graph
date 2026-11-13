"""Context-scoped timing profiler for Apple Notes MCP actions (issue #98).

A ``Profile`` collects named phase spans plus a total, carried implicitly via a
``contextvar`` so callers stay decoupled: ``span()`` and ``record()`` are
no-ops when no profile is active. The outer wrapper (``profile()`` /
``profiled()``) times the whole call and logs one human-readable START and END
line per action to ``timing.log``.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from . import unified_log
from .config import CONFIG_DIR

logger = logging.getLogger(__name__)

# Active profile for the current context (per-thread / per-asyncio-task).
_active_profile: contextvars.ContextVar[Optional["Profile"]] = contextvars.ContextVar(
    "notes_graph_active_profile", default=None
)


class Profile:
    """Collects per-phase durations and a total for one profiled action.

    Durations are accumulated internally in seconds and exposed as integer
    milliseconds. ``record`` sums repeated names so a phase entered twice (or a
    ``span`` plus a manual ``record``) shows one combined figure.
    """

    def __init__(self, action: str, detail: str = ""):
        self.action = action
        self.detail = detail
        self.outcome: str | None = None  # explicit override; else derived
        self.error: BaseException | None = None  # attached by attach_error()
        self._spans: dict[str, float] = {}
        self._start = time.monotonic()
        self.total_secs: float | None = None

    def record(self, name: str, secs: float) -> None:
        self._spans[name] = self._spans.get(name, 0.0) + secs

    def finalize(self) -> None:
        """Lock in the total elapsed time. Idempotent — first call wins."""
        if self.total_secs is None:
            self.total_secs = time.monotonic() - self._start

    @property
    def total_ms(self) -> int:
        secs = (
            self.total_secs
            if self.total_secs is not None
            else time.monotonic() - self._start
        )
        return round(secs * 1000)

    @property
    def timing_ms(self) -> dict[str, int]:
        return {name: round(secs * 1000) for name, secs in self._spans.items()}


@contextmanager
def span(name: str) -> Iterator[None]:
    """Time the wrapped block, recording it under ``name`` in the active profile.

    No-op (zero overhead beyond the contextvar read) when no profile is active.
    """
    prof = _active_profile.get()
    if prof is None:
        yield
        return
    start = time.monotonic()
    try:
        yield
    finally:
        prof.record(name, time.monotonic() - start)


def record(name: str, secs: float) -> None:
    """Add ``secs`` to ``name`` in the active profile; no-op when inactive."""
    prof = _active_profile.get()
    if prof is not None:
        prof.record(name, secs)


def attach_error(exc: BaseException) -> None:
    """Attach the real exception to the active profile for the unified-log
    hook (#147). Used by callers that convert exceptions to error responses
    instead of raising (the UI helper daemon); no-op when no profile is
    active."""
    prof = _active_profile.get()
    if prof is not None:
        prof.error = exc


# Human-readable, greppable timing log in the per-user app-support dir (same
# place as the daemon socket and #96 edit lock). Read at call time so tests can
# redirect it.
TIMING_LOG_PATH = CONFIG_DIR / "timing.log"


def outcome_for_error_type(type_name: str) -> str:
    """Map an exception/error type name to an END-line outcome token."""
    if type_name == "EditLockTimeoutError":
        return "timeout"
    return f"error:{type_name}"


def _timestamp() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 100000}"


def _write_line(text: str) -> None:
    """Append one line to the timing log. Best-effort: never raises."""
    path = TIMING_LOG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError as e:  # observability must never break a real operation
        logger.debug("timing log write failed: %s", e)


def _log_start(prof: "Profile") -> None:
    parts = [_timestamp(), prof.action]
    if prof.detail:
        parts.append(prof.detail)
    parts.append("START")
    _write_line("  ".join(parts))


def _log_end(prof: "Profile", outcome: str) -> None:
    parts = [_timestamp(), prof.action]
    if prof.detail:
        parts.append(prof.detail)
    parts.append(f"total={prof.total_ms}ms")
    spans = prof.timing_ms
    if spans:
        parts.append("[" + " ".join(f"{k}={v}" for k, v in spans.items()) + "]")
    parts.append(outcome)
    _write_line("  ".join(parts))


@contextmanager
def profile(action: str, detail: str = "") -> Iterator["Profile"]:
    """Time a whole action: set the active profile, log START then END.

    On exit the END line carries total elapsed, the per-phase span breakdown,
    and an outcome (``ok`` / ``error:<Type>`` / ``timeout``). A caller may set
    ``prof.outcome`` to override the derived outcome (used by the daemon, which
    converts exceptions to error responses rather than propagating them).
    """
    prof = Profile(action, detail)
    token = _active_profile.set(prof)
    # Outer try guarantees the contextvar token is reset no matter what the
    # logging or the wrapped body does — a leaked token would corrupt the
    # active-profile state for everything that follows in this context.
    try:
        _log_start(prof)
        derived = "ok"
        caught: BaseException | None = None
        try:
            yield prof
        except BaseException as exc:
            derived = outcome_for_error_type(type(exc).__name__)
            caught = exc
            raise
        finally:
            prof.finalize()
            outcome = prof.outcome if prof.outcome is not None else derived
            _log_end(prof, outcome)
            if outcome != "ok":
                # Best-effort bridge to the unified logger (#147): a hook
                # failure must never mask the operation's real outcome.
                try:
                    unified_log.report_failure(
                        action=prof.action,
                        detail=prof.detail,
                        exc=caught if caught is not None else prof.error,
                        elapsed_ms=prof.total_ms,
                    )
                except Exception:
                    logger.debug("unified-log hook failed", exc_info=True)
    finally:
        _active_profile.reset(token)


def _render_detail(fn, args, kwargs, detail_arg: str | None) -> str:
    if not detail_arg:
        return ""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        value = bound.arguments.get(detail_arg)
    except TypeError:
        value = kwargs.get(detail_arg)
    if value is None or value == "":
        return ""
    return f'{detail_arg}="{value}"'


def profiled(
    action: str, *, detail_arg: str | None = None, attach_response: bool = False
):
    """Decorator: profile a tool call end-to-end.

    Wraps ``fn`` in ``profile(action, detail)``. ``detail_arg`` names the call
    argument rendered into the log detail (e.g. ``name="My Note"``). When
    ``attach_response`` is set and ``fn`` returns a dict (write tools), the dict
    gains ``elapsed_ms`` + ``timing``; read tools (which return varied shapes)
    pass ``attach_response=False`` and are logged only.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            detail = _render_detail(fn, args, kwargs, detail_arg)
            with profile(action, detail) as prof:
                result = fn(*args, **kwargs)
                if attach_response and isinstance(result, dict):
                    prof.finalize()  # lock total so response == logged END line
                    result = {
                        **result,
                        "elapsed_ms": prof.total_ms,
                        "timing": prof.timing_ms,
                    }
                return result

        return wrapper

    return decorator
