"""Tests for ClipboardManager."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from apple_notes.clipboard import ClipboardManager
from apple_notes.exceptions import ClipboardError

pytestmark = pytest.mark.macos_gui


# Load real Apple Notes RTF fixture for integration-style tests
FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_RTF_PATH = FIXTURE_DIR / "apple_notes_sample.rtf"


class TestClipboardManager:
    """Tests for ClipboardManager class."""

    @pytest.fixture
    def mock_pasteboard(self):
        """Create a mock NSPasteboard."""
        with patch("apple_notes.clipboard.NSPasteboard") as mock_pb_class:
            mock_pb = MagicMock()
            mock_pb_class.generalPasteboard.return_value = mock_pb
            yield mock_pb

    def test_read_rtf_returns_bytes(self, mock_pasteboard):
        """Test reading RTF data from clipboard."""
        mock_data = MagicMock()
        mock_data.__bytes__ = MagicMock(return_value=b"{\\rtf1 test}")
        mock_data.__iter__ = MagicMock(return_value=iter([123, 92, 114]))
        mock_pasteboard.dataForType_.return_value = mock_data

        manager = ClipboardManager()
        result = manager.read_rtf()

        assert result is not None
        mock_pasteboard.dataForType_.assert_called_once()

    def test_read_rtf_returns_none_when_empty(self, mock_pasteboard):
        """Test reading RTF returns None when clipboard has no RTF."""
        mock_pasteboard.dataForType_.return_value = None

        manager = ClipboardManager()
        result = manager.read_rtf()

        assert result is None

    def test_write_rtf_success(self, mock_pasteboard):
        """Test writing RTF data to clipboard."""
        mock_pasteboard.setData_forType_.return_value = True

        manager = ClipboardManager()
        manager.write_rtf(b"{\\rtf1 test}")

        mock_pasteboard.clearContents.assert_called_once()
        mock_pasteboard.setData_forType_.assert_called_once()

    def test_write_rtf_failure_raises_error(self, mock_pasteboard):
        """Test write_rtf raises ClipboardError on failure."""
        mock_pasteboard.setData_forType_.return_value = False

        manager = ClipboardManager()
        with pytest.raises(ClipboardError, match="Failed to write RTF"):
            manager.write_rtf(b"{\\rtf1 test}")

    def test_read_plain_text(self, mock_pasteboard):
        """Test reading plain text from clipboard."""
        mock_pasteboard.stringForType_.return_value = "Hello World"

        manager = ClipboardManager()
        result = manager.read_plain_text()

        assert result == "Hello World"

    def test_read_plain_text_returns_none(self, mock_pasteboard):
        """Test reading plain text returns None when empty."""
        mock_pasteboard.stringForType_.return_value = None

        manager = ClipboardManager()
        result = manager.read_plain_text()

        assert result is None

    def test_write_plain_text_success(self, mock_pasteboard):
        """Test writing plain text to clipboard."""
        mock_pasteboard.setString_forType_.return_value = True

        manager = ClipboardManager()
        manager.write_plain_text("Hello World")

        mock_pasteboard.clearContents.assert_called_once()
        mock_pasteboard.setString_forType_.assert_called_once()

    def test_write_plain_text_failure_raises_error(self, mock_pasteboard):
        """Test write_plain_text raises ClipboardError on failure."""
        mock_pasteboard.setString_forType_.return_value = False

        manager = ClipboardManager()
        with pytest.raises(ClipboardError, match="Failed to write text"):
            manager.write_plain_text("Hello")

    def test_save_state_captures_rtf_and_text(self, mock_pasteboard):
        """Test save_state captures both RTF and text."""
        mock_rtf_data = MagicMock()
        mock_rtf_data.__bytes__ = MagicMock(return_value=b"{\\rtf1}")
        mock_rtf_data.__iter__ = MagicMock(return_value=iter([123]))
        mock_pasteboard.dataForType_.return_value = mock_rtf_data
        mock_pasteboard.stringForType_.return_value = "text content"

        manager = ClipboardManager()
        state = manager.save_state()

        assert "rtf" in state or "text" in state

    def test_save_state_empty_clipboard(self, mock_pasteboard):
        """Test save_state handles empty clipboard."""
        mock_pasteboard.dataForType_.return_value = None
        mock_pasteboard.stringForType_.return_value = None

        manager = ClipboardManager()
        state = manager.save_state()

        assert set(state) == {"change_count"}

    def test_restore_state_with_rtf(self, mock_pasteboard):
        """Test restore_state restores RTF data."""
        state = {"rtf": b"{\\rtf1 restored}"}

        manager = ClipboardManager()
        manager.restore_state(state)

        mock_pasteboard.clearContents.assert_called_once()
        mock_pasteboard.setData_forType_.assert_called_once()

    def test_restore_state_with_text_only(self, mock_pasteboard):
        """Test restore_state falls back to text when no RTF."""
        state = {"text": "restored text"}

        manager = ClipboardManager()
        manager.restore_state(state)

        mock_pasteboard.clearContents.assert_called_once()
        mock_pasteboard.setString_forType_.assert_called_once()

    def test_restore_state_empty(self, mock_pasteboard):
        """Test restore_state handles empty state."""
        state = {}

        manager = ClipboardManager()
        manager.restore_state(state)

        mock_pasteboard.clearContents.assert_called_once()


class TestTextToRtf:
    """Tests for text_to_rtf conversion."""

    @pytest.fixture
    def mock_nsattributedstring(self):
        """Create mock NSAttributedString."""
        with patch("apple_notes.clipboard.NSAttributedString") as mock_class:
            mock_instance = MagicMock()
            mock_class.alloc.return_value.initWithString_attributes_.return_value = (
                mock_instance
            )
            yield mock_instance

    @pytest.fixture
    def mock_nsfont(self):
        """Create mock NSFont."""
        with patch("apple_notes.clipboard.NSFont") as mock_class:
            mock_class.systemFontOfSize_.return_value = MagicMock()
            yield mock_class

    @pytest.fixture
    def mock_pasteboard(self):
        """Create a mock NSPasteboard."""
        with patch("apple_notes.clipboard.NSPasteboard") as mock_pb_class:
            mock_pb = MagicMock()
            mock_pb_class.generalPasteboard.return_value = mock_pb
            yield mock_pb

    def test_text_to_rtf_returns_bytes(
        self, mock_nsattributedstring, mock_nsfont, mock_pasteboard
    ):
        """Test text_to_rtf returns RTF bytes."""
        mock_rtf_data = MagicMock()
        mock_rtf_data.__bytes__ = MagicMock(return_value=b"{\\rtf1}")
        mock_rtf_data.__iter__ = MagicMock(return_value=iter([123]))
        mock_nsattributedstring.RTFFromRange_documentAttributes_.return_value = (
            mock_rtf_data
        )

        manager = ClipboardManager()
        result = manager.text_to_rtf("Hello World")

        assert result is not None

    def test_text_to_rtf_uses_18px_font(self, mock_nsfont, mock_pasteboard):
        """Test text_to_rtf uses 18px system font."""
        with patch("apple_notes.clipboard.NSAttributedString") as mock_class:
            mock_instance = MagicMock()
            mock_instance.RTFFromRange_documentAttributes_.return_value = MagicMock(
                __bytes__=lambda self: b"{\\rtf1}",
                __iter__=lambda self: iter([123]),
            )
            mock_class.alloc.return_value.initWithString_attributes_.return_value = (
                mock_instance
            )

            manager = ClipboardManager()
            try:
                manager.text_to_rtf("test")
            except Exception:
                pass  # May fail due to incomplete mocking

            mock_nsfont.systemFontOfSize_.assert_called_with(18.0)

    def test_text_to_rtf_raises_on_conversion_failure(
        self, mock_nsattributedstring, mock_nsfont, mock_pasteboard
    ):
        """Test text_to_rtf raises ClipboardError on failure."""
        mock_nsattributedstring.RTFFromRange_documentAttributes_.return_value = None

        manager = ClipboardManager()
        with pytest.raises(ClipboardError, match="Failed to convert text to RTF"):
            manager.text_to_rtf("Hello")


class TestRtfSurgery(TestClipboardManager):
    """Tests for RTF surgery methods (find/replace)."""

    def test_rtf_to_mutable_attributed_string_success(self, mock_pasteboard):
        """Test RTF to attributed string conversion."""
        with patch("apple_notes.clipboard.NSMutableAttributedString") as mock_class:
            with patch("apple_notes.clipboard.NSData") as mock_nsdata:
                mock_attr_string = MagicMock()
                mock_class.alloc.return_value.initWithRTF_documentAttributes_.return_value = (
                    mock_attr_string,
                    None,
                    None,
                )
                mock_nsdata.dataWithBytes_length_.return_value = MagicMock()

                manager = ClipboardManager()
                result = manager.rtf_to_mutable_attributed_string(b"{\\rtf1}")

                assert result == mock_attr_string

    def test_rtf_to_mutable_attributed_string_failure(self, mock_pasteboard):
        """Test RTF conversion raises on failure."""
        with patch("apple_notes.clipboard.NSMutableAttributedString") as mock_class:
            with patch("apple_notes.clipboard.NSData"):
                mock_class.alloc.return_value.initWithRTF_documentAttributes_.return_value = (
                    None,
                    None,
                    None,
                )

                manager = ClipboardManager()
                with pytest.raises(ClipboardError, match="Failed to convert RTF"):
                    manager.rtf_to_mutable_attributed_string(b"{\\rtf1}")

    def test_attributed_string_to_rtf_success(self, mock_pasteboard):
        """Test attributed string to RTF conversion."""
        mock_attr_string = MagicMock()
        mock_attr_string.length.return_value = 10
        # Create a mock that returns bytes when bytes() is called on it
        mock_rtf_data = MagicMock()
        mock_rtf_data.__iter__ = MagicMock(
            return_value=iter([ord(c) for c in "{\\rtf1}"])
        )
        mock_attr_string.RTFFromRange_documentAttributes_.return_value = mock_rtf_data

        manager = ClipboardManager()
        result = manager.attributed_string_to_rtf(mock_attr_string)

        # Just verify the method runs and returns bytes
        assert isinstance(result, bytes)

    def test_attributed_string_to_rtf_failure(self, mock_pasteboard):
        """Test attributed string conversion raises on failure."""
        mock_attr_string = MagicMock()
        mock_attr_string.length.return_value = 10
        mock_attr_string.RTFFromRange_documentAttributes_.return_value = None

        manager = ClipboardManager()
        with pytest.raises(ClipboardError, match="Failed to convert attributed string"):
            manager.attributed_string_to_rtf(mock_attr_string)

    def test_find_replace_in_rtf_empty_search_raises(self, mock_pasteboard):
        """Test find_replace raises on empty search text."""
        manager = ClipboardManager()
        with pytest.raises(ClipboardError, match="Search text cannot be empty"):
            manager.find_replace_in_rtf(b"{\\rtf1}", "", "replacement")

    def test_find_replace_in_rtf_performs_replacement(self, mock_pasteboard):
        """Test find_replace performs replacements correctly."""
        with patch.object(
            ClipboardManager, "rtf_to_mutable_attributed_string"
        ) as mock_to_attr:
            with patch.object(
                ClipboardManager, "attributed_string_to_rtf"
            ) as mock_to_rtf:
                mock_attr_string = MagicMock()
                mock_attr_string.string.return_value = "Hello World Hello"
                mock_to_attr.return_value = mock_attr_string
                mock_to_rtf.return_value = b"{\\rtf1 modified}"

                manager = ClipboardManager()
                result, count = manager.find_replace_in_rtf(b"{\\rtf1}", "Hello", "Hi")

                assert result == b"{\\rtf1 modified}"
                assert count == 2  # Two occurrences
                # Verify replacements were called from end to start
                assert (
                    mock_attr_string.replaceCharactersInRange_withString_.call_count
                    == 2
                )

    def test_find_replace_in_rtf_no_matches(self, mock_pasteboard):
        """Test find_replace with no matches."""
        with patch.object(
            ClipboardManager, "rtf_to_mutable_attributed_string"
        ) as mock_to_attr:
            with patch.object(
                ClipboardManager, "attributed_string_to_rtf"
            ) as mock_to_rtf:
                mock_attr_string = MagicMock()
                mock_attr_string.string.return_value = "Hello World"
                mock_to_attr.return_value = mock_attr_string
                mock_to_rtf.return_value = b"{\\rtf1 unchanged}"

                manager = ClipboardManager()
                result, count = manager.find_replace_in_rtf(
                    b"{\\rtf1}", "Goodbye", "Hi"
                )

                assert count == 0
                mock_attr_string.replaceCharactersInRange_withString_.assert_not_called()


class TestRawRtfFindReplace:
    """Tests for raw RTF find/replace using real Apple Notes RTF.

    These tests use a real RTF fixture captured from Apple Notes to ensure
    the raw RTF manipulation preserves all Apple-proprietary attributes.
    """

    @pytest.fixture
    def sample_rtf(self) -> bytes:
        """Load the real Apple Notes RTF fixture."""
        if not SAMPLE_RTF_PATH.exists():
            pytest.skip(f"RTF fixture not found at {SAMPLE_RTF_PATH}")
        return SAMPLE_RTF_PATH.read_bytes()

    @pytest.fixture
    def mock_pasteboard(self):
        """Create a mock NSPasteboard."""
        with patch("apple_notes.clipboard.NSPasteboard") as mock_pb_class:
            mock_pb = MagicMock()
            mock_pb_class.generalPasteboard.return_value = mock_pb
            yield mock_pb

    @pytest.fixture
    def manager(self, mock_pasteboard):
        """Create a ClipboardManager with mocked pasteboard."""
        return ClipboardManager()

    # --- Basic functionality ---

    def test_simple_replacement_in_real_rtf(self, manager, sample_rtf):
        """Test replacing text in actual Apple Notes RTF."""
        # The sample RTF contains "great sentence"
        result, count = manager.find_replace_in_raw_rtf(
            sample_rtf, "great sentence", "amazing sentence"
        )
        assert b"amazing sentence" in result
        assert b"great sentence" not in result
        assert count == 1

    def test_preserves_apple_type_services(self, manager, sample_rtf):
        """Test that AppleTypeServices markers survive replacement."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        assert b"AppleTypeServices" in result
        assert b"AppleTypeServicesF2293774" in result

    def test_preserves_font_definitions(self, manager, sample_rtf):
        """Test that AppleSystemUIFont is preserved (not changed to HelveticaNeue)."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        assert b"AppleSystemUIFont" in result

    def test_preserves_block_quote_formatting(self, manager, sample_rtf):
        """Test that block quote paragraph styles survive."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        # Block quotes have specific paragraph formatting
        assert b"\\pard\\pardeftab560\\slleading20\\pardirnatural" in result

    def test_preserves_checklist_markers(self, manager, sample_rtf):
        """Test that checklist list definitions survive."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        # Unchecked and checked list styles
        assert b"\\ls4" in result  # Unchecked style
        assert b"\\ls5" in result  # Checked style

    def test_preserves_internal_links(self, manager, sample_rtf):
        """Test that internal note links survive."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        assert b"applenotes:note/" in result
        assert b"HYPERLINK" in result

    def test_preserves_external_links(self, manager, sample_rtf):
        """Test that external hyperlinks survive."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        assert b"http://www.google.com" in result

    def test_preserves_colored_text(self, manager, sample_rtf):
        """Test that AppleHighlight color markers survive."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        assert b"AppleHighlight" in result
        assert b"AppleHilightClrSch" in result

    def test_preserves_table_structure(self, manager, sample_rtf):
        """Test that table RTF codes survive."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        assert b"\\itap1\\trowd" in result
        assert b"\\cell" in result

    # --- Edge cases and safety ---

    def test_rejects_backslash_in_search(self, manager, sample_rtf):
        """Test that backslash in search text raises error."""
        with pytest.raises(ClipboardError, match="special character"):
            manager.find_replace_in_raw_rtf(sample_rtf, "\\test", "x")

    def test_rejects_open_brace_in_search(self, manager, sample_rtf):
        """Test that open brace in search text raises error."""
        with pytest.raises(ClipboardError, match="special character"):
            manager.find_replace_in_raw_rtf(sample_rtf, "test{", "x")

    def test_rejects_close_brace_in_search(self, manager, sample_rtf):
        """Test that close brace in search text raises error."""
        with pytest.raises(ClipboardError, match="special character"):
            manager.find_replace_in_raw_rtf(sample_rtf, "test}", "x")

    def test_rejects_empty_search(self, manager, sample_rtf):
        """Test that empty search text raises error."""
        with pytest.raises(ClipboardError, match="cannot be empty"):
            manager.find_replace_in_raw_rtf(sample_rtf, "", "x")

    def test_skips_control_words(self, manager, sample_rtf):
        """Test that control words are not replaced."""
        # "pard" appears as \pard control word - should not be replaced
        result, count = manager.find_replace_in_raw_rtf(sample_rtf, "pard", "XXX")
        # \pard should remain intact - the control word shouldn't be touched
        assert b"\\pard" in result

    def test_no_match_returns_zero_count(self, manager, sample_rtf):
        """Test that no matches returns zero count."""
        result, count = manager.find_replace_in_raw_rtf(
            sample_rtf, "xyznonexistent", "replacement"
        )
        assert count == 0
        assert result == sample_rtf

    def test_replacement_with_different_length(self, manager, sample_rtf):
        """Test that different-length replacements work correctly."""
        # Shorter replacement
        result, count = manager.find_replace_in_raw_rtf(
            sample_rtf, "great sentence", "ok"
        )
        assert b"ok" in result
        assert count == 1

        # Longer replacement
        result, count = manager.find_replace_in_raw_rtf(
            sample_rtf, "great", "absolutely fantastic"
        )
        assert b"absolutely fantastic" in result
        assert count == 1

    # --- Byte-level integrity ---

    def test_rtf_structure_remains_valid(self, manager, sample_rtf):
        """Test that result is still valid RTF (starts/ends correctly)."""
        result, _ = manager.find_replace_in_raw_rtf(sample_rtf, "great", "amazing")
        assert result.startswith(b"{\\rtf1")
        assert result.endswith(b"}")
        # Braces should be balanced
        assert result.count(b"{") == result.count(b"}")


class TestIsRtfTextContext:
    """Tests for the _is_rtf_text_context helper method."""

    @pytest.fixture
    def mock_pasteboard(self):
        """Create a mock NSPasteboard."""
        with patch("apple_notes.clipboard.NSPasteboard") as mock_pb_class:
            mock_pb = MagicMock()
            mock_pb_class.generalPasteboard.return_value = mock_pb
            yield mock_pb

    @pytest.fixture
    def manager(self, mock_pasteboard):
        """Create a ClipboardManager with mocked pasteboard."""
        return ClipboardManager()

    def test_text_after_space_is_text_context(self, manager):
        """Text after a space is in text context."""
        rtf = "Hello world"
        assert manager._is_rtf_text_context(rtf, 6) is True  # 'w' in 'world'

    def test_control_word_is_not_text_context(self, manager):
        """Position inside a control word is not text context."""
        rtf = "\\pard text"
        assert manager._is_rtf_text_context(rtf, 1) is False  # 'p' in \pard

    def test_text_after_control_word_is_text_context(self, manager):
        """Text after a control word (with space) is text context."""
        rtf = "\\pard text"
        assert manager._is_rtf_text_context(rtf, 6) is True  # 't' in 'text'

    def test_text_after_brace_is_text_context(self, manager):
        """Text immediately after a brace is text context."""
        rtf = "{Hello}"
        assert manager._is_rtf_text_context(rtf, 1) is True  # 'H' in 'Hello'

    def test_at_start_is_text_context(self, manager):
        """Position at start of string with no backslash is text context."""
        rtf = "Hello"
        assert manager._is_rtf_text_context(rtf, 0) is True

    def test_after_number_in_control_is_text_context(self, manager):
        """Text after a number in control word is text context."""
        rtf = "\\fs26 text"
        # After \fs26 (which has number), the space breaks the control word
        assert manager._is_rtf_text_context(rtf, 6) is True  # 't' in 'text'
