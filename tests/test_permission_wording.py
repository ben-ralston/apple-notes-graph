"""macOS 27 renamed the Accessibility pane; our directions must use the new name."""

from pathlib import Path

from apple_notes.exceptions import ACCESSIBILITY_SETTINGS
from apple_notes.ui_scripting import _PERSISTENT_HINT

ROOT = Path(__file__).resolve().parent.parent


def _user_facing_files():
    yield from (ROOT / "src" / "apple_notes").rglob("*.py")
    yield from (ROOT / "src" / "apple_notes_mcp").rglob("*.py")
    yield ROOT / "README.md"
    yield ROOT / "run"
    yield ROOT / "docs" / "remote-connector.md"


def test_settings_path_names_the_current_pane_and_the_old_one():
    assert "Privacy & Security > Device Control and Data Access" in ACCESSIBILITY_SETTINGS
    assert "Accessibility" in ACCESSIBILITY_SETTINGS


def test_hint_shown_to_users_uses_the_shared_path():
    assert ACCESSIBILITY_SETTINGS in _PERSISTENT_HINT


def test_no_directions_to_the_old_pane_name_remain():
    stale = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _user_facing_files()
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if "Security > Accessibility" in line
        or "2. Accessibility:" in line
        or "**Accessibility**" in line
    ]
    assert stale == []
