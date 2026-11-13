"""Focus after the typed table flow (issues #222, #223).

``_replace_with_table`` ends with Escape, but keyboard focus stays inside
the table, so the verify copy-back's Cmd+A that follows copied only a cell
and a write that landed was reported partial. The pending-action pass must
put focus back in the note body once after any table.

No GUI: the keystroke helpers are replaced by recorders, so this module
carries no ``macos_gui`` marker and runs in CI.
"""

import pytest

from apple_notes.exceptions import (
    NotesModalOpenError,
    NotesNotFrontmostError,
    UIScriptingError,
)
from apple_notes.richtext import PendingKeyboardAction
from apple_notes.ui_scripting import UIScriptingExecutor


def _table(i: int) -> PendingKeyboardAction:
    return PendingKeyboardAction(
        placeholder=f"{{__NG_TABLE_{i}__}}",
        action_type="table",
        text_to_type="",
        original_markdown="| a | b |\n| --- | --- |\n| 1 | 2 |",
        table_cells=[["a", "b"], ["1", "2"]],
        table_col_count=2,
    )


def _tag(i: int) -> PendingKeyboardAction:
    return PendingKeyboardAction(
        placeholder=f"{{__NG_TAG_{i}__}}",
        action_type="hashtag",
        text_to_type="todo",
        original_markdown="{#todo}",
    )


@pytest.fixture
def recorder():
    executor = UIScriptingExecutor.__new__(UIScriptingExecutor)
    executor._dialog_expected = False
    calls: list[str] = []
    executor._find_and_select_text = lambda text: calls.append("find")
    executor._replace_with_table = lambda cells, cols: calls.append("table")
    executor._replace_with_hashtag = lambda name: calls.append("tag")
    executor._focus_note_content = lambda: calls.append("focus")
    return executor, calls


def test_table_action_refocuses_note_body_once(recorder):
    executor, calls = recorder
    assert executor.apply_pending_keyboard_actions([_table(0), _tag(1), _table(2)]) == 3
    assert calls.count("focus") == 1
    assert calls[-1] == "focus"
    assert calls.index("focus") > max(i for i, c in enumerate(calls) if c == "table")


def test_hashtag_only_actions_do_not_refocus(recorder):
    executor, calls = recorder
    executor.apply_pending_keyboard_actions([_tag(0), _tag(1)])
    assert "focus" not in calls


def test_no_actions_do_not_refocus(recorder):
    executor, calls = recorder
    assert executor.apply_pending_keyboard_actions([]) == 0
    assert calls == []


def test_wiki_link_action_still_refused_before_any_key(recorder):
    executor, calls = recorder
    link = PendingKeyboardAction(
        placeholder="{__NG_LINK_0__}",
        action_type="wiki_link",
        text_to_type="Target",
        original_markdown="[[Target]]",
    )
    with pytest.raises(UIScriptingError):
        executor.apply_pending_keyboard_actions([_table(0), link])
    assert calls == []


@pytest.mark.parametrize(
    "error",
    [
        NotesNotFrontmostError("Notes lost the front"),
        NotesModalOpenError("a dialog is open in Notes"),
        UIScriptingError("focus failed"),
    ],
)
def test_refocus_failure_after_landed_table_is_not_a_write_error(recorder, error):
    # The table already landed: a refocus failure must not surface as a
    # retryable write error (a retry would append the table twice). The
    # verify copy-back that follows reports what it can or can't read.
    executor, calls = recorder

    def failing_focus():
        calls.append("focus")
        raise error

    executor._focus_note_content = failing_focus
    assert executor.apply_pending_keyboard_actions([_table(0)]) == 1
    assert calls[-1] == "focus"
