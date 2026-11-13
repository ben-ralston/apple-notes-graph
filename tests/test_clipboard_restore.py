"""restore_state must not leave plain text to be produced on demand (issue #248),
and must bring back everything else that was on the clipboard (issue #125).

With RTF alone on the pasteboard, a later plain-text read is answered by the
process that wrote the RTF. If that process is alive but not serving pasteboard
requests, the reader hangs until the 90s pasteboard timeout, and save_state runs
before every write. test_clipboard.py is macos_gui-marked, so CI never runs it:
these use a fake pasteboard and stay CI-visible.
"""

import logging
import threading
import time
from unittest.mock import patch

import pytest
from AppKit import NSData, NSPasteboardTypeRTF, NSPasteboardTypeString

from apple_notes import clipboard as clipboard_module
from apple_notes import profiling, unified_log
from apple_notes.clipboard import ClipboardManager

RTF = (
    rb"{\rtf1\ansi\ansicpg1252{\fonttbl\f0\fswiss Helvetica;}"
    rb"\f0 caf\'e9 rich}"
)


class FakePasteboard:
    """Holds what was written, per type, the way one pasteboard item would."""

    def __init__(self):
        self.types: dict[str, object] = {}

    def clearContents(self):
        self.types.clear()
        return 1

    def setData_forType_(self, data, pb_type):
        self.types[pb_type] = bytes(data)
        return True

    def setString_forType_(self, text, pb_type):
        self.types[pb_type] = text
        return True


@pytest.fixture
def board():
    fake = FakePasteboard()
    with patch("apple_notes.clipboard.NSPasteboard") as pasteboard:
        pasteboard.generalPasteboard.return_value = fake
        yield fake


def test_restore_writes_both_types_when_both_were_saved(board):
    ClipboardManager().restore_state({"rtf": RTF, "text": "café ☃ rich"})
    assert board.types == {
        NSPasteboardTypeRTF: RTF,
        NSPasteboardTypeString: "café ☃ rich",
    }


def test_restore_text_only_writes_text_only(board):
    ClipboardManager().restore_state({"text": "plain"})
    assert board.types == {NSPasteboardTypeString: "plain"}


def test_restore_empty_state_leaves_the_pasteboard_empty(board):
    board.types["stale"] = b"x"
    ClipboardManager().restore_state({})
    assert board.types == {}


def test_restore_keeps_empty_saved_text(board):
    # Saved as "" is still an explicit string: nothing left to produce on demand.
    ClipboardManager().restore_state({"rtf": RTF, "text": ""})
    assert board.types[NSPasteboardTypeString] == ""


def test_restore_rtf_without_text_writes_the_text_the_rtf_holds(board):
    # Measured: RTF alone stalls the next plain-text read just the same, and a
    # clipboard can be saved this way after an earlier restore lost its text.
    ClipboardManager().restore_state({"rtf": RTF})
    assert board.types[NSPasteboardTypeRTF] == RTF
    assert board.types[NSPasteboardTypeString] == "café rich"


def test_restore_unreadable_rtf_still_restores_the_rtf(board):
    ClipboardManager().restore_state({"rtf": b"not rtf"})
    assert board.types[NSPasteboardTypeRTF] == b"not rtf"
    # No text in it to recover, but an explicit string still rules out the stall.
    assert board.types[NSPasteboardTypeString] == ""


# --- issue #125 (NTG-020): keep everything else on the clipboard ------------

PNG = b"\x89PNG\r\n\x1a\n fake image bytes"
FILE_URL = "public.file-url"
PNG_TYPE = "public.png"


class FakeItem:
    """One pasteboard item as read back: its declared types and their data."""

    def __init__(self, data: dict[str, bytes], on_read=None):
        self._data = data
        self._on_read = on_read

    def types(self):
        return list(self._data)

    def dataForType_(self, pb_type):
        if self._on_read is not None:
            self._on_read(pb_type)
        data = self._data.get(pb_type)
        if data is None or not isinstance(data, bytes):
            return data
        return NSData.dataWithBytes_length_(data, len(data))


class ItemBoard(FakePasteboard):
    """A pasteboard holding several items, with a change count like the real one."""

    def __init__(self, items=None):
        super().__init__()
        self.items = items or []
        self.count = 7
        self.written: list = []
        self.clears = 0

    def changeCount(self):
        return self.count

    def clearContents(self):
        self.clears += 1
        self.count += 1
        self.items = []
        return super().clearContents()

    def setData_forType_(self, data, pb_type):
        self.count += 1
        return super().setData_forType_(data, pb_type)

    def setString_forType_(self, text, pb_type):
        self.count += 1
        return super().setString_forType_(text, pb_type)

    def pasteboardItems(self):
        return list(self.items)

    def dataForType_(self, pb_type):
        first = self.items[0] if self.items else None
        return None if first is None else first.dataForType_(pb_type)

    def stringForType_(self, pb_type):
        data = self.dataForType_(pb_type)
        return None if data is None else bytes(data).decode()

    def writeObjects_(self, objects):
        self.count += 1
        self.written = [
            {t: bytes(o.dataForType_(t)) for t in o.types()} for o in objects
        ]
        return True


@pytest.fixture(autouse=True)
def no_snapshot_left_running():
    """A timed-out snapshot's thread clears _snapshot_in_flight when its read
    returns; don't let one test's leftover thread skip the next test's."""
    yield
    deadline = time.monotonic() + 6
    while clipboard_module._snapshot_in_flight.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    clipboard_module._snapshot_in_flight.clear()


@pytest.fixture
def items_board():
    fake = ItemBoard()
    with patch("apple_notes.clipboard.NSPasteboard") as pasteboard:
        pasteboard.generalPasteboard.return_value = fake
        yield fake


def _screenshot_and_files(board):
    board.items = [
        FakeItem({PNG_TYPE: PNG, FILE_URL: b"file:///tmp/a.png"}),
        FakeItem({FILE_URL: b"file:///tmp/b.txt"}),
    ]


def test_save_snapshots_every_item_and_type(items_board):
    _screenshot_and_files(items_board)
    state = ClipboardManager().save_state()
    assert state["items"] == [
        {PNG_TYPE: PNG, FILE_URL: b"file:///tmp/a.png"},
        {FILE_URL: b"file:///tmp/b.txt"},
    ]


def test_write_then_restore_brings_back_an_image_and_files(items_board):
    _screenshot_and_files(items_board)
    manager = ClipboardManager()
    state = manager.save_state()
    manager.write_plain_text("note body the write pasted")  # the write's own copy
    manager.restore_state(state)
    assert items_board.written == [
        {PNG_TYPE: PNG, FILE_URL: b"file:///tmp/a.png"},
        {FILE_URL: b"file:///tmp/b.txt"},
    ]


def test_restore_is_skipped_when_the_write_never_touched_the_clipboard(items_board):
    _screenshot_and_files(items_board)
    manager = ClipboardManager()
    state = manager.save_state()
    clears = items_board.clears
    manager.restore_state(state)
    assert items_board.clears == clears
    assert items_board.written == []


def test_restored_first_item_always_carries_explicit_text(items_board):
    # #248: RTF with no string must never be left for on-demand conversion.
    items_board.items = [FakeItem({NSPasteboardTypeRTF: RTF})]
    manager = ClipboardManager()
    state = manager.save_state()
    manager.clear()
    manager.restore_state(state)
    first = items_board.written[0]
    assert first[NSPasteboardTypeRTF] == RTF
    assert first[NSPasteboardTypeString].decode() == "café rich"


def test_restored_items_keep_their_own_saved_text(items_board):
    items_board.items = [
        FakeItem({NSPasteboardTypeRTF: RTF, NSPasteboardTypeString: b"own text"})
    ]
    manager = ClipboardManager()
    state = manager.save_state()
    manager.clear()
    manager.restore_state(state)
    assert items_board.written[0][NSPasteboardTypeString] == b"own text"


def test_snapshot_runs_off_the_caller_thread_with_a_span(items_board):
    threads = []
    items_board.items = [
        FakeItem({PNG_TYPE: PNG}, on_read=lambda _t: threads.append(threading.current_thread()))
    ]
    with profiling.profile("test", "spans") as prof:
        ClipboardManager().save_state()
    assert threads and all(t is not threading.current_thread() for t in threads)
    assert "clip_read_items" in prof.timing_ms


def test_slow_snapshot_is_abandoned_and_the_write_goes_on(items_board, caplog):
    release = threading.Event()
    items_board.items = [
        # Only the image is slow; the RTF/text reads before it answer at once.
        FakeItem({PNG_TYPE: PNG}, on_read=lambda t: t == PNG_TYPE and release.wait(5)),
    ]
    with patch.object(clipboard_module, "SNAPSHOT_TIMEOUT", 0.2):
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="apple_notes.clipboard"):
            state = ClipboardManager().save_state()
        elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 2.0
    assert "items" not in state
    assert "change_count" in state
    assert "not saved in full" in caplog.text


def test_oversized_snapshot_is_dropped_with_a_log_line(items_board, caplog):
    items_board.items = [FakeItem({PNG_TYPE: b"x" * 64})]
    with patch.object(clipboard_module, "SNAPSHOT_MAX_BYTES", 16):
        with caplog.at_level(logging.WARNING, logger="apple_notes.clipboard"):
            state = ClipboardManager().save_state()
    assert "items" not in state
    assert "not saved in full" in caplog.text
    # The log line names sizes and counts only, never clipboard contents.
    assert "x" * 16 not in caplog.text


def test_failed_snapshot_falls_back_to_text_restore(items_board):
    items_board.items = [FakeItem({NSPasteboardTypeString: b"kept text"})]
    with patch.object(clipboard_module, "SNAPSHOT_MAX_BYTES", 1):
        manager = ClipboardManager()
        state = manager.save_state()
    manager.clear()
    manager.restore_state(state)
    assert items_board.types == {NSPasteboardTypeString: "kept text"}


def test_snapshot_timeout_keeps_the_reason_code_prefix(items_board):
    # unified_log maps the "Pasteboard read timed out" prefix to a reason
    # code (#255); the snapshot's shorter budget must keep that wording.
    release = threading.Event()
    with pytest.raises(clipboard_module.ClipboardError) as caught:
        ClipboardManager()._read_via_fresh_thread(lambda: release.wait(2), timeout=0.05)
    release.set()
    assert str(caught.value).startswith("Pasteboard read timed out")
    assert unified_log.reason_code(caught.value) == "pasteboard_read_timeout"


class FakeHugeData:
    """NSData stand-in that is too big to copy: only its length may be read."""

    def length(self):
        return clipboard_module.SNAPSHOT_MAX_BYTES + 1

    def __bytes__(self):
        raise AssertionError("an oversized type was copied before the cap check")


def test_oversized_type_is_refused_before_it_is_copied(items_board, caplog):
    items_board.items = [FakeItem({PNG_TYPE: FakeHugeData()})]
    with patch.object(unified_log, "report_failure") as report:
        with caplog.at_level(logging.WARNING, logger="apple_notes.clipboard"):
            state = ClipboardManager().save_state()
    assert "items" not in state
    assert "before a write (over 256 MB" in caplog.text
    report.assert_not_called()  # an expected limit, not a fault


def test_abandoned_snapshot_still_in_flight_skips_the_next_one(items_board, caplog):
    release = threading.Event()
    reads = []

    def slow(pb_type):
        if pb_type == PNG_TYPE:
            reads.append(pb_type)
            release.wait(5)

    items_board.items = [FakeItem({PNG_TYPE: PNG}, on_read=slow)]
    with patch.object(clipboard_module, "SNAPSHOT_TIMEOUT", 0.2):
        ClipboardManager().save_state()  # abandoned, its read still blocked
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="apple_notes.clipboard"):
            state = ClipboardManager().save_state()
        elapsed = time.monotonic() - started
        assert "items" not in state
        assert reads == [PNG_TYPE]  # no second blocked read piled up
        assert elapsed < 0.15  # and no second wait
        assert "still running" in caplog.text
        release.set()
        deadline = time.monotonic() + 2
        while clipboard_module._snapshot_in_flight.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "items" in ClipboardManager().save_state()


def test_clipboard_changed_during_save_drops_the_snapshot(items_board):
    # The user copies something new mid-save: the text read and the snapshot
    # are from different clipboards, so the items must not be restored.
    def user_copies(pb_type):
        if pb_type == PNG_TYPE:
            items_board.count += 1

    items_board.items = [
        FakeItem({NSPasteboardTypeString: b"A", PNG_TYPE: PNG}, on_read=user_copies)
    ]
    state = ClipboardManager().save_state()
    assert "items" not in state


def test_unexpected_snapshot_error_reaches_the_unified_logger(items_board):
    class Broken:
        def types(self):
            raise RuntimeError("pyobjc changed")

        def dataForType_(self, _pb_type):  # the RTF/text reads still work
            return None

    items_board.items = [Broken()]
    with patch.object(unified_log, "report_failure") as report:
        state = ClipboardManager().save_state()
    assert "items" not in state
    report.assert_called_once()
    assert report.call_args.kwargs["exc"].__class__ is RuntimeError


PROMISE_TYPES = (
    "com.apple.NSFilePromiseItemMetaData",
    "com.apple.pasteboard.promised-file-url",
    "com.apple.pasteboard.promised-file-content-type",
)


def test_file_promise_types_are_never_read_or_restored(items_board):
    read = []
    items_board.items = [
        FakeItem(
            {PNG_TYPE: PNG, **{t: b"promise" for t in PROMISE_TYPES}},
            on_read=read.append,
        )
    ]
    state = ClipboardManager().save_state()
    assert state["items"] == [{PNG_TYPE: PNG}]
    assert not set(read) & set(PROMISE_TYPES)


def test_rich_text_first_item_without_text_gets_an_explicit_string(items_board):
    # #248: HTML alone is converted to text on demand by this process too.
    items_board.items = [FakeItem({"public.html": b"<b>hi</b>"})]
    manager = ClipboardManager()
    state = manager.save_state()
    assert "text" not in state
    manager.clear()
    manager.restore_state(state)
    assert items_board.written[0][NSPasteboardTypeString] == b""


def test_image_only_first_item_gets_no_string(items_board):
    # A string would make apps paste "" instead of the image.
    items_board.items = [FakeItem({PNG_TYPE: PNG})]
    manager = ClipboardManager()
    state = manager.save_state()
    manager.clear()
    manager.restore_state(state)
    assert NSPasteboardTypeString not in items_board.written[0]
