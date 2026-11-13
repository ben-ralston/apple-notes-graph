"""Tests for the AppleScript executor module."""

from unittest.mock import patch, MagicMock
import subprocess

import pytest

from apple_notes.applescript import AppleScriptExecutor
from apple_notes.exceptions import (
    AppleScriptError,
    NoteNotFoundError,
    FolderNotFoundError,
)

pytestmark = pytest.mark.macos_gui


class TestAppleScriptExecutor:
    """Tests for the AppleScriptExecutor class."""

    def test_escape_applescript_string_basic(self):
        """Should return basic strings unchanged."""
        executor = AppleScriptExecutor()
        assert executor._escape_applescript_string("hello") == "hello"

    def test_escape_applescript_string_quotes(self):
        """Should escape double quotes."""
        executor = AppleScriptExecutor()
        result = executor._escape_applescript_string('say "hello"')
        assert result == 'say \\"hello\\"'

    def test_escape_applescript_string_backslashes(self):
        """Should escape backslashes."""
        executor = AppleScriptExecutor()
        result = executor._escape_applescript_string("path\\to\\file")
        assert result == "path\\\\to\\\\file"

    def test_escape_applescript_string_both(self):
        """Should escape both backslashes and quotes."""
        executor = AppleScriptExecutor()
        result = executor._escape_applescript_string('say \\"hello\\"')
        assert result == 'say \\\\\\"hello\\\\\\"'

    @patch("subprocess.run")
    def test_execute_success(self, mock_run):
        """Should execute AppleScript and return success with output."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="success output\n", stderr=""
        )

        executor = AppleScriptExecutor()
        success, output = executor.execute('tell application "Notes" to return 1')

        assert success is True
        assert output == "success output"
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_execute_failure(self, mock_run):
        """Should return failure with error message on non-zero exit."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="error: syntax error"
        )

        executor = AppleScriptExecutor()
        success, output = executor.execute("invalid script")

        assert success is False
        assert "syntax error" in output

    @patch("subprocess.run")
    def test_execute_timeout(self, mock_run):
        """Should handle timeout gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired("osascript", 30)

        executor = AppleScriptExecutor()
        success, output = executor.execute("long running script")

        assert success is False
        assert "timed out" in output.lower()

    @patch("subprocess.run")
    def test_execute_os_error(self, mock_run):
        """Should handle OSError gracefully."""
        mock_run.side_effect = OSError("osascript not found")

        executor = AppleScriptExecutor()
        success, output = executor.execute("any script")

        assert success is False
        assert "osascript" in output.lower()


class TestExecuteTimeout:
    """Tests for the execute() timeout budget and its classifier (issue #141)."""

    @patch("subprocess.run")
    def test_default_timeout_is_30(self, mock_run):
        """Should pass the generous default budget when none is given."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        executor = AppleScriptExecutor()
        executor.execute("return 1")

        assert mock_run.call_args.kwargs["timeout"] == 30.0

    @patch("subprocess.run")
    def test_custom_timeout_forwarded(self, mock_run):
        """Should forward a caller-supplied budget to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        executor = AppleScriptExecutor()
        executor.execute("return 1", timeout=10.0)

        assert mock_run.call_args.kwargs["timeout"] == 10.0

    @patch("subprocess.run")
    def test_timeout_message_names_the_budget(self, mock_run):
        """Should report the budget that was exceeded, without raising."""
        mock_run.side_effect = subprocess.TimeoutExpired("osascript", 10)

        executor = AppleScriptExecutor()
        success, output = executor.execute("return 1", timeout=10.0)

        assert success is False
        assert output == "AppleScript execution timed out after 10s"

    def test_is_timeout_output_classifier(self):
        """Should classify timeout outputs and nothing else."""
        from apple_notes.applescript import is_timeout_output

        assert is_timeout_output("AppleScript execution timed out after 10s")
        assert is_timeout_output("AppleScript execution timed out after 30s")
        assert not is_timeout_output("Notes got an error: Can't get note")
        assert not is_timeout_output("")


class TestFolderExists:
    """Tests for folder_exists method."""

    @patch.object(AppleScriptExecutor, "execute")
    def test_folder_exists_true(self, mock_execute):
        """Should return True when folder exists."""
        mock_execute.return_value = (True, "true")

        executor = AppleScriptExecutor()
        result = executor.folder_exists("Work")

        assert result is True

    @patch.object(AppleScriptExecutor, "execute")
    def test_folder_exists_false(self, mock_execute):
        """Should return False when folder doesn't exist."""
        mock_execute.return_value = (True, "false")

        executor = AppleScriptExecutor()
        result = executor.folder_exists("NonexistentFolder")

        assert result is False

    @patch.object(AppleScriptExecutor, "execute")
    def test_folder_exists_script_error(self, mock_execute):
        """Should return False when script execution fails."""
        mock_execute.return_value = (False, "error")

        executor = AppleScriptExecutor()
        result = executor.folder_exists("AnyFolder")

        assert result is False


class TestCreateNote:
    """Tests for create_note method."""

    @patch.object(AppleScriptExecutor, "folder_exists")
    @patch.object(AppleScriptExecutor, "execute")
    def test_create_note_success(self, mock_execute, mock_folder_exists):
        """Should create note and return title on success."""
        mock_folder_exists.return_value = True
        mock_execute.return_value = (True, "My Note")

        executor = AppleScriptExecutor()
        result = executor.create_note("My Note", "Note body", "Notes")

        assert result == "My Note"
        mock_folder_exists.assert_called_once_with("Notes")

    @patch.object(AppleScriptExecutor, "folder_exists")
    def test_create_note_folder_not_found(self, mock_folder_exists):
        """Should raise FolderNotFoundError when folder doesn't exist."""
        mock_folder_exists.return_value = False

        executor = AppleScriptExecutor()

        with pytest.raises(FolderNotFoundError) as exc_info:
            executor.create_note("My Note", "Body", "NonexistentFolder")

        assert "NonexistentFolder" in str(exc_info.value)

    @patch.object(AppleScriptExecutor, "folder_exists")
    @patch.object(AppleScriptExecutor, "execute")
    def test_create_note_script_failure(self, mock_execute, mock_folder_exists):
        """Should raise AppleScriptError when creation fails."""
        mock_folder_exists.return_value = True
        mock_execute.return_value = (False, "Failed to create")

        executor = AppleScriptExecutor()

        with pytest.raises(AppleScriptError) as exc_info:
            executor.create_note("My Note", "Body", "Notes")

        assert "Failed to create" in str(exc_info.value)

    @patch.object(AppleScriptExecutor, "folder_exists")
    @patch.object(AppleScriptExecutor, "execute")
    def test_create_note_escapes_special_chars(self, mock_execute, mock_folder_exists):
        """Should properly escape quotes in title and body."""
        mock_folder_exists.return_value = True
        mock_execute.return_value = (True, 'Note with "quotes"')

        executor = AppleScriptExecutor()
        executor.create_note('Note with "quotes"', 'Body with "quotes"', "Notes")

        # Verify the script was called with escaped strings
        call_args = mock_execute.call_args[0][0]
        assert '\\"' in call_args


class TestGetNoteBody:
    """Tests for get_note_body method."""

    @patch.object(AppleScriptExecutor, "execute")
    def test_get_note_body_success(self, mock_execute):
        """Should return note body on success."""
        mock_execute.return_value = (True, "This is the note body")

        executor = AppleScriptExecutor()
        result = executor.get_note_body("My Note")

        assert result == "This is the note body"

    @patch.object(AppleScriptExecutor, "execute")
    def test_get_note_body_not_found(self, mock_execute):
        """Should raise NoteNotFoundError when note doesn't exist."""
        mock_execute.return_value = (False, "Note not found")

        executor = AppleScriptExecutor()

        with pytest.raises(NoteNotFoundError) as exc_info:
            executor.get_note_body("Nonexistent Note")

        assert "Nonexistent Note" in str(exc_info.value)

    @patch.object(AppleScriptExecutor, "execute")
    def test_get_note_body_cant_get_note(self, mock_execute):
        """Should raise NoteNotFoundError for 'Can't get note' errors."""
        mock_execute.return_value = (False, 'Can\'t get note whose name = "X"')

        executor = AppleScriptExecutor()

        with pytest.raises(NoteNotFoundError):
            executor.get_note_body("X")


    @patch.object(AppleScriptExecutor, "execute")
    def test_get_note_body_curly_apostrophe_not_found(self, mock_execute):
        """Real osascript output uses a curly apostrophe (issue #126)."""
        mock_execute.return_value = (
            False,
            'Notes got an error: Can’t get note 1 whose name = "X". Invalid index. (-1719)',
        )

        with pytest.raises(NoteNotFoundError):
            AppleScriptExecutor().get_note_body("X")

    @patch.object(AppleScriptExecutor, "execute")
    def test_get_note_body_unrelated_failure_is_not_not_found(self, mock_execute):
        """Only a genuine missing-note error maps to NoteNotFoundError; the
        old blanket `on error error "Note not found"` hid every other failure
        behind the wrong diagnosis (issue #126, NTG-021)."""
        mock_execute.return_value = (
            False, "Notes got an error: AppleEvent timed out. (-1712)"
        )

        with pytest.raises(AppleScriptError) as exc_info:
            AppleScriptExecutor().get_note_body("X")
        assert not isinstance(exc_info.value, NoteNotFoundError)

    @patch.object(AppleScriptExecutor, "execute")
    def test_get_note_body_script_has_no_blanket_handler(self, mock_execute):
        mock_execute.return_value = (True, "body")
        AppleScriptExecutor().get_note_body("X")
        assert 'error "Note not found"' not in mock_execute.call_args[0][0]


class TestUpdateNoteBody:
    """Tests for update_note_body method."""

    @patch.object(AppleScriptExecutor, "execute")
    def test_update_note_body_success(self, mock_execute):
        """Should update note body without raising errors."""
        mock_execute.return_value = (True, "")

        executor = AppleScriptExecutor()
        # Should not raise
        executor.update_note_body("My Note", "New body content")

        mock_execute.assert_called_once()

    @patch.object(AppleScriptExecutor, "execute")
    def test_update_note_body_not_found(self, mock_execute):
        """Should raise NoteNotFoundError when note doesn't exist."""
        mock_execute.return_value = (False, "Note not found")

        executor = AppleScriptExecutor()

        with pytest.raises(NoteNotFoundError):
            executor.update_note_body("Nonexistent", "Body")

    @patch.object(AppleScriptExecutor, "execute")
    def test_update_note_body_script_error(self, mock_execute):
        """Should raise AppleScriptError on other failures."""
        mock_execute.return_value = (False, "Some other error")

        executor = AppleScriptExecutor()

        with pytest.raises(AppleScriptError):
            executor.update_note_body("My Note", "Body")


class TestHtmlFormatting:
    """Tests for HTML formatting methods."""

    def test_escape_html_basic(self):
        """Should return basic strings unchanged."""
        result = AppleScriptExecutor._escape_html("hello world")
        assert result == "hello world"

    def test_escape_html_ampersand(self):
        """Should escape ampersands."""
        result = AppleScriptExecutor._escape_html("Tom & Jerry")
        assert result == "Tom &amp; Jerry"

    def test_escape_html_less_than(self):
        """Should escape less than signs."""
        result = AppleScriptExecutor._escape_html("x < y")
        assert result == "x &lt; y"

    def test_escape_html_greater_than(self):
        """Should escape greater than signs."""
        result = AppleScriptExecutor._escape_html("x > y")
        assert result == "x &gt; y"

    def test_escape_html_all_chars(self):
        """Should escape all special HTML characters."""
        result = AppleScriptExecutor._escape_html("if x < y && y > z")
        assert result == "if x &lt; y &amp;&amp; y &gt; z"

    def test_format_note_html(self):
        """Should format note matching native Apple Notes format."""
        result = AppleScriptExecutor._format_note_html("My Title", "Body text")
        expected = '<div><h1>My Title</h1></div>\n<div><br></div>\n<div><span style="font-size: 18px">Body text</span></div>'
        assert result == expected

    def test_format_note_html_with_newlines(self):
        """Should create separate div for each line."""
        result = AppleScriptExecutor._format_note_html("Title", "Line 1\nLine 2")
        expected = '<div><h1>Title</h1></div>\n<div><br></div>\n<div><span style="font-size: 18px">Line 1</span></div>\n<div><span style="font-size: 18px">Line 2</span></div>'
        assert result == expected

    def test_format_note_html_with_empty_lines(self):
        """Should use <div><br></div> for empty lines."""
        result = AppleScriptExecutor._format_note_html("Title", "Line 1\n\nLine 2")
        expected = '<div><h1>Title</h1></div>\n<div><br></div>\n<div><span style="font-size: 18px">Line 1</span></div>\n<div><br></div>\n<div><span style="font-size: 18px">Line 2</span></div>'
        assert result == expected

    def test_format_note_html_escapes_content(self):
        """Should escape HTML special characters in content."""
        result = AppleScriptExecutor._format_note_html("A & B", "x < y")
        expected = '<div><h1>A &amp; B</h1></div>\n<div><br></div>\n<div><span style="font-size: 18px">x &lt; y</span></div>'
        assert result == expected


class TestAppendToNoteBody:
    """Tests for append_to_note_body method.

    NOTE: Append mode has a known limitation - it corrupts some existing
    formatting (internal links, external hyperlinks, block quotes, colored text).
    This is a fundamental limitation of the Apple Notes AppleScript API.
    """

    @patch.object(AppleScriptExecutor, "get_note_body")
    @patch.object(AppleScriptExecutor, "execute")
    def test_append_to_note_body_success(self, mock_execute, mock_get_body):
        """Should append content to existing note body."""
        mock_get_body.return_value = "<body><h1>Title</h1><div>Existing</div></body>"
        mock_execute.return_value = (True, "")

        executor = AppleScriptExecutor()
        executor.append_to_note_body("My Note", "New content")

        mock_get_body.assert_called_once_with("My Note")
        mock_execute.assert_called_once()
        # Verify the script sets note body
        call_args = mock_execute.call_args[0][0]
        assert "set body of theNote" in call_args

    @patch.object(AppleScriptExecutor, "get_note_body")
    @patch.object(AppleScriptExecutor, "execute")
    def test_append_inserts_before_closing_body_tag(self, mock_execute, mock_get_body):
        """Should insert new content before </body> tag."""
        mock_get_body.return_value = "<body><h1>Title</h1><div>Old</div></body>"
        mock_execute.return_value = (True, "")

        executor = AppleScriptExecutor()
        executor.append_to_note_body("Note", "Added")

        call_args = mock_execute.call_args[0][0]
        # Check that the content is being appended before </body>
        assert "<div><br>Added</div></body>" in call_args

    @patch.object(AppleScriptExecutor, "get_note_body")
    @patch.object(AppleScriptExecutor, "execute")
    def test_append_escapes_html_in_content(self, mock_execute, mock_get_body):
        """Should escape HTML special characters in appended content."""
        mock_get_body.return_value = "<body><div>Existing</div></body>"
        mock_execute.return_value = (True, "")

        executor = AppleScriptExecutor()
        executor.append_to_note_body("Note", "x < y & z > w")

        call_args = mock_execute.call_args[0][0]
        # Verify HTML escaping in the appended content
        assert "x &lt; y &amp; z &gt; w" in call_args

    @patch.object(AppleScriptExecutor, "get_note_body")
    def test_append_note_not_found(self, mock_get_body):
        """Should raise NoteNotFoundError when note doesn't exist."""
        mock_get_body.side_effect = NoteNotFoundError("Note not found")

        executor = AppleScriptExecutor()

        with pytest.raises(NoteNotFoundError):
            executor.append_to_note_body("Nonexistent", "Content")

    @patch.object(AppleScriptExecutor, "get_note_body")
    @patch.object(AppleScriptExecutor, "execute")
    def test_append_script_failure(self, mock_execute, mock_get_body):
        """Should raise AppleScriptError when update fails."""
        mock_get_body.return_value = "<body><div>Existing</div></body>"
        mock_execute.return_value = (False, "Some error")

        executor = AppleScriptExecutor()

        with pytest.raises(AppleScriptError):
            executor.append_to_note_body("Note", "Content")
