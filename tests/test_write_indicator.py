"""Tests for the write-guard indicator context manager (issue #108)."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apple_notes import write_indicator as wi

# Child processes spawned by tests import apple_notes; pytest's
# pythonpath = ["src"] doesn't propagate to them.
_CHILD_ENV = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}


@pytest.fixture(autouse=True)
def _enable_indicator(monkeypatch):
    """This file tests the indicator itself; undo conftest's kill-switch."""
    monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR", "1")


@pytest.fixture
def mock_helper():
    """Patch the subprocess plumbing; yield the mocks."""
    with (
        patch("apple_notes.write_indicator._spawn_helper") as spawn,
        patch(
            "apple_notes.write_indicator._await_shown", return_value=True
        ) as await_shown,
        patch("apple_notes.write_indicator._terminate_helper") as terminate,
        patch("apple_notes.write_indicator._lead_ms", return_value=0),
    ):
        spawn.return_value = MagicMock(name="proc")
        yield spawn, await_shown, terminate


class TestLeadMs:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS", raising=False)
        assert wi._lead_ms() == 1500

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS", "0")
        assert wi._lead_ms() == 0

    def test_positive_value(self, monkeypatch):
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS", "800")
        assert wi._lead_ms() == 800

    def test_negative_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS", "-5")
        assert wi._lead_ms() == 1500

    def test_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS", "abc")
        assert wi._lead_ms() == 1500


class TestBarFraction:
    def test_full_at_start(self):
        assert wi._bar_fraction(0.0, 1.5) == 1.0

    def test_half_at_midpoint(self):
        assert wi._bar_fraction(0.75, 1.5) == 0.5

    def test_empty_at_end(self):
        assert wi._bar_fraction(1.5, 1.5) == 0.0

    def test_clamped_past_end(self):
        assert wi._bar_fraction(3.0, 1.5) == 0.0

    def test_zero_lead_is_empty(self):
        assert wi._bar_fraction(0.0, 0.0) == 0.0


class TestEnabled:
    @pytest.mark.parametrize("value", ["0", "false", "no", "False", "NO"])
    def test_disabled_values(self, monkeypatch, value):
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR", value)
        assert wi._enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", ""])
    def test_enabled_values(self, monkeypatch, value):
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR", value)
        assert wi._enabled() is True

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("NOTES_GRAPH_WRITE_INDICATOR", raising=False)
        assert wi._enabled() is True


class TestWriteIndicator:
    def test_spawns_and_terminates(self, mock_helper):
        spawn, await_shown, terminate = mock_helper
        with wi.write_indicator("add_note", "My Note"):
            wi.show_pending()
            spawn.assert_called_once_with("add_note", "My Note")
            terminate.assert_not_called()
        terminate.assert_called_once_with(spawn.return_value)

    def test_terminates_on_exception(self, mock_helper):
        spawn, _, terminate = mock_helper
        with pytest.raises(RuntimeError, match="boom"):
            with wi.write_indicator("add_note", "My Note"):
                wi.show_pending()
                raise RuntimeError("boom")
        terminate.assert_called_once_with(spawn.return_value)

    def test_disabled_env_skips_spawn(self, mock_helper, monkeypatch):
        spawn, _, terminate = mock_helper
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR", "0")
        with wi.write_indicator("add_note", "My Note"):
            wi.show_pending()
        spawn.assert_not_called()
        terminate.assert_not_called()

    def test_fail_open_on_spawn_error(self, mock_helper):
        spawn, _, terminate = mock_helper
        spawn.side_effect = OSError("no such file")
        ran = False
        with wi.write_indicator("add_note", "My Note"):
            wi.show_pending()
            ran = True
        assert ran
        terminate.assert_not_called()

    def test_handshake_timeout_keeps_helper_and_proceeds(self, mock_helper):
        # A late-showing overlay is still useful: keep the helper alive,
        # don't block the write, and still terminate it at the end.
        spawn, await_shown, terminate = mock_helper
        await_shown.return_value = False
        ran = False
        with wi.write_indicator("add_note", "My Note"):
            wi.show_pending()
            ran = True
        assert ran
        terminate.assert_called_once_with(spawn.return_value)


class TestDeferredShow:
    """Issue #141: the overlay is registered at entry, spawned at the first
    guarded keystroke — so a stall before the keystrokes doesn't leave a
    countdown-finished popup sitting on screen."""

    def test_enter_does_not_spawn(self, mock_helper):
        spawn, _, _ = mock_helper
        with wi.write_indicator("update_note_content(append)", "Note"):
            assert spawn.call_count == 0

    def test_show_pending_spawns_once(self, mock_helper):
        spawn, _, _ = mock_helper
        with wi.write_indicator("update_note_content(append)", "Note"):
            wi.show_pending()
            wi.show_pending()
            wi.show_pending()
            assert spawn.call_count == 1

    def test_failed_spawn_is_not_retried(self, mock_helper):
        spawn, _, _ = mock_helper
        spawn.side_effect = OSError("no exec")
        with wi.write_indicator("add_note", "N"):
            wi.show_pending()
            wi.show_pending()
        assert spawn.call_count == 1

    def test_show_pending_noop_outside_context(self, mock_helper):
        spawn, _, terminate = mock_helper
        wi.show_pending()  # must not raise (read paths call it too)
        spawn.assert_not_called()
        terminate.assert_not_called()

    def test_pending_cleared_after_context(self, mock_helper):
        spawn, _, _ = mock_helper
        with wi.write_indicator("add_note", "N"):
            pass
        wi.show_pending()
        spawn.assert_not_called()

    def test_pending_cleared_after_exception(self, mock_helper):
        spawn, _, _ = mock_helper
        with pytest.raises(RuntimeError):
            with wi.write_indicator("add_note", "N"):
                raise RuntimeError("boom")
        wi.show_pending()
        spawn.assert_not_called()

    def test_helper_terminated_on_exit_when_shown(self, mock_helper):
        spawn, _, terminate = mock_helper
        with wi.write_indicator("add_note", "N"):
            wi.show_pending()
            terminate.assert_not_called()
        terminate.assert_called_once_with(spawn.return_value)

    def test_no_terminate_when_never_shown(self, mock_helper):
        _, _, terminate = mock_helper
        with wi.write_indicator("add_note", "N"):
            pass
        terminate.assert_not_called()

    def test_disabled_env_full_noop(self, mock_helper, monkeypatch):
        spawn, _, terminate = mock_helper
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR", "0")
        with wi.write_indicator("add_note", "Note"):
            wi.show_pending()  # no spawn, no error
        spawn.assert_not_called()
        terminate.assert_not_called()

    def test_handshake_error_still_terminates_the_spawned_helper(self, mock_helper):
        """A spawned helper must never be orphaned (issue #141 fix-wave).

        If `_await_shown` raises after the spawn succeeded, dropping the proc
        handle left a live overlay on screen until its own 120s self-exit.
        """
        spawn, await_shown, terminate = mock_helper
        await_shown.side_effect = OSError("read failed")
        ran = False
        with wi.write_indicator("add_note", "N"):
            wi.show_pending()  # fail-open: must not raise
            ran = True
        assert ran
        terminate.assert_called_once_with(spawn.return_value)

    def test_records_indicator_span_at_show_time(self, mock_helper):
        from apple_notes import profiling

        with profiling.profile("test") as prof:
            with wi.write_indicator("add_note", "N"):
                assert "indicator" not in prof.timing_ms
                wi.show_pending()
        assert "indicator" in prof.timing_ms


class TestLeadWindow:
    def test_sleeps_at_show_time_not_at_entry(self, mock_helper):
        with (
            patch("apple_notes.write_indicator._lead_ms", return_value=1500),
            patch("apple_notes.write_indicator.time.sleep") as sleep,
        ):
            with wi.write_indicator("add_note", "N"):
                sleep.assert_not_called()
                wi.show_pending()
                sleep.assert_called_once_with(1.5)

    def test_skips_when_not_shown(self, mock_helper):
        _, await_shown, _ = mock_helper
        await_shown.return_value = False
        with (
            patch("apple_notes.write_indicator._lead_ms", return_value=1500),
            patch("apple_notes.write_indicator.time.sleep") as sleep,
        ):
            with wi.write_indicator("add_note", "N"):
                wi.show_pending()
        sleep.assert_not_called()

    def test_skips_when_spawn_fails(self, mock_helper):
        spawn, _, _ = mock_helper
        spawn.side_effect = OSError("no exec")
        with (
            patch("apple_notes.write_indicator._lead_ms", return_value=1500),
            patch("apple_notes.write_indicator.time.sleep") as sleep,
        ):
            with wi.write_indicator("add_note", "N"):
                wi.show_pending()
        sleep.assert_not_called()

    def test_sleeps_once_across_repeated_show_calls(self, mock_helper):
        with (
            patch("apple_notes.write_indicator._lead_ms", return_value=1500),
            patch("apple_notes.write_indicator.time.sleep") as sleep,
        ):
            with wi.write_indicator("add_note", "N"):
                wi.show_pending()
                wi.show_pending()
        sleep.assert_called_once_with(1.5)

    def test_zero_lead_no_sleep(self, mock_helper):
        with (
            patch("apple_notes.write_indicator._lead_ms", return_value=0),
            patch("apple_notes.write_indicator.time.sleep") as sleep,
        ):
            with wi.write_indicator("add_note", "N"):
                wi.show_pending()
        sleep.assert_not_called()

    def test_records_lead_profiling_span(self, mock_helper):
        from apple_notes import profiling

        with (
            patch("apple_notes.write_indicator._lead_ms", return_value=1500),
            patch("apple_notes.write_indicator.time.sleep"),
        ):
            with profiling.profile("test") as prof:
                with wi.write_indicator("add_note", "N"):
                    wi.show_pending()
        assert "lead" in prof.timing_ms


class TestSpawnHelperEnv:
    def test_passes_resolved_lead_ms(self, monkeypatch):
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS", "800")
        with patch("apple_notes.write_indicator.subprocess.Popen") as popen:
            wi._spawn_helper("add_note", "N")
        env = popen.call_args.kwargs["env"]
        assert env["NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS"] == "800"

    def test_normalizes_invalid_lead_ms(self, monkeypatch):
        monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS", "garbage")
        with patch("apple_notes.write_indicator.subprocess.Popen") as popen:
            wi._spawn_helper("add_note", None)
        env = popen.call_args.kwargs["env"]
        assert env["NOTES_GRAPH_WRITE_INDICATOR_LEAD_MS"] == "1500"


class TestAwaitShown:
    def _proc(self, code: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(code)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def test_returns_true_on_shown_line(self):
        proc = self._proc("print('SHOWN', flush=True); import time; time.sleep(5)")
        try:
            assert wi._await_shown(proc, timeout=5.0) is True
        finally:
            proc.kill()
            proc.wait()

    def test_returns_false_on_timeout(self):
        proc = self._proc("import time; time.sleep(5)")
        try:
            assert wi._await_shown(proc, timeout=0.2) is False
        finally:
            proc.kill()
            proc.wait()

    def test_returns_false_if_helper_dies(self):
        proc = self._proc("raise SystemExit(1)")
        try:
            assert wi._await_shown(proc, timeout=2.0) is False
        finally:
            proc.kill()
            proc.wait()


class TestTerminateHelper:
    def test_terminates_running_helper(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        wi._terminate_helper(proc)
        assert proc.poll() is not None

    def test_never_raises_on_dead_helper(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        proc.wait()
        wi._terminate_helper(proc)  # must not raise


class TestHelperLogic:
    def test_helper_message_creating_with_note(self):
        msg = wi._helper_message("add_note", "My Note")
        assert msg == "Apple Notes MCP is creating “My Note”."

    def test_helper_message_editing_with_note(self):
        msg = wi._helper_message("update_note_content(find_replace)", "My Note")
        assert msg == "Apple Notes MCP is editing “My Note”."

    def test_helper_message_editing_toggle(self):
        msg = wi._helper_message("toggle_checklist_item", "My Note")
        assert msg == "Apple Notes MCP is editing “My Note”."

    def test_helper_message_without_note(self):
        msg = wi._helper_message("add_note", None)
        assert msg == "Apple Notes MCP is creating a note."

    def test_stdin_eof_exits_helper_process(self):
        # Run only the exit-watcher machinery (no AppKit) in a child process:
        # closing stdin must terminate it well before the 30s sleep ends.
        code = (
            "from apple_notes.write_indicator import _install_exit_watchers\n"
            "import time\n"
            "_install_exit_watchers(max_lifetime=30.0)\n"
            "print('READY', flush=True)\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=_CHILD_ENV,
        )
        try:
            assert proc.stdout.readline().strip() == "READY"
            proc.stdin.close()
            assert proc.wait(timeout=5) == 0
        finally:
            proc.kill()

    def test_max_lifetime_exits_helper_process(self):
        code = (
            "from apple_notes.write_indicator import _install_exit_watchers\n"
            "import time\n"
            "_install_exit_watchers(max_lifetime=0.3)\n"
            "print('READY', flush=True)\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=_CHILD_ENV,
        )
        try:
            assert proc.stdout.readline().strip() == "READY"
            assert proc.wait(timeout=5) == 0
        finally:
            proc.kill()
