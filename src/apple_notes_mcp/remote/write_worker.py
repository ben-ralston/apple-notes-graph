"""Drain loop for queued remote writes (issue #169, the owner's decisions §9).

Runs as a daemon thread inside the remote server (started by ``cli._serve``).
A queued write applies when the GUI session is unlocked and either

- it was accepted during the current unlocked stretch (apply now - the same as
  a local Claude Code write today), or
- the session has been unlocked for ``delay`` seconds (60 by default: a write
  accepted while the Mac was locked waits a minute after the owner opens it).

There is no idle gate: the write-guard overlay + lead countdown in the stdio
pipeline is the at-the-Mac protection. The queue drains strictly FIFO, so a
newer write never overtakes an older one on the same note. Every apply runs
under the queue's drain lock; the frontmost app is re-activated afterwards.
"""

import threading
import time
from typing import Callable, Optional

from apple_notes import session_lock, unified_log

from .write_apply import MODAL_WAIT_REASON
from .write_queue import APPLIED_STATUSES, WriteQueue

_NO_RESTORE = frozenset({"", "com.apple.Notes", "com.apple.loginwindow"})


def frontmost_bundle_id() -> Optional[str]:
    """Bundle id of the frontmost app (a System Events read, no keystrokes)."""
    from apple_notes.applescript import AppleScriptExecutor

    ok, out = AppleScriptExecutor().execute(
        'tell application "System Events" to get bundle identifier of '
        "first application process whose frontmost is true"
    )
    return out.strip() if ok else None


def activate_bundle_id(bundle_id: str) -> None:
    from apple_notes.applescript import AppleScriptExecutor

    escaped = AppleScriptExecutor._escape_applescript_string(bundle_id)
    AppleScriptExecutor().execute(f'tell application id "{escaped}" to activate')


def restore_frontmost(front: Optional[str], activate: Callable[[str], None] = None) -> None:
    """Re-activate the app that was frontmost before a write (never raises).

    Skips Notes, loginwindow and unknown ids: ``activate`` would relaunch an
    app the user had quit.
    """
    if not front or front in _NO_RESTORE:
        return
    try:
        (activate or activate_bundle_id)(front)
    except Exception:
        pass


def notes_modal_title() -> Optional[str]:
    """Title of a dialog open in Notes, else None (a read, no keystrokes)."""
    from apple_notes.ui_scripting import notes_modal_title as probe

    return probe()


def _report(exc: BaseException) -> None:
    unified_log.report_failure("write_queue", "drain pass", exc)


class WriteWorker:
    def __init__(
        self,
        queue: WriteQueue,
        applier,
        *,
        delay: Optional[int] = None,
        auto_apply: Optional[bool] = None,
        clock: Callable[[], float] = time.time,
        session_probe: Callable[[], bool] = session_lock.ui_session_available,
        modal_probe: Callable[[], Optional[str]] = notes_modal_title,
        frontmost: Callable[[], Optional[str]] = frontmost_bundle_id,
        activate: Callable[[str], None] = activate_bundle_id,
        report: Callable[[BaseException], None] = _report,
        period: float = 5.0,
    ):
        cfg = queue.config
        self.queue = queue
        self.applier = applier
        self.delay = cfg.apply_delay_after_unlock_seconds if delay is None else delay
        self.auto_apply = cfg.auto_apply if auto_apply is None else auto_apply
        self.clock = clock
        self._session_probe = session_probe
        self._modal_probe = modal_probe
        self._frontmost = frontmost
        self._activate = activate
        self._report = report
        self.period = period
        self.unlocked_since: Optional[float] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ state
    def _observe_session(self) -> bool:
        if self._session_probe():
            if self.unlocked_since is None:
                self.unlocked_since = self.clock()
            return True
        self.unlocked_since = None
        return False

    def _eligible(self, rec: dict, now: float) -> bool:
        return (
            rec["created_at"] > self.unlocked_since
            or now - self.unlocked_since >= self.delay
        )

    def applies_hint(self) -> str:
        """What a remote caller is told about when its write will land."""
        if not self.auto_apply:
            return "when the owner runs ./run remote-writes apply"
        if self.queue.paused():
            return "the queue is paused; when the owner resumes it"
        if self._session_probe():
            return "now (Mac unlocked)"
        minutes = max(1, round(self.delay / 60))
        return f"{minutes} min after the Mac is next unlocked"

    # ------------------------------------------------------------ drain
    def run_once(self) -> list[str]:
        """One drain pass; returns the ids it applied."""
        self.queue.expire()
        self.queue.purge()
        if not self._observe_session():
            return []
        if not self.auto_apply or self.queue.paused():
            return []
        applied: list[str] = []
        with self.queue.drain_lock() as acquired:
            if not acquired:
                return []
            for rec in self.queue.pending():
                now = self.clock()
                if not self._eligible(rec, now):
                    break  # strict FIFO: nothing overtakes an older write
                if not self._observe_session():
                    break
                if self.queue.paused():
                    break  # the owner paused mid-drain: finish nothing more
                if self._modal_probe():
                    # A dialog is open in Notes (issue #199): starting the
                    # write would back up, show the overlay and pull Notes to
                    # the front only to refuse at the first key - every pass.
                    self.queue.note_waiting(rec["id"], MODAL_WAIT_REASON)
                    break
                front = self._frontmost()
                try:
                    status = self.applier.apply(rec["id"])
                finally:
                    restore_frontmost(front, self._activate)
                if status in APPLIED_STATUSES:
                    applied.append(rec["id"])
                elif status == "queued":
                    break  # locked or busy: try again next pass, in order
        return applied

    def tick(self) -> None:
        try:
            self.run_once()
        except Exception as e:
            self._report(e)

    def recover(self) -> int:
        return self.queue.fail_stuck_applying()

    # ------------------------------------------------------------ thread
    def wake(self) -> None:
        self._wake.set()

    def start(self) -> threading.Thread:
        self.recover()

        def loop():
            while not self._stop.is_set():
                self.tick()
                self._wake.wait(self.period)
                self._wake.clear()

        self._thread = threading.Thread(
            target=loop, name="write-queue-drain", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
