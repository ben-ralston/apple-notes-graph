"""Pasteboard reads must run on a fresh short-lived thread (issue #141).

macOS's CFPasteboard promise resolution has a lost-wakeup race: when the
MCP server's long-lived main thread reads a promised type (e.g. Apple Notes
richtext after a Cmd+C of an attachment-bearing note), the daemon's reply
can fail to wake the nested runloop, stalling the read for CFPasteboard's
fixed 60s timeout — after which it succeeds. The armed state then sticks to
that thread, stalling every later promised read too. A freshly spawned
thread has clean runloop/port state and never hits the race (verified live
against the deterministic reproducer, 2026-08-13).

These tests are pure-mock (no live pasteboard) and stay CI-visible.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from apple_notes import clipboard as clipboard_module
from apple_notes.clipboard import ClipboardManager
from apple_notes.exceptions import ClipboardError


@pytest.fixture
def mock_pasteboard():
    with patch("apple_notes.clipboard.NSPasteboard") as mock_pb_class:
        mock_pb = MagicMock()
        mock_pb_class.generalPasteboard.return_value = mock_pb
        yield mock_pb


def _record_thread(calls):
    def side_effect(*args, **kwargs):
        calls.append(threading.current_thread())
        return None

    return side_effect


class TestReadsRunOnFreshThread:
    def test_read_notes_richtext_runs_off_caller_thread(self, mock_pasteboard):
        calls = []
        mock_pasteboard.dataForType_.side_effect = _record_thread(calls)

        ClipboardManager().read_notes_richtext()

        assert len(calls) == 1
        assert calls[0] is not threading.current_thread()

    def test_read_rtf_runs_off_caller_thread(self, mock_pasteboard):
        calls = []
        mock_pasteboard.dataForType_.side_effect = _record_thread(calls)

        ClipboardManager().read_rtf()

        assert len(calls) == 1
        assert calls[0] is not threading.current_thread()

    def test_read_plain_text_runs_off_caller_thread(self, mock_pasteboard):
        calls = []
        mock_pasteboard.stringForType_.side_effect = _record_thread(calls)

        ClipboardManager().read_plain_text()

        assert len(calls) == 1
        assert calls[0] is not threading.current_thread()

    def test_each_read_uses_a_fresh_thread(self, mock_pasteboard):
        """A new thread per call — never a reused worker (a reused worker
        would accumulate the same armed runloop state as the main thread)."""
        calls = []
        mock_pasteboard.dataForType_.side_effect = _record_thread(calls)

        manager = ClipboardManager()
        manager.read_notes_richtext()
        manager.read_notes_richtext()

        assert len(calls) == 2
        assert calls[0] is not calls[1]

    def test_read_thread_count_returns_to_baseline(self, mock_pasteboard):
        """The hop must not leak threads: _read_via_fresh_thread joins its
        thread before returning, so the process thread count is unchanged."""
        mock_pasteboard.dataForType_.return_value = b"x"
        before = threading.active_count()

        ClipboardManager().read_notes_richtext()

        assert threading.active_count() == before


class TestSaveStateHopsToo:
    """save_state runs before EVERY write against the user's clipboard —
    which may hold promised types from any app — so its reads must hop
    like the rest (Codex review finding on 544364b)."""

    def test_save_state_reads_run_off_caller_thread(self, mock_pasteboard):
        data_calls, string_calls = [], []
        mock_pasteboard.dataForType_.side_effect = _record_thread(data_calls)
        mock_pasteboard.stringForType_.side_effect = _record_thread(string_calls)

        ClipboardManager().save_state()

        assert data_calls and string_calls
        for t in data_calls + string_calls:
            assert t is not threading.current_thread()

    def test_save_state_still_returns_saved_formats(self, mock_pasteboard):
        mock_pasteboard.dataForType_.return_value = b"{\\rtf1 x}"
        mock_pasteboard.stringForType_.return_value = "plain"

        state = ClipboardManager().save_state()

        assert state["rtf"] == b"{\\rtf1 x}"
        assert state["text"] == "plain"


class TestReadSemanticsPreserved:
    def test_read_notes_richtext_returns_bytes(self, mock_pasteboard):
        mock_pasteboard.dataForType_.return_value = b"\x08\x01payload"

        result = ClipboardManager().read_notes_richtext()

        assert result == b"\x08\x01payload"

    def test_read_returns_none_when_type_absent(self, mock_pasteboard):
        mock_pasteboard.dataForType_.return_value = None

        assert ClipboardManager().read_notes_richtext() is None
        assert ClipboardManager().read_rtf() is None

    def test_read_exception_propagates_to_caller(self, mock_pasteboard):
        mock_pasteboard.dataForType_.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            ClipboardManager().read_notes_richtext()

    def test_base_exception_propagates_not_swallowed_as_none(self, mock_pasteboard):
        """A BaseException in the reader must surface, never masquerade as
        'type absent on the pasteboard' (review finding on 544364b)."""
        mock_pasteboard.dataForType_.side_effect = KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            ClipboardManager().read_notes_richtext()


class TestReadTimeout:
    def test_hung_read_raises_clipboard_error(self, mock_pasteboard, monkeypatch):
        monkeypatch.setattr(clipboard_module, "PASTEBOARD_READ_TIMEOUT", 0.05)
        release = threading.Event()

        def hang(*args, **kwargs):
            release.wait(2.0)
            return None

        mock_pasteboard.dataForType_.side_effect = hang

        with pytest.raises(ClipboardError, match="timed out"):
            ClipboardManager().read_notes_richtext()
        release.set()

    def test_timeout_is_generous_enough_to_outlast_the_60s_race(self):
        """The race self-heals at CFPasteboard's 60s timeout; ours must sit
        safely above it so a stalled-but-recovering read still succeeds."""
        assert clipboard_module.PASTEBOARD_READ_TIMEOUT > 60.0


class TestSpansAtChokePoint:
    """Every pasteboard read records its clip_read_* span from inside
    ClipboardManager, so ALL call sites (find_replace, replace, checklist
    toggle, save_state) get timing attribution — a stall must never again
    hide as unattributed time inside `execute` (review finding on 544364b)."""

    def test_each_read_method_records_its_span(self, mock_pasteboard):
        from apple_notes import profiling

        mock_pasteboard.dataForType_.return_value = b"x"
        mock_pasteboard.stringForType_.return_value = "x"

        with profiling.profile("test", "spans") as prof:
            manager = ClipboardManager()
            manager.read_notes_richtext()
            manager.read_rtf()
            manager.read_plain_text()

        assert "clip_read_richtext" in prof.timing_ms
        assert "clip_read_rtf" in prof.timing_ms
        assert "clip_read_text" in prof.timing_ms

    def test_save_state_reads_are_span_attributed(self, mock_pasteboard):
        from apple_notes import profiling

        mock_pasteboard.dataForType_.return_value = b"{\\rtf1 x}"
        mock_pasteboard.stringForType_.return_value = "plain"

        with profiling.profile("test", "spans") as prof:
            ClipboardManager().save_state()

        assert "clip_read_rtf" in prof.timing_ms
        assert "clip_read_text" in prof.timing_ms


class TestNoReadsBypassTheChokePoint:
    """Structural guard: raw pasteboard reads exist ONLY inside the two
    choke-point privates. A new read added anywhere else would silently
    reintroduce the 60s stall — this is the exact shape of the save_state()
    bypass (review finding on 544364b)."""

    def test_raw_read_calls_confined_to_choke_points(self):
        import pathlib

        src = pathlib.Path(__file__).parent.parent / "src" / "apple_notes"
        offenders = []
        for py in sorted(src.rglob("*.py")):
            text = py.read_text()
            if py.name == "clipboard.py":
                assert text.count("self._pasteboard.dataForType_") == 1
                assert text.count("self._pasteboard.stringForType_") == 1
                # Plus the one per-item read inside _snapshot_items (#125).
                assert text.count("dataForType_(") == 2
                assert text.count("item.dataForType_(") == 1
                continue
            for needle in ("dataForType_", "stringForType_"):
                if needle in text:
                    offenders.append(f"{py.name}: {needle}")
        assert offenders == []


class TestWritesStayOnCallerThread:
    """Writes are plain setData (no promise machinery) and never stalled in
    any observed run — they deliberately stay synchronous on the caller."""

    def test_write_notes_richtext_runs_on_caller_thread(self, mock_pasteboard):
        calls = []

        def record(*args, **kwargs):
            calls.append(threading.current_thread())
            return True

        mock_pasteboard.setData_forType_.side_effect = record

        ClipboardManager().write_notes_richtext(b"data", None)

        assert calls and all(t is threading.current_thread() for t in calls)


def test_clipboard_clear_calls_clear_contents(mock_pasteboard):
    ClipboardManager().clear()
    mock_pasteboard.clearContents.assert_called_once_with()
