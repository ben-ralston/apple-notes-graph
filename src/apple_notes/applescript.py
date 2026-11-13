"""AppleScript operations for Apple Notes write operations."""
import re
import subprocess
from typing import Tuple

from . import demo_mode
from .exceptions import (
    AppleScriptError,
    EncryptedNoteError,
    FolderNotFoundError,
    NoteInTrashError,
    NoteNotFoundError,
)

# Default osascript subprocess budget. UI-scripting System Events calls pass a
# tighter budget (issue #141); data-ish AppleScript keeps this generous one.
DEFAULT_TIMEOUT = 30.0

_TIMEOUT_MESSAGE_PREFIX = "AppleScript execution timed out"


def is_timeout_output(output: str) -> bool:
    """True if a (False, output) result came from an osascript timeout."""
    return output.startswith(_TIMEOUT_MESSAGE_PREFIX)


# What Notes itself says when a note reference doesn't resolve — real osascript
# output uses a curly apostrophe (`Can’t get note 1 whose name = "x". Invalid
# index. (-1719)` / `Can’t get note "x". (-1728)`); the ASCII form is kept for
# callers that pre-format output. Scripts that establish "missing" themselves
# raise the literal "Note not found". Nothing else — a timeout, a permissions
# problem, a locked note — is a missing note (issue #126, NTG-021).
_NOTE_NOT_FOUND_MARKERS = ("Can’t get note", "Can't get note", "Note not found")


_COREDATA = r"x-coredata://[0-9A-Fa-f-]{36}"
_NOTE_ID_RE = re.compile(rf"^{_COREDATA}/ICNote/p[1-9][0-9]*$")
_FOLDER_ID_RE = re.compile(rf"^{_COREDATA}/ICFolder/p[1-9][0-9]*$")
_IN_TRASH_SENTINEL = "NOTE_IN_RECENTLY_DELETED"
_LOCKED_SENTINEL = "NOTE_PASSWORD_PROTECTED"


def is_note_not_found_output(output: str) -> bool:
    """True if a (False, output) result means the note genuinely doesn't exist."""
    return any(marker in output for marker in _NOTE_NOT_FOUND_MARKERS)


class AppleScriptExecutor:
    """Execute AppleScript commands for Apple Notes."""

    @staticmethod
    def _escape_html(s: str) -> str:
        """
        Escape special characters for HTML content.

        Args:
            s: The string to escape

        Returns:
            The HTML-escaped string
        """
        s = s.replace("&", "&amp;")
        s = s.replace("<", "&lt;")
        s = s.replace(">", "&gt;")
        return s

    @staticmethod
    def _escape_applescript_string(s: str) -> str:
        """
        Escape a string for use in AppleScript.

        AppleScript strings use double quotes and backslash escaping.

        Args:
            s: The string to escape

        Returns:
            The escaped string (without surrounding quotes)
        """
        # Escape backslashes first, then quotes, then control characters
        s = s.replace("\\", "\\\\")
        s = s.replace('"', '\\"')
        s = s.replace("\n", "\\n")
        s = s.replace("\r", "\\r")
        s = s.replace("\t", "\\t")
        return s

    @classmethod
    def _format_note_html(cls, title: str, body: str) -> str:
        """
        Format note content as HTML matching Apple Notes native format.

        Native format discovered by reading a manually-created note's body:
        - <div><h1>Title</h1></div> for title
        - <div><br></div> for empty line after title
        - <div>line</div> for each body line

        Args:
            title: The note title
            body: The note body (plain text)

        Returns:
            HTML string suitable for Apple Notes body property
        """
        escaped_title = cls._escape_html(title)

        # Title: h1 tag (matches native Apple Notes format exactly)
        html_parts = [f"<div><h1>{escaped_title}</h1></div>"]

        # Empty line after title (matches native format)
        html_parts.append("<div><br></div>")

        # Each body line with 18px font-size (closest match to native)
        # Empty lines become <div><br></div>
        for line in body.split("\n"):
            escaped_line = cls._escape_html(line)
            if escaped_line.strip():
                html_parts.append(
                    f'<div><span style="font-size: 18px">{escaped_line}</span></div>'
                )
            else:
                html_parts.append("<div><br></div>")

        return "\n".join(html_parts)

    def execute(self, script: str, timeout: float | None = None) -> Tuple[bool, str]:
        """
        Execute an AppleScript and return (success, output/error).

        Args:
            script: The AppleScript code to execute
            timeout: Subprocess budget in seconds (default DEFAULT_TIMEOUT).

        Returns:
            Tuple of (success: bool, output_or_error: str)
        """
        # Every osascript call in the library goes through here.
        demo_mode.require_live("Scripting the Notes app")
        if timeout is None:
            timeout = DEFAULT_TIMEOUT
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                return True, result.stdout.strip()
            else:
                return False, result.stderr.strip()

        except subprocess.TimeoutExpired:
            return False, f"{_TIMEOUT_MESSAGE_PREFIX} after {timeout:g}s"
        except OSError as e:
            return False, f"Failed to execute osascript: {e}"

    def move_note_by_id(self, note_id: str, folder_id: str) -> None:
        """Move one note into an existing folder, both addressed by id (#230).

        Pure AppleScript (no clipboard, keystrokes or focus). Ids are the
        x-coredata://<store>/ICNote|ICFolder/p<pk> form (note_target.coredata_id),
        validated before use. Refuses, without moving anything, a note in
        Recently Deleted (moving it would restore it) or a locked note. Never
        creates a folder. A trashed note's container is the "Recently Deleted"
        folder (verified live, #230), so this layer compares the localized
        name, like delete_note's script; the locale-independent check is the
        caller's NoteStore guard (ZFOLDERTYPE 1, note_target).
        """
        if not _NOTE_ID_RE.match(note_id or ""):
            raise ValueError(f"not a Notes note id: {note_id!r}")
        if not _FOLDER_ID_RE.match(folder_id or ""):
            raise ValueError(f"not a Notes folder id: {folder_id!r}")
        script = f'''
tell application "Notes"
    set theNote to note id "{note_id}"
    set c to container of theNote
    if name of c is "Recently Deleted" then
        error "{_IN_TRASH_SENTINEL}"
    end if
    if password protected of theNote then
        error "{_LOCKED_SENTINEL}"
    end if
    set theFolder to folder id "{folder_id}"
    move theNote to theFolder
end tell
'''
        success, output = self.execute(script)
        if success:
            return
        if _IN_TRASH_SENTINEL in output:
            raise NoteInTrashError("The note is in Recently Deleted; it was not moved")
        if _LOCKED_SENTINEL in output:
            raise EncryptedNoteError("The note is password-protected (locked); it was not moved")
        if "get folder" in output.replace("’", "'"):
            raise FolderNotFoundError(f"Folder not found in Apple Notes: {output}")
        if is_note_not_found_output(output):
            raise NoteNotFoundError(f"Note not found in Apple Notes: {output}")
        raise AppleScriptError(f"Failed to move note: {output}")

    def folder_exists(self, folder_name: str) -> bool:
        """
        Check if a folder exists in Apple Notes.

        Args:
            folder_name: The name of the folder to check

        Returns:
            True if folder exists, False otherwise
        """
        escaped_folder = self._escape_applescript_string(folder_name)
        script = f'''
tell application "Notes"
    try
        set theFolder to folder "{escaped_folder}"
        return true
    on error
        return false
    end try
end tell
'''
        success, output = self.execute(script)
        return success and output.lower() == "true"

    def create_note(
        self,
        name: str,
        body: str,
        folder: str = "Notes"
    ) -> str:
        """
        Create a new note in Apple Notes.

        Args:
            name: The note name/title
            body: The note body (plain text)
            folder: The folder name (default "Notes")

        Returns:
            The name of the created note

        Raises:
            FolderNotFoundError: If folder doesn't exist
            AppleScriptError: If creation fails
        """
        # Check if folder exists
        if not self.folder_exists(folder):
            raise FolderNotFoundError(f"Folder '{folder}' not found in Apple Notes")

        # Format content as HTML matching native Apple Notes format
        # Title is styled in HTML body with font-size: 24px
        html_body = self._format_note_html(name, body)

        escaped_body = self._escape_applescript_string(html_body)
        escaped_folder = self._escape_applescript_string(folder)

        # Set name to empty string to prevent duplicate title display
        # Apple Notes automatically shows the `name` property as the first line
        # By using empty string, we let the HTML-formatted title in body be the visual title
        # The first line of body HTML becomes the display name in the sidebar
        script = f'''
tell application "Notes"
    tell folder "{escaped_folder}"
        make new note with properties {{name:"", body:"{escaped_body}"}}
    end tell
end tell
'''
        success, output = self.execute(script)

        if not success:
            raise AppleScriptError(f"Failed to create note: {output}")

        return name

    def get_note_body(self, note_name: str) -> str:
        """
        Get the current body of a note.

        Args:
            note_name: The title of the note

        Returns:
            The note's body content

        Raises:
            NoteNotFoundError: If note doesn't exist
            AppleScriptError: If retrieval fails
        """
        escaped_name = self._escape_applescript_string(note_name)

        script = f'''
tell application "Notes"
    set theNote to first note whose name = "{escaped_name}"
    return body of theNote
end tell
'''
        success, output = self.execute(script)

        if not success:
            if is_note_not_found_output(output):
                raise NoteNotFoundError(f"Note '{note_name}' not found")
            raise AppleScriptError(f"Failed to get note body: {output}")

        return output

    def update_note_body(
        self,
        note_name: str,
        new_body: str
    ) -> None:
        """
        Update a note's body content.

        Args:
            note_name: The title of the note
            new_body: The new body content (plain text)

        Raises:
            NoteNotFoundError: If note doesn't exist
            AppleScriptError: If update fails
        """
        # Format content as HTML matching native Apple Notes format
        # Title is included in body HTML as <div><h1>Title</h1></div>
        html_body = self._format_note_html(note_name, new_body)

        escaped_name = self._escape_applescript_string(note_name)
        escaped_body = self._escape_applescript_string(html_body)

        script = f'''
tell application "Notes"
    set theNote to first note whose name = "{escaped_name}"
    set body of theNote to "{escaped_body}"
end tell
'''
        success, output = self.execute(script)

        if not success:
            if is_note_not_found_output(output):
                raise NoteNotFoundError(f"Note '{note_name}' not found")
            raise AppleScriptError(f"Failed to update note: {output}")

    def append_to_note_body(
        self,
        note_name: str,
        content_to_append: str
    ) -> None:
        """
        Append content to a note's body.

        KNOWN LIMITATION: Appending content will corrupt some existing formatting
        in the note, including:
        - Internal [[Note Name]] links (will become plain text)
        - External hyperlinks (will lose clickability)
        - Block quotes (will become plain text)
        - Colored text (will lose color)

        This is a fundamental limitation of the Apple Notes AppleScript API.
        The `body` property doesn't preserve these special formats when written
        back. There is no native "append" method in Apple Notes AppleScript.

        For notes with special formatting, recommend using replace mode with
        full content, or manual editing.

        Args:
            note_name: The title of the note
            content_to_append: The content to append (plain text)

        Raises:
            NoteNotFoundError: If note doesn't exist
            AppleScriptError: If update fails
        """
        # Get current body (HTML)
        current_body = self.get_note_body(note_name)

        # Format new content as HTML
        escaped_content = self._escape_html(content_to_append)
        new_html = escaped_content.replace("\n", "<br>")

        # Insert before </body> tag, or append if no closing tag found
        if "</body>" in current_body:
            updated_body = current_body.replace(
                "</body>",
                f"<div><br>{new_html}</div></body>"
            )
        else:
            # Fallback: just append as new div
            updated_body = current_body + f"<div><br>{new_html}</div>"

        escaped_name = self._escape_applescript_string(note_name)
        escaped_body = self._escape_applescript_string(updated_body)

        script = f'''
tell application "Notes"
    set theNote to first note whose name = "{escaped_name}"
    set body of theNote to "{escaped_body}"
end tell
'''
        success, output = self.execute(script)

        if not success:
            if is_note_not_found_output(output):
                raise NoteNotFoundError(f"Note '{note_name}' not found")
            raise AppleScriptError(f"Failed to append to note: {output}")
