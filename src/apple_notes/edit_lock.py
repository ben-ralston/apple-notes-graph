"""Cross-process serialization lock for Apple Notes UI-scripting edits.

Apple Notes UI scripting (CGEvent keystrokes + clipboard paste) is not
re-entrant: if two edits overlap, the second steals focus mid-keystroke and
both corrupt silently — or the server hangs with no error (issue #96). This
module provides one file-based lock that serializes every UI-scripting edit
across coroutines, threads, AND processes:

* Same-session parallelism — a single MCP server issuing parallel tool calls.
  A plain ``threading.Lock`` wouldn't help if the MCP framework runs handlers
  in separate threads or an event loop; a file lock spans both.
* Cross-session / multi-process — a second MCP server process (another Claude
  Code tab, Claude Desktop) on the same Mac. ``flock`` is OS-enforced, so the
  second process blocks until the first releases.

The lock is advisory and tied to an open file description, so a crashed holder
is released automatically by the OS when its process exits — it can never wedge
forever. As a second safety net, acquisition times out after ``timeout``
seconds and raises ``EditLockTimeoutError`` instead of blocking indefinitely.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import profiling
from .config import CONFIG_DIR
from .exceptions import EditLockTimeoutError

# Well-known path shared by every MCP server process and the UI helper daemon
# on this machine. Lives in the per-user app-support dir (where the daemon
# socket already lives) rather than world-writable /tmp, so another user can't
# pre-create the path and block or hijack the lock.
LOCK_PATH = CONFIG_DIR / "ui-edit.lock"

# Default acquisition timeout. A real edit takes a few seconds; 30s leaves
# generous headroom for a slow paste while still failing loudly if a holder is
# stuck rather than hanging the caller forever.
EDIT_LOCK_TIMEOUT = 30.0

# Seconds between non-blocking acquisition attempts. flock has no native
# timeout, and signal-based alarms only fire on the main thread (the daemon
# runs edits in worker threads via asyncio.to_thread), so we poll instead.
_POLL_INTERVAL = 0.1


@contextmanager
def edit_lock(
    timeout: float = EDIT_LOCK_TIMEOUT,
    lock_path: Path | None = None,
) -> Iterator[None]:
    """Hold an exclusive cross-process lock for one UI-scripting edit.

    Blocks until the lock is acquired or ``timeout`` seconds elapse, then
    raises ``EditLockTimeoutError``. Released on exit, including if the body
    raises. ``lock_path`` defaults to the module-level ``LOCK_PATH`` (read at
    call time so tests can redirect it).
    """
    path = Path(lock_path) if lock_path is not None else LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _acquire(fd, timeout, path)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _acquire(fd: int, timeout: float, path: Path) -> None:
    """Poll for the exclusive lock until acquired or the timeout expires."""
    deadline = time.monotonic() + timeout
    wait_start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                # Held by another open file description (this or another process).
                if time.monotonic() >= deadline:
                    raise EditLockTimeoutError(
                        f"Could not acquire the Apple Notes edit lock within "
                        f"{timeout:.0f}s — another note edit is taking too long or "
                        f"is stuck. Retry shortly. Lock file: {path}."
                    ) from None
                time.sleep(_POLL_INTERVAL)
    finally:
        # Feed the wait into the active profile (no-op when none active, #98).
        # The caller's `execute` span wraps the dispatch that holds this lock, so
        # `lock_wait` is a nested sub-interval of `execute` (not additive): on a
        # contended write you'll see e.g. lock_wait≈execute when it was mostly
        # waiting, lock_wait≪execute when it was mostly real scripting work.
        profiling.record("lock_wait", time.monotonic() - wait_start)
