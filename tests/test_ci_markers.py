"""Guard: GUI/Notes-dependent test modules must carry the macos_gui marker.

CI (.github/workflows/ci.yml) runs the suite headlessly on a GitHub macOS
runner with `-m "not macos_gui"` (issue #95). These modules exercise code
paths that assume a live GUI session with Apple Notes and TCC grants, so
they must be excluded there. This test keeps the marker from silently
disappearing when those files are edited.
"""

import importlib.util
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent

# Modules needing a live macOS GUI session + Apple Notes / TCC (issue #95).
GUI_TEST_MODULES = [
    "test_applescript",
    "test_clipboard",
    "test_icloud_sync",
    "test_reminders_eventkit",
    "test_session_lock",
    "test_ui_helper",
]

# Fully mocked modules that must stay CI-visible (issue #143): marking one
# macos_gui would silently drop its tests from merge gating again.
CI_VISIBLE_MOCKED_MODULES = [
    "test_ui_scripting",
    "test_ui_scripting_errors",
    "test_ui_helper_timeouts",
]


def _module_marks(module_name):
    spec = importlib.util.spec_from_file_location(
        module_name, TESTS_DIR / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    marks = getattr(module, "pytestmark", [])
    if not isinstance(marks, list):
        marks = [marks]
    return [mark.name for mark in marks]


@pytest.mark.parametrize("module_name", GUI_TEST_MODULES)
def test_gui_module_is_marked_macos_gui(module_name):
    assert "macos_gui" in _module_marks(module_name), (
        f"{module_name}.py must set `pytestmark = pytest.mark.macos_gui` "
        f'so CI can exclude it with -m "not macos_gui"'
    )


@pytest.mark.parametrize("module_name", CI_VISIBLE_MOCKED_MODULES)
def test_mocked_module_is_not_marked_macos_gui(module_name):
    assert "macos_gui" not in _module_marks(module_name), (
        f"{module_name}.py is fully mocked and must run in CI; "
        "move any test that needs a live GUI into a macos_gui-marked module"
    )
