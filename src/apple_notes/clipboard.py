"""Clipboard operations for RTF content on macOS."""

import logging
import threading
from typing import Optional

import objc

from . import demo_mode, profiling, unified_log
from AppKit import (
    NSPasteboard,
    NSPasteboardItem,
    NSPasteboardTypeRTF,
    NSPasteboardTypeString,
    NSData,
    NSAttributedString,
    NSMutableAttributedString,
    NSFont,
)
from Foundation import NSMakeRange, NSString

from .exceptions import ClipboardError
from .title_scope import body_start_offset, rtf_body_start_offset

# Apple Notes proprietary pasteboard type - preserves all formatting including
# checklists, block quotes, and other Notes-specific elements
NOTES_RICHTEXT_TYPE = "com.apple.notes.richtext"

# Upper bound on a single pasteboard read (issue #141). CFPasteboard's
# promise machinery self-heals a lost wakeup at its own fixed 60s timeout,
# so this must sit safely above 60s: a read that hits the race despite the
# fresh-thread dodge still returns its data rather than erroring.
PASTEBOARD_READ_TIMEOUT = 90.0

# Before a write, every type on every pasteboard item is snapshotted so an
# image or Finder files on the user's clipboard survive the write (issue #125,
# NTG-020). The snapshot is optional, so it gets its own short budget: a
# source app slow to produce its data costs the write at most this long, and
# the write then goes on with text-only restore, as before #125.
SNAPSHOT_TIMEOUT = 5.0
# Held in memory for the length of one write. Comfortably above a 5K Retina
# screenshot (~60 MB as TIFF).
SNAPSHOT_MAX_BYTES = 256 * 1024 * 1024

# File promises (Photos, Mail attachments): only the source app can keep one.
# Reading the type asks that app to start delivering the file, and a restored
# copy is a promise nobody keeps, so pasting it into Finder would fail. Never
# read or restore them; the item's other types still come back.
_PROMISE_TYPES = frozenset(
    {
        "com.apple.NSFilePromiseItemMetaData",
        "NSPromiseContentsPboardType",
    }
)
_PROMISE_TYPE_PREFIX = "com.apple.pasteboard.promised-"

# Types AppKit turns into plain text on demand, answered by the process that
# wrote them: like RTF in #248, a restored first item holding one of these
# gets an explicit string so the next write's text read can't stall.
_TEXT_CONVERTIBLE_TYPES = frozenset(
    {
        NSPasteboardTypeRTF,
        "public.html",
        "public.rtfd",
        "com.apple.flat-rtfd",
        "com.apple.webarchive",
        "Apple Web Archive pasteboard type",
    }
)

# Set while a snapshot's reader thread runs. A snapshot that timed out leaves
# its thread blocked in dataForType_ until the source app answers (up to
# CFPasteboard's 60s); while it is, later writes skip the snapshot rather than
# pile up another blocked thread and another SNAPSHOT_TIMEOUT wait.
_snapshot_in_flight = threading.Event()

logger = logging.getLogger(__name__)


class _SnapshotTooLarge(Exception):
    """The clipboard holds more than SNAPSHOT_MAX_BYTES."""


class _SnapshotStillRunning(Exception):
    """An earlier, abandoned snapshot is still blocked reading the clipboard."""


class ClipboardManager:
    """Manage clipboard operations for RTF content.

    All reads run on a fresh short-lived thread (see ``_read_via_fresh_thread``);
    writes are plain ``setData`` calls with no promise machinery and stay
    synchronous on the caller.
    """

    def __init__(self):
        demo_mode.require_live("Using the clipboard for a Notes write")
        self._pasteboard = NSPasteboard.generalPasteboard()

    def _read_via_fresh_thread(self, reader, timeout: float | None = None):
        """Run a pasteboard read on a freshly spawned thread.

        Reading a *promised* pasteboard type (what Notes puts up for Cmd+C,
        fulfilled lazily by the pasteboard daemon) from the MCP server's
        long-lived main thread can hit a macOS lost-wakeup race: the
        daemon's reply never wakes the nested runloop inside
        ``dataForType_``, the call sleeps until CFPasteboard's fixed 60s
        timeout, then finds the data and succeeds — and the armed state
        sticks to that thread, stalling every subsequent promised read too
        (issue #141: deterministic ~60s stalls on find_replace after
        copying an attachment-bearing note). A fresh thread has clean
        runloop/port state and reliably gets the wakeup; spawn cost is
        microseconds against a 60s downside.

        Args:
            reader: Zero-arg callable performing the actual pasteboard read.
            timeout: Seconds to wait for it (PASTEBOARD_READ_TIMEOUT unless
                the read is optional, like the whole-clipboard snapshot).

        Returns:
            Whatever ``reader`` returns.

        Raises:
            ClipboardError: If the read exceeds ``timeout``.
            Exception: Whatever ``reader`` raised, re-raised on the caller.
        """
        if timeout is None:
            timeout = PASTEBOARD_READ_TIMEOUT
        result: dict = {}

        def run():
            try:
                # PyObjC requires an autorelease pool on threads that call
                # Cocoa; without one, autoreleased objects leak per read.
                with objc.autorelease_pool():
                    result["value"] = reader()
            except BaseException as exc:
                # BaseException too: a SystemExit/KeyboardInterrupt on the
                # reader must surface on the caller, never masquerade as
                # "type absent" (silent None).
                result["error"] = exc

        thread = threading.Thread(target=run, name="pasteboard-read", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            # Python threads cannot be force-stopped, so a read that outlives
            # the join leaks its (daemon) thread until dataForType_ returns on
            # its own. At PASTEBOARD_READ_TIMEOUT that should never happen in
            # the known failure mode (it sits above CFPasteboard's 60s
            # self-heal); the snapshot's shorter budget can, so it guards
            # against piling up such threads (_snapshot_in_flight).
            raise ClipboardError(f"Pasteboard read timed out after {timeout:.0f}s")
        if "error" in result:
            raise result["error"]
        # The thread completed and stored no error, so "value" must exist;
        # a KeyError here means the impossible state and should be loud.
        return result["value"]

    def _read_data(self, type_identifier: str, span_name: str) -> Optional[bytes]:
        """Choke point for all pasteboard data reads: fresh-thread hop +
        clip_read_* span. Never call ``dataForType_`` anywhere else — a
        direct call reintroduces the 60s stall and loses timing attribution
        (guarded by tests/test_clipboard_read_thread.py). The one other read,
        per item in ``_snapshot_items``, makes the same hop under its own
        ``clip_read_items`` span."""

        def read():
            data = self._pasteboard.dataForType_(type_identifier)
            if data is None:
                return None
            return bytes(data)

        with profiling.span(span_name):
            return self._read_via_fresh_thread(read)

    def _read_string(self, type_identifier: str, span_name: str) -> Optional[str]:
        """Choke point for all pasteboard string reads — see ``_read_data``."""
        with profiling.span(span_name):
            return self._read_via_fresh_thread(
                lambda: self._pasteboard.stringForType_(type_identifier)
            )

    def read_rtf(self) -> Optional[bytes]:
        """
        Read RTF data from clipboard.

        Returns:
            RTF data as bytes, or None if no RTF data available
        """
        return self._read_data(NSPasteboardTypeRTF, "clip_read_rtf")

    def write_rtf(self, rtf_data: bytes) -> None:
        """
        Write RTF data to clipboard.

        Args:
            rtf_data: RTF content as bytes

        Raises:
            ClipboardError: If writing to clipboard fails
        """
        self._pasteboard.clearContents()
        ns_data = NSData.dataWithBytes_length_(rtf_data, len(rtf_data))
        result = self._pasteboard.setData_forType_(ns_data, NSPasteboardTypeRTF)
        if not result:
            raise ClipboardError("Failed to write RTF data to clipboard")

    def read_notes_richtext(self) -> Optional[bytes]:
        """
        Read Apple Notes proprietary richtext format from clipboard.

        This format (com.apple.notes.richtext) preserves ALL Notes formatting
        including checklists, block quotes, and other Notes-specific elements
        that are lost when using standard RTF.

        Returns:
            Notes richtext data as bytes, or None if not available
        """
        return self._read_data(NOTES_RICHTEXT_TYPE, "clip_read_richtext")

    def write_notes_richtext(self, data: bytes, rtf_data: bytes | None = None) -> None:
        """
        Write Apple Notes proprietary richtext format to clipboard.

        This format (com.apple.notes.richtext) preserves ALL Notes formatting
        including checklists, block quotes, and other Notes-specific elements.

        Args:
            data: Notes richtext content as bytes
            rtf_data: Optional RTF content to also write (Notes may need this to paste)

        Raises:
            ClipboardError: If writing to clipboard fails
        """
        self._pasteboard.clearContents()

        # Write Notes richtext
        ns_data = NSData.dataWithBytes_length_(data, len(data))
        result = self._pasteboard.setData_forType_(ns_data, NOTES_RICHTEXT_TYPE)
        if not result:
            raise ClipboardError("Failed to write Notes richtext data to clipboard")

        # Also write RTF if provided (Notes may need this fallback for pasting)
        if rtf_data is not None:
            ns_rtf_data = NSData.dataWithBytes_length_(rtf_data, len(rtf_data))
            self._pasteboard.setData_forType_(ns_rtf_data, NSPasteboardTypeRTF)

    def read_plain_text(self) -> Optional[str]:
        """
        Read plain text from clipboard.

        Returns:
            Plain text string, or None if no text available
        """
        return self._read_string(NSPasteboardTypeString, "clip_read_text")

    def write_plain_text(self, text: str) -> None:
        """
        Write plain text to clipboard.

        Args:
            text: Plain text to write

        Raises:
            ClipboardError: If writing to clipboard fails
        """
        self._pasteboard.clearContents()
        result = self._pasteboard.setString_forType_(text, NSPasteboardTypeString)
        if not result:
            raise ClipboardError("Failed to write text to clipboard")

    def clear(self) -> None:
        """Empty the clipboard, so a later read can only see a fresh copy."""
        self._pasteboard.clearContents()

    def text_to_rtf(self, text: str) -> bytes:
        """
        Convert plain text to RTF format.

        Uses NSAttributedString to create properly formatted RTF that matches
        Apple Notes default styling (18px system font).

        Args:
            text: Plain text content

        Returns:
            RTF-formatted bytes

        Raises:
            ClipboardError: If RTF conversion fails
        """
        # Create attributed string with Apple Notes default font
        font = NSFont.systemFontOfSize_(18.0)
        attr_string = NSAttributedString.alloc().initWithString_attributes_(
            text, {"NSFont": font}
        )

        # Convert to RTF
        rtf_data = attr_string.RTFFromRange_documentAttributes_(
            NSMakeRange(0, len(text)), None
        )

        if rtf_data is None:
            raise ClipboardError("Failed to convert text to RTF format")

        return bytes(rtf_data)

    def _snapshot_items(self) -> list[dict[str, bytes]]:
        """Every type of every pasteboard item, as bytes (issue #125).

        Reads run on one fresh thread like every other read (see
        ``_read_via_fresh_thread``), under SNAPSHOT_TIMEOUT for the whole
        snapshot. A snapshot that times out stops at its next type rather
        than reading the rest of the clipboard in the background, and while
        its thread is still blocked, later snapshots are skipped. File-promise
        types are never read (see _PROMISE_TYPES).

        Raises:
            ClipboardError: The snapshot took longer than SNAPSHOT_TIMEOUT.
            _SnapshotTooLarge: The clipboard holds more than SNAPSHOT_MAX_BYTES.
            _SnapshotStillRunning: An abandoned snapshot is still blocked.
        """
        if _snapshot_in_flight.is_set():
            raise _SnapshotStillRunning()
        abandoned = threading.Event()
        started = threading.Event()

        def read():
            started.set()
            try:
                items = []
                total = 0
                for item in self._pasteboard.pasteboardItems() or []:
                    saved = {}
                    for pb_type in item.types() or []:
                        if abandoned.is_set():
                            return []
                        pb_type = str(pb_type)
                        if pb_type in _PROMISE_TYPES or pb_type.startswith(
                            _PROMISE_TYPE_PREFIX
                        ):
                            continue
                        data = item.dataForType_(pb_type)
                        if data is None:
                            continue
                        # The cap is checked on the length, before the copy.
                        total += int(data.length())
                        if total > SNAPSHOT_MAX_BYTES:
                            raise _SnapshotTooLarge(total)
                        saved[pb_type] = bytes(data)
                    if saved:
                        items.append(saved)
                return items
            finally:
                _snapshot_in_flight.clear()

        _snapshot_in_flight.set()
        with profiling.span("clip_read_items"):
            try:
                return self._read_via_fresh_thread(read, timeout=SNAPSHOT_TIMEOUT)
            finally:
                abandoned.set()
                if not started.is_set():
                    # The thread never ran (or never will): nothing to wait for.
                    _snapshot_in_flight.clear()

    def save_state(self) -> dict:
        """
        Save current clipboard contents for later restoration.

        Saves RTF and plain text as before, plus a snapshot of every type on
        every pasteboard item (an image, Finder files, a browser's HTML), so
        a write no longer destroys them (issue #125, NTG-020). The snapshot is
        best effort: if it takes longer than SNAPSHOT_TIMEOUT or holds more
        than SNAPSHOT_MAX_BYTES, a warning is logged and only the text is
        restored, which is what every write did before. It never fails the
        write. The change count lets ``restore_state`` leave the clipboard
        alone when the write never touched it.

        Returns:
            Dictionary with saved clipboard state
        """
        state: dict = {"change_count": int(self._pasteboard.changeCount())}

        # Route through the fresh-thread readers: this runs before every
        # write against the USER's clipboard, which may hold promised types
        # from any app (issue #141 lost-wakeup race).
        rtf_data = self.read_rtf()
        if rtf_data is not None:
            state["rtf"] = rtf_data

        text = self.read_plain_text()
        if text is not None:
            state["text"] = text

        try:
            items = self._snapshot_items()
        except _SnapshotTooLarge as exc:
            reason = f"over {SNAPSHOT_MAX_BYTES // (1024 * 1024)} MB ({exc.args[0]} bytes)"
        except _SnapshotStillRunning:
            reason = "an earlier snapshot is still running"
        except ClipboardError:
            reason = f"took over {SNAPSHOT_TIMEOUT:.0f}s"
        except Exception as exc:  # best effort: never a new way for writes to fail
            # An unexpected error is a fault in this code, not the clipboard.
            unified_log.report_failure("clipboard_snapshot", exc=exc)
            reason = type(exc).__name__
        else:
            count = int(self._pasteboard.changeCount())
            if count == state["change_count"]:
                if items:
                    state["items"] = items
                return state
            # Something was copied mid-save: the text and the items may be from
            # different clipboards. Keep the newer count, so a write that never
            # touches the clipboard leaves that newer copy alone.
            state["change_count"] = count
            reason = "the clipboard changed while it was being saved"
        # Sizes and type names only: never clipboard contents.
        logger.warning(
            "Clipboard not saved in full before a write (%s); "
            "only its text will be restored",
            reason,
        )
        return state

    def restore_state(self, state: dict) -> None:
        """
        Restore previously saved clipboard contents.

        If the change count is the one save_state saw, the write never touched
        the clipboard and it is left exactly as it is. Otherwise every saved
        item is put back with all its types (issue #125), or only the RTF and
        text when the snapshot wasn't taken.

        Plain text is always written explicitly next to RTF. With RTF alone, a
        later plain-text read is answered on demand by this process, and hangs
        until the pasteboard timeout (90s here) if this process is alive but not
        serving the pasteboard. save_state reads plain text before every write,
        so the next write would fail (issue #248). With items, the first item
        (the one a plain-text read is answered from) gets that explicit text.

        Args:
            state: Dictionary from save_state()
        """
        saved_count = state.get("change_count")
        if saved_count is not None and int(self._pasteboard.changeCount()) == saved_count:
            return

        items = state.get("items")
        if items and self._restore_items(items, state.get("text")):
            return
        self._restore_text(state)

    def _restore_items(self, items: list[dict[str, bytes]], text: str | None) -> bool:
        """Write the saved items back; False if the pasteboard refused them."""
        pb_items = []
        for saved in items:
            item = NSPasteboardItem.alloc().init()
            for pb_type, data in saved.items():
                item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), pb_type)
            pb_items.append(item)

        first = items[0]
        if NSPasteboardTypeString not in first:
            if text is None and NSPasteboardTypeRTF in first:
                text = self._plain_text_of_rtf(first[NSPasteboardTypeRTF])
            if text is None and first.keys() & _TEXT_CONVERTIBLE_TYPES:
                # No text known, but AppKit would convert on demand (#248).
                # Never for an image alone: apps would paste "" over it.
                text = ""
            if text is not None:
                pb_items[0].setString_forType_(text, NSPasteboardTypeString)

        self._pasteboard.clearContents()
        if self._pasteboard.writeObjects_(pb_items):
            return True
        logger.warning("Clipboard items could not be restored; restoring its text only")
        return False

    def _restore_text(self, state: dict) -> None:
        """The RTF + plain-text restore (the whole restore before #125)."""
        self._pasteboard.clearContents()

        text = state.get("text")
        if "rtf" in state:
            rtf_data = state["rtf"]
            ns_data = NSData.dataWithBytes_length_(rtf_data, len(rtf_data))
            self._pasteboard.setData_forType_(ns_data, NSPasteboardTypeRTF)
            if text is None:
                text = self._plain_text_of_rtf(rtf_data)
        if text is not None:
            self._pasteboard.setString_forType_(text, NSPasteboardTypeString)

    def _plain_text_of_rtf(self, rtf_data: bytes) -> str:
        """The text an RTF blob holds; empty if it can't be parsed, which still
        leaves an explicit string on the pasteboard."""
        try:
            return str(self.rtf_to_mutable_attributed_string(rtf_data).string())
        except ClipboardError:
            return ""

    def rtf_to_mutable_attributed_string(
        self, rtf_data: bytes
    ) -> NSMutableAttributedString:
        """
        Convert RTF data to NSMutableAttributedString for editing.

        Args:
            rtf_data: RTF content as bytes

        Returns:
            Mutable attributed string that can be edited while preserving formatting

        Raises:
            ClipboardError: If conversion fails
        """
        ns_data = NSData.dataWithBytes_length_(rtf_data, len(rtf_data))
        result = NSMutableAttributedString.alloc().initWithRTF_documentAttributes_(
            ns_data, None
        )

        # Result is (attributed_string, document_attributes_dict)
        if result is None:
            raise ClipboardError("Failed to convert RTF to attributed string")
        attr_string = result[0]

        if attr_string is None:
            raise ClipboardError("Failed to convert RTF to attributed string")

        return attr_string

    def attributed_string_to_rtf(self, attr_string: NSMutableAttributedString) -> bytes:
        """
        Convert NSMutableAttributedString back to RTF data.

        Args:
            attr_string: The attributed string to convert

        Returns:
            RTF data as bytes

        Raises:
            ClipboardError: If conversion fails
        """
        length = attr_string.length()
        rtf_data = attr_string.RTFFromRange_documentAttributes_(
            NSMakeRange(0, length), None
        )

        if rtf_data is None:
            raise ClipboardError("Failed to convert attributed string to RTF")

        return bytes(rtf_data)

    def find_replace_in_rtf(
        self, rtf_data: bytes, search_text: str, replace_text: str
    ) -> tuple[bytes, int]:
        """
        Perform find/replace on RTF content while preserving all formatting.

        This method surgically replaces text occurrences while keeping the
        original formatting attributes (fonts, colors, links, etc.) intact
        for the surrounding content.

        NOTE: The replacement text will inherit formatting from the character
        immediately before the match (or default formatting if at start).

        Args:
            rtf_data: Original RTF content as bytes
            search_text: Text to find
            replace_text: Text to replace with

        Returns:
            Tuple of (modified RTF data, number of replacements made)

        Raises:
            ClipboardError: If RTF operations fail
        """
        if not search_text:
            raise ClipboardError("Search text cannot be empty")

        # Convert to mutable attributed string
        attr_string = self.rtf_to_mutable_attributed_string(rtf_data)
        full_text = str(attr_string.string())

        # Count and perform replacements (from end to start to maintain positions)
        ns_search = NSString.stringWithString_(search_text)
        ns_replace = NSString.stringWithString_(replace_text)
        replacements = 0

        # Find all occurrences first
        positions = []
        start = 0
        while True:
            pos = full_text.find(search_text, start)
            if pos == -1:
                break
            positions.append(pos)
            start = pos + len(search_text)

        # Replace from end to start (maintains positions for earlier matches)
        for pos in reversed(positions):
            range_to_replace = NSMakeRange(pos, len(search_text))
            attr_string.replaceCharactersInRange_withString_(
                range_to_replace, ns_replace
            )
            replacements += 1

        # Convert back to RTF
        result_rtf = self.attributed_string_to_rtf(attr_string)

        return result_rtf, replacements

    def find_replace_in_raw_rtf(
        self,
        rtf_data: bytes,
        search_text: str,
        replace_text: str,
        body_only: bool = False,
    ) -> tuple[bytes, int]:
        """
        Perform find/replace directly on RTF bytes, preserving ALL formatting.

        This method does surgical text replacement without converting to
        NSAttributedString, which would strip Apple-proprietary attributes
        like AppleTypeServices, AppleSystemUIFont, and paragraph styles.

        Args:
            rtf_data: Original RTF content as bytes
            search_text: Text to find (must be simple ASCII, no RTF special chars)
            replace_text: Text to replace with
            body_only: If True, skip matches in the first paragraph (the note's
                title line in copied Notes content). If the RTF has no paragraph
                break at all there is no title to protect, so the whole document
                is searched.

        Returns:
            Tuple of (modified RTF bytes, number of replacements made)

        Raises:
            ClipboardError: If search_text contains RTF special characters
        """
        # Validate search text doesn't contain RTF special chars
        for char in ["\\", "{", "}"]:
            if char in search_text:
                raise ClipboardError(
                    f"Search text cannot contain RTF special character: {char}"
                )

        if not search_text:
            raise ClipboardError("Search text cannot be empty")

        rtf_str = rtf_data.decode("utf-8")

        # Find all occurrences in text context
        positions = []
        start = 0
        if body_only:
            start = rtf_body_start_offset(rtf_str)
        while True:
            pos = rtf_str.find(search_text, start)
            if pos == -1:
                break
            # Verify this is in text context (not a control word)
            if self._is_rtf_text_context(rtf_str, pos):
                positions.append(pos)
            start = pos + len(search_text)

        # Replace from end to start to maintain positions
        replacements = 0
        for pos in reversed(positions):
            rtf_str = rtf_str[:pos] + replace_text + rtf_str[pos + len(search_text) :]
            replacements += 1

        return rtf_str.encode("utf-8"), replacements

    def _is_rtf_text_context(self, rtf_str: str, pos: int) -> bool:
        """
        Check if position is in text content, not a control sequence.

        Control sequences start with backslash:
        - \\word (control word)
        - \\'XX (hex escape)
        - \\uXXXX (unicode)

        Returns True if the position is safe to replace (text content).
        """
        # Look backwards for the nearest backslash or brace
        i = pos - 1
        letters_only = True

        while i >= 0:
            char = rtf_str[i]

            if char == "\\":
                # Found a backslash - check if we're part of a control word
                # Control word = \ followed by only letters up to our position
                if letters_only:
                    # We're part of a control word like \someword
                    return False
                else:
                    # There was a non-letter between \ and us, we're text
                    return True

            if char in "{}":
                # Hit a brace, we're definitely in text context
                return True

            if not char.isalpha():
                # Non-letter breaks potential control word chain
                letters_only = False

            i -= 1

        # Reached start of string without finding backslash, we're text
        return True

    def find_replace_in_notes_richtext(
        self, data: bytes, search_text: str, replace_text: str
    ) -> tuple[bytes, int]:
        """
        Perform find/replace on Apple Notes richtext data, preserving ALL formatting.

        This method operates directly on the com.apple.notes.richtext binary format,
        which is an NSKeyedArchive containing Notes' native content representation.
        Unlike RTF-based approaches, this preserves ALL Notes formatting including:
        - Checklists (checked and unchecked states)
        - Block quotes
        - Internal note links
        - External hyperlinks
        - All text styling (bold, italic, colors, etc.)
        - Tables
        - Code blocks

        IMPORTANT: This method currently only supports same-length replacements
        (where len(search_text) == len(replace_text)). For different-length
        replacements, the internal length fields in the protobuf structure
        would need to be updated.

        Args:
            data: Apple Notes richtext data as bytes (from read_notes_richtext)
            search_text: Text to find
            replace_text: Text to replace with (must be same length as search_text)

        Returns:
            Tuple of (modified data bytes, number of replacements made)

        Raises:
            ClipboardError: If search/replace text lengths don't match,
                           or if search_text is empty
        """
        if not search_text:
            raise ClipboardError("Search text cannot be empty")

        search_bytes = search_text.encode("utf-8")
        replace_bytes = replace_text.encode("utf-8")

        if len(search_bytes) != len(replace_bytes):
            raise ClipboardError(
                f"Search and replace text must have same byte length for Notes richtext format. "
                f"Search: {len(search_bytes)} bytes, Replace: {len(replace_bytes)} bytes. "
                f"Use RTF-based find/replace for different-length replacements."
            )

        # Convert to mutable list for in-place replacement
        data_bytes = bytearray(data)
        replacements = 0

        # Find and replace all occurrences
        pos = 0
        while pos <= len(data_bytes) - len(search_bytes):
            # Check for match
            if data_bytes[pos : pos + len(search_bytes)] == search_bytes:
                # Replace in-place
                data_bytes[pos : pos + len(search_bytes)] = replace_bytes
                replacements += 1
                pos += len(search_bytes)  # Move past replacement
            else:
                pos += 1

        return bytes(data_bytes), replacements

    def find_replace_in_notes_richtext_variable(
        self,
        data: bytes,
        search_text: str,
        replace_text: str,
        replace_all: bool = True,
        links=None,
        body_only: bool = True,
    ) -> tuple[bytes, int, list]:
        """
        Perform find/replace on Apple Notes richtext data, supporting variable-length
        replacements and markdown formatting in the replacement text.

        The replacement text is parsed as markdown. If it contains formatting
        (bold, italic, etc.), the formatted runs are spliced into the note's
        richtext. Plain text replacements inherit the existing formatting.

        All existing formatting is preserved including:
        - Checklists (checked and unchecked states)
        - Block quotes
        - Internal note links
        - External hyperlinks
        - All text styling (bold, italic, colors, etc.)
        - Tables
        - Code blocks

        Args:
            data: Apple Notes richtext data as bytes (from read_notes_richtext)
            search_text: Text to find
            replace_text: Replacement text (supports markdown formatting)
            replace_all: If True, replace all occurrences; otherwise, just the first
            links: Optional mapping of lower-case note title -> LinkTarget;
                each [[Name]] in it is written as a synthesized link attachment
                (issue #212). Other [[names]] stay plain text.
            body_only: If True (default), the note's first line — its title in
                Apple Notes — is excluded from both the occurrence count and the
                replacement, matching the server's body-only pre-check. Pass
                False only when the title should be rewritten too.

        Returns:
            Tuple of (modified data bytes, number of replacements made,
            list of PendingKeyboardAction for hashtags and tables — never links)

        Raises:
            ClipboardError: If parsing or serialization fails, or if search_text is empty
        """
        from .richtext import (
            NotesRichtextParser,
            NotesRichtextSerializer,
            PendingKeyboardAction,
            build_richtext,
            find_replace,
            parse_markdown,
        )
        from .richtext.parser import NotesRichtextParseError
        from .richtext.section_fragment import (
            link_resolver_for,
            synthesize_pending_links,
        )
        from .richtext.serializer import NotesRichtextSerializeError

        if not search_text:
            raise ClipboardError("Search text cannot be empty")

        try:
            # Parse the richtext data
            parser = NotesRichtextParser()
            richtext = parser.parse(data)

            # Copied note content starts with the title line; scope the search
            # to the body so a find_replace can never rewrite the title.
            search_start = body_start_offset(richtext.text) if body_only else 0

            # Count occurrences (body only unless the caller opted out)
            count = richtext.text.count(search_text, search_start)
            if count == 0:
                return data, 0, []

            # Parse replacement text as markdown to get formatted runs
            doc = parse_markdown(replace_text)
            link_resolver = link_resolver_for(links) if links else None
            replacement_richtext, pending_actions = build_richtext(
                doc, richtext, link_resolver=link_resolver
            )

            # build_richtext emits one pending keyboard action per placeholder,
            # so a replacement carrying native elements (links, tags, tables)
            # can only be spliced into a single occurrence — later copies would
            # keep their literal {__NG_…__} placeholder text in the note.
            if pending_actions:
                replace_all = False

            # Perform replacement with formatted runs
            modified = find_replace(
                richtext,
                search_text,
                replacement_richtext.text,
                replace_all=replace_all,
                replacement_runs=replacement_richtext.runs,
                search_start=search_start,
            )
            # Links become real attachments here, not Cmd+K actions (#212).
            modified, pending_actions = synthesize_pending_links(
                modified, pending_actions, links or {}
            )

            # Serialize back to binary
            serializer = NotesRichtextSerializer()
            result_data = serializer.serialize(modified)

            replacements = count if replace_all else min(1, count)
            return result_data, replacements, pending_actions

        except (NotesRichtextParseError, NotesRichtextSerializeError, ValueError) as e:
            raise ClipboardError(
                f"Failed to perform variable-length replacement: {e}"
            ) from e
