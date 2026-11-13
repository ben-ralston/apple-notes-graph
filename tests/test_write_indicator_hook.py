"""Every UI-scripting write shows the indicator for its keystroke window (#108)."""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from apple_notes import ui_operations


@pytest.fixture
def harness():
    """Mock the write plumbing and record indicator/keystroke ordering."""
    events = []

    @contextmanager
    def fake_indicator(action, note_name=None):
        events.append(("indicator_enter", action, note_name))
        try:
            yield
        finally:
            events.append(("indicator_exit", action))

    with (
        patch("apple_notes.ui_operations.ClipboardManager"),
        patch("apple_notes.ui_operations.UIScriptingExecutor") as executor_cls,
        patch(
            "apple_notes.ui_operations.write_indicator", side_effect=fake_indicator
        ) as indicator,
        # Content verification (#200) has its own tests (test_content_verify.py).
        patch("apple_notes.ui_operations._verify_content"),
    ):
        executor = executor_cls.return_value
        executor._note_body_html.side_effect = ["<before>", "<after>"]
        executor._note_exists.return_value = True
        for method in (
            "create_note_with_richtext",
            "append_richtext_to_note",
            "replace_note_richtext",
            "transform_full_note_richtext",
        ):
            getattr(executor, method).side_effect = lambda *a, **k: events.append(
                ("keystrokes",)
            )
        executor._find_replace_via_notes_richtext.side_effect = lambda *a, **k: (
            events.append(("keystrokes",)) or 1
        )
        yield events, executor, indicator


def _assert_wrapped(events, action, note_name):
    """Indicator context opens before the first keystroke and closes after the
    last. The overlay itself is spawned later, by the first guarded keystroke
    (issue #141) — see test_entry_does_not_spawn_overlay."""
    assert ("indicator_enter", action, note_name) in events
    assert ("keystrokes",) in events
    assert ("indicator_exit", action) in events
    assert (
        events.index(("indicator_enter", action, note_name))
        < events.index(("keystrokes",))
        < events.index(("indicator_exit", action))
    )


def test_create_note_wrapped(harness):
    events, _, _ = harness
    ui_operations.execute_create_note("N", b"rt", "Folder", [])
    _assert_wrapped(events, "add_note", "N")


def test_append_wrapped(harness):
    events, _, _ = harness
    ui_operations.execute_append_to_note("N", b"rt", [])
    _assert_wrapped(events, "update_note_content(append)", "N")


def test_replace_wrapped(harness):
    events, _, _ = harness
    ui_operations.execute_replace_note("N", "body", {})
    _assert_wrapped(events, "update_note_content(replace)", "N")


def test_toggle_wrapped(harness):
    # The toggle verifies its checked state from an editor copy-back (#177),
    # so it needs a real checklist note to toggle and read back.
    events, executor, _ = harness
    data = (
        Path(__file__).parent / "fixtures/notes_richtext_sample_with_great_sentence.bin"
    ).read_bytes()
    written = []

    def transform(name, fn):
        events.append(("keystrokes",))
        written.append(fn(data))

    executor.transform_full_note_richtext.side_effect = transform
    with patch("apple_notes.ui_operations.ClipboardManager") as clipboard_cls:
        clipboard_cls.return_value.read_notes_richtext.side_effect = lambda: written[-1]
        ui_operations.execute_toggle_checklist_item("N", "This needs to happen")
    _assert_wrapped(events, "toggle_checklist_item", "N")


def test_find_replace_wrapped(harness):
    events, _, _ = harness
    ui_operations.execute_find_replace_multiline("N", "old", "new")
    _assert_wrapped(events, "update_note_content(find_replace)", "N")


def test_indicator_cleared_when_keystrokes_raise(harness):
    from apple_notes.exceptions import UIScriptingError

    events, executor, _ = harness
    executor.append_richtext_to_note.side_effect = UIScriptingError("focus lost")
    with pytest.raises(UIScriptingError):
        ui_operations.execute_append_to_note("N", b"rt", [])
    assert ("indicator_exit", "update_note_content(append)") in events


def test_delete_note_shows_no_indicator(harness):
    events, _, indicator = harness
    ui_operations.execute_delete_note("N")
    indicator.assert_not_called()
    assert events == []


def test_entry_does_not_spawn_overlay(monkeypatch):
    """The real write_indicator only registers at entry (issue #141).

    With the executor mocked out Notes is never activated and no guarded
    keystroke runs, so nothing calls show_pending() — and the overlay helper must never be spawned.
    """
    monkeypatch.setenv("NOTES_GRAPH_WRITE_INDICATOR", "1")
    with (
        patch("apple_notes.ui_operations.ClipboardManager"),
        patch("apple_notes.ui_operations.UIScriptingExecutor") as executor_cls,
        patch("apple_notes.write_indicator._spawn_helper") as spawn,
        patch("apple_notes.write_indicator._terminate_helper") as terminate,
        patch("apple_notes.ui_operations._verify_content"),
    ):
        executor_cls.return_value._note_body_html.side_effect = ["<before>", "<after>"]
        ui_operations.execute_append_to_note("N", b"rt", [])
    spawn.assert_not_called()
    terminate.assert_not_called()
