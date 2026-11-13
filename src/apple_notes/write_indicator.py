"""On-screen write-guard indicator for UI-scripting writes (issue #108).

Shows a floating overlay naming the in-progress write ("Apple Notes MCP
is creating/editing …") for the keystroke window of a UI-scripting write,
holds a brief lead window (a depleting bar, default 1500ms via
NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS) so the user can finish typing, and
clears it when the write ends (success or error).
The overlay is rendered by a helper subprocess (`python -m
apple_notes.write_indicator <action> [note_name]`) because neither the MCP
server process nor the daemon's asyncio worker thread runs the AppKit
main-thread event loop an NSPanel needs.

Showing is deferred (issue #141): `write_indicator()` only *registers* the
overlay for the current write; `show_pending()` spawns it and holds the lead
window. It is called just before `ui_scripting` brings Notes to the front
(`open_note` / `_activate_notes`), so the warning and countdown come while
the user's own app is still frontmost - they finish typing there, not into
Notes - and again (idempotently) at the guarded keystroke choke point for
any path that skips those. The overlay stays up until the write ends.

Fail-open by design: if the helper can't spawn or confirm, the write
proceeds without an indicator. Disable entirely with
NOTES_GRAPH_WRITE_INDICATOR=0.
"""

import contextlib
import contextvars
import logging
import os
import select
import subprocess
import sys
import time

from . import demo_mode, profiling

logger = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 5.0  # max wait for the helper's SHOWN line
HELPER_MAX_LIFETIME = 120.0  # helper self-exits after this, whatever happens
_TERMINATE_GRACE = 2.0
DEFAULT_LEAD_MS = 1500  # lead window shown before the first keystroke


def _enabled() -> bool:
    value = os.environ.get("NOTES_GRAPH_WRITE_INDICATOR", "1")
    return value.strip().lower() not in ("0", "false", "no")


def _lead_ms() -> int:
    """Resolve the lead-window duration in ms from the environment.

    Default 1500. ``0`` disables the lead (immediate keystrokes). Negative or
    unparseable values fall back to the default.
    """
    raw = os.environ.get("NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS")
    if raw is None:
        return DEFAULT_LEAD_MS
    try:
        value = int(raw.strip())
    except (ValueError, TypeError):
        return DEFAULT_LEAD_MS
    return value if value >= 0 else DEFAULT_LEAD_MS


def _bar_fraction(elapsed_secs: float, lead_secs: float) -> float:
    """Remaining fraction (1.0 → 0.0) of the lead window, clamped to [0, 1]."""
    if lead_secs <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - elapsed_secs / lead_secs))


def _spawn_helper(action: str, note_name: str | None) -> subprocess.Popen:
    argv = [sys.executable, "-m", "apple_notes.write_indicator", action]
    if note_name:
        argv.append(note_name)
    # Lock the resolved lead into the child so parent and helper never disagree.
    env = {**os.environ, "NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS": str(_lead_ms())}
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )


def _await_shown(proc: subprocess.Popen, timeout: float) -> bool:
    """Wait for the helper to print SHOWN, so the overlay is visible before
    the first keystroke. Returns False on timeout or helper death."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        remaining = deadline - time.monotonic()
        readable, _, _ = select.select([proc.stdout], [], [], max(remaining, 0))
        if readable:
            line = proc.stdout.readline()
            if not line:  # EOF — helper died
                return False
            if line.strip() == "SHOWN":
                return True
    return False


def _terminate_helper(proc: subprocess.Popen) -> None:
    """Clear the overlay. Must never raise — the write already happened."""
    try:
        if proc.stdin is not None:
            proc.stdin.close()  # EOF is the helper's primary exit signal
        proc.terminate()
        try:
            proc.wait(timeout=_TERMINATE_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_TERMINATE_GRACE)
    except Exception as exc:
        logger.warning("failed to clear write indicator: %s", exc)


class _PendingIndicator:
    """Registered-but-not-yet-shown overlay for the current write context."""

    __slots__ = ("action", "note_name", "proc", "shown", "spawn_attempted")

    def __init__(self, action: str, note_name: str | None):
        self.action = action
        self.note_name = note_name
        self.proc = None
        self.shown = False
        self.spawn_attempted = False


_pending: contextvars.ContextVar[_PendingIndicator | None] = contextvars.ContextVar(
    "write_indicator_pending", default=None
)


def show_pending() -> None:
    """Spawn the overlay registered by write_indicator(), once (issue #141).

    Called right before Notes is activated (`open_note` / `_activate_notes`)
    so the overlay + lead window warn the user before the app switch, and
    again from the keystroke choke point (`_guarded_system_events`) as a
    fallback. Idempotent; no-op when nothing is pending (indicator disabled,
    or no write in progress — read paths reach the same call sites).
    Fail-open: a spawn failure never blocks the write, and is not retried on
    the next keystroke.
    """
    pending = _pending.get()
    if pending is None or pending.spawn_attempted:
        return
    pending.spawn_attempted = True
    with profiling.span("indicator"):
        try:
            pending.proc = _spawn_helper(pending.action, pending.note_name)
            pending.shown = _await_shown(pending.proc, HANDSHAKE_TIMEOUT)
            if not pending.shown:
                # Keep the helper: a late overlay still helps mid-write.
                logger.warning(
                    "write indicator did not confirm within %.1fs; "
                    "continuing without confirmation",
                    HANDSHAKE_TIMEOUT,
                )
        except Exception as exc:
            # Deliberately keep `pending.proc`: if the spawn itself raised it
            # is still None, but if the spawn succeeded and the handshake blew
            # up, dropping the handle would orphan a live overlay until its
            # own 120s self-exit — `write_indicator.__exit__` needs it to
            # terminate the helper.
            logger.warning("write indicator unavailable: %s", exc)
    # Lead window: only meaningful once the overlay is confirmed visible.
    if pending.shown:
        lead_ms = _lead_ms()
        if lead_ms > 0:
            with profiling.span("lead"):
                time.sleep(lead_ms / 1000.0)


@contextlib.contextmanager
def write_indicator(action: str, note_name: str | None = None):
    """Register a deferred overlay for the duration of the block (issue #141).

    The overlay is NOT shown at entry — the step that brings Notes to the
    front (or else the first guarded keystroke) calls show_pending(), which
    spawns it and holds the lead window (a depleting bar). Exit clears the
    overlay if it was spawned. Fail-open throughout.
    """
    demo_mode.require_live(action)
    if not _enabled():
        yield
        return
    pending = _PendingIndicator(action, note_name)
    token = _pending.set(pending)
    try:
        yield
    finally:
        _pending.reset(token)
        if pending.proc is not None:
            _terminate_helper(pending.proc)


# --- Helper-process side ------------------------------------------------
# Everything below runs in the spawned overlay process. AppKit imports are
# kept inside _run_helper so importing this module never loads AppKit.


def _helper_message(action: str, note_name: str | None) -> str:
    verb = "creating" if action == "add_note" else "editing"
    target = f" “{note_name}”" if note_name else " a note"
    return f"Apple Notes MCP is {verb}{target}."


def _install_exit_watchers(max_lifetime: float) -> None:
    """Exit on stdin EOF (parent gone) or after max_lifetime seconds.

    os._exit is deliberate: this is a throwaway overlay process and exiting
    from a watcher thread must not depend on the AppKit runloop.
    """
    import threading

    def watch_stdin() -> None:
        try:
            sys.stdin.buffer.read()  # blocks until parent closes the pipe
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=watch_stdin, daemon=True).start()
    timer = threading.Timer(max_lifetime, lambda: os._exit(0))
    timer.daemon = True
    timer.start()


def _run_helper(action: str, note_name: str | None) -> None:
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSColor,
        NSFont,
        NSMakeRect,
        NSPanel,
        NSScreen,
        NSStatusWindowLevel,
        NSTextAlignmentCenter,
        NSTextField,
        NSView,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from Foundation import NSTimer
    from Quartz import CGColorCreateGenericRGB

    lead_secs = _lead_ms() / 1000.0
    show_bar = lead_secs > 0

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    label = NSTextField.labelWithString_(_helper_message(action, note_name))
    label.setTextColor_(NSColor.whiteColor())
    label.setFont_(NSFont.systemFontOfSize_(15.0))
    label.setAlignment_(NSTextAlignmentCenter)

    screen = NSScreen.mainScreen().frame()
    # Size the panel to the message so long note names aren't truncated.
    width = min(label.intrinsicContentSize().width + 48.0, screen.size.width - 80.0)
    height = 72.0 if show_bar else 56.0
    x = screen.origin.x + (screen.size.width - width) / 2
    y = screen.origin.y + screen.size.height - height - 40  # just below menu bar

    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, width, height),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.setLevel_(NSStatusWindowLevel)
    panel.setIgnoresMouseEvents_(True)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
    )
    panel.setHidesOnDeactivate_(False)

    content = panel.contentView()
    content.setWantsLayer_(True)
    content.layer().setBackgroundColor_(CGColorCreateGenericRGB(0.0, 0.0, 0.0, 0.85))
    content.layer().setCornerRadius_(12.0)

    if show_bar:
        label.setFrame_(NSMakeRect(12.0, 28.0, width - 24.0, height - 34.0))
    else:
        label.setFrame_(NSMakeRect(12.0, 0.0, width - 24.0, height))
    content.addSubview_(label)

    fill = None
    bar_full_width = width - 32.0
    if show_bar:
        bar_margin = 16.0
        bar_height = 6.0
        bar_y = 14.0
        track = NSView.alloc().initWithFrame_(
            NSMakeRect(bar_margin, bar_y, bar_full_width, bar_height)
        )
        track.setWantsLayer_(True)
        track.layer().setBackgroundColor_(CGColorCreateGenericRGB(1.0, 1.0, 1.0, 0.22))
        track.layer().setCornerRadius_(bar_height / 2.0)
        content.addSubview_(track)

        fill = NSView.alloc().initWithFrame_(
            NSMakeRect(bar_margin, bar_y, bar_full_width, bar_height)
        )
        fill.setWantsLayer_(True)
        fill.layer().setBackgroundColor_(CGColorCreateGenericRGB(1.0, 1.0, 1.0, 0.95))
        fill.layer().setCornerRadius_(bar_height / 2.0)
        content.addSubview_(fill)

    panel.orderFrontRegardless()
    print("SHOWN", flush=True)

    if fill is not None:
        start = time.monotonic()

        def _tick(timer):
            frac = _bar_fraction(time.monotonic() - start, lead_secs)
            frame = fill.frame()
            frame.size.width = bar_full_width * frac
            fill.setFrame_(frame)
            if frac <= 0.0:
                timer.invalidate()

        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.0 / 30.0, True, _tick)

    _install_exit_watchers(HELPER_MAX_LIFETIME)
    app.run()


if __name__ == "__main__":
    _action = sys.argv[1] if len(sys.argv) > 1 else "write"
    _note = sys.argv[2] if len(sys.argv) > 2 else None
    _run_helper(_action, _note)
