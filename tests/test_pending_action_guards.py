"""Guards on the typed tag and table flow (issue #122).

- A Cmd+F that misses leaves the previous selection (or a bare caret) in the
  note, and the tag and table flows start with Delete: the write must confirm
  the selection is exactly the placeholder before any destructive key.
- Ragged markdown table rows must be padded to the column count, and a
  table that isn't exactly two columns wide is refused before anything is
  pasted (one column: #122; three or more: #260).
- Adjacent identical tags must not merge into one (one tag was dropped).

No GUI: osascript is mocked, so this module carries no ``macos_gui`` marker
and runs in CI.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apple_notes.exceptions import (
    PartialWriteError,
    PlaceholderNotFoundError,
    UIScriptingError,
)
from apple_notes.richtext import PendingKeyboardAction, markdown_to_richtext_new
from apple_notes.richtext.markdown_converter import parse_markdown
from apple_notes.ui_helper_client import _ERROR_TYPE_MAP
from apple_notes.ui_scripting import UIScriptingExecutor, refuse_untypeable_actions

DELETE = "key code 51"
TAB = "key code 48"


def _tag(i: int, name: str = "todo") -> PendingKeyboardAction:
    return PendingKeyboardAction(
        placeholder=f"{{__NG_TAG_{i}__}}",
        action_type="hashtag",
        text_to_type=name,
        original_markdown=f"{{#{name}}}",
    )


def _table(cells, col_count, i: int = 0) -> PendingKeyboardAction:
    return PendingKeyboardAction(
        placeholder=f"{{__NG_TABLE_{i}__}}",
        action_type="table",
        text_to_type="",
        original_markdown="",
        table_cells=cells,
        table_col_count=col_count,
    )


COPY = 'keystroke "c" using command down'
CLEAR = "<clipboard cleared>"


def _is_probe(script: str) -> bool:
    return COPY in script


@pytest.fixture
def scripted():
    """An executor whose osascript calls are recorded; the selection copy
    (Cmd+C, then a plain-text read) answers from ``answers``, one per copy
    with the last repeating. ``None`` = nothing was copied."""
    with (
        patch("apple_notes.ui_scripting.AppleScriptExecutor") as cls,
        patch("apple_notes.ui_scripting.ClipboardManager") as clip_cls,
        patch("apple_notes.ui_scripting.time.sleep"),
        patch("apple_notes.ui_scripting.show_pending"),
    ):
        cls._escape_applescript_string = lambda s: s.replace("\\", "\\\\").replace(
            '"', '\\"'
        )
        inst = MagicMock()
        cls.return_value = inst
        clip = MagicMock()
        clip_cls.return_value = clip
        calls: list[str] = []
        answers: list = []

        def execute(script, **kwargs):
            calls.append(script)
            return True, ""

        def read_plain_text():
            return answers.pop(0) if len(answers) > 1 else answers[0]

        inst.execute.side_effect = execute
        clip.clear.side_effect = lambda: calls.append(CLEAR)
        clip.read_plain_text.side_effect = read_plain_text
        yield UIScriptingExecutor(), calls, answers


def _after_last_probe(calls):
    idx = max(i for i, s in enumerate(calls) if _is_probe(s))
    return calls[idx + 1 :]


class TestFindSelectionCheck:
    def test_matching_selection_proceeds_to_delete(self, scripted):
        ex, calls, answers = scripted
        answers.append("{__NG_TAG_0__}")
        assert ex.apply_pending_keyboard_actions([_tag(0)]) == 1
        assert any(_is_probe(s) for s in calls)
        assert any(DELETE in s for s in _after_last_probe(calls))

    def test_miss_keeping_other_selection_sends_no_delete(self, scripted):
        """Live (2026-11-09): a Find miss leaves the previous selection."""
        ex, calls, answers = scripted
        answers.append("some real words")
        with pytest.raises(PlaceholderNotFoundError):
            ex.apply_pending_keyboard_actions([_tag(0)])
        assert not any(DELETE in s for s in _after_last_probe(calls))

    def test_miss_with_bare_caret_sends_no_delete(self, scripted):
        ex, calls, answers = scripted
        answers.append("")
        with pytest.raises(PlaceholderNotFoundError):
            ex.apply_pending_keyboard_actions([_table([["a", "b"]], 2)])
        assert not any(DELETE in s for s in _after_last_probe(calls))

    def test_no_focused_text_is_a_miss(self, scripted):
        ex, calls, answers = scripted
        answers.append(None)
        with pytest.raises(PlaceholderNotFoundError):
            ex.apply_pending_keyboard_actions([_tag(0)])

    def test_match_must_be_exact(self, scripted):
        """Notes' Find ignores case, and real text can hold a near copy."""
        ex, calls, answers = scripted
        answers.append("{__ng_tag_0__}")
        with pytest.raises(PlaceholderNotFoundError):
            ex.apply_pending_keyboard_actions([_tag(0)])

    def test_trailing_whitespace_is_not_stripped_into_a_match(self, scripted):
        ex, calls, answers = scripted
        answers.append("{__NG_TAG_0__} ")
        with pytest.raises(PlaceholderNotFoundError):
            ex.apply_pending_keyboard_actions([_tag(0)])

    def test_slow_find_is_polled_briefly(self, scripted):
        ex, calls, answers = scripted
        answers.extend(["", "{__NG_TAG_0__}"])
        assert ex.apply_pending_keyboard_actions([_tag(0)]) == 1

    def test_second_action_miss_reports_partial_progress(self, scripted):
        ex, calls, answers = scripted
        answers.extend(["{__NG_TAG_0__}", "nope"])
        with pytest.raises(PlaceholderNotFoundError) as info:
            ex.apply_pending_keyboard_actions([_tag(0), _tag(1, "done")])
        msg = str(info.value)
        assert "1 of 2" in msg
        assert "{__NG_TAG_1__}" in msg

    def test_error_never_quotes_the_selected_note_text(self, scripted):
        ex, calls, answers = scripted
        answers.append("my private sentence")
        with pytest.raises(PlaceholderNotFoundError) as info:
            ex.apply_pending_keyboard_actions([_tag(0)])
        assert "my private sentence" not in str(info.value)

    def test_error_is_partial_so_nobody_retries_the_write(self):
        # The paste already landed: a retry would duplicate it, and the
        # remote queue reports a PartialWriteError as applied_unverified.
        assert issubclass(PlaceholderNotFoundError, PartialWriteError)

    def test_error_keeps_its_type_across_the_daemon(self):
        assert _ERROR_TYPE_MAP["PlaceholderNotFoundError"] is PlaceholderNotFoundError

    def test_probe_runs_behind_the_frontmost_guard(self, scripted):
        ex, calls, answers = scripted
        answers.append("{__NG_TAG_0__}")
        ex.apply_pending_keyboard_actions([_tag(0)])
        probe = next(s for s in calls if _is_probe(s))
        assert "NOTES_NOT_FRONTMOST" in probe

    def test_clipboard_is_cleared_before_every_copy(self, scripted):
        """A bare caret copies nothing: a stale clipboard holding the
        placeholder must not read as a match."""
        ex, calls, answers = scripted
        answers.extend(["", "{__NG_TAG_0__}"])
        ex.apply_pending_keyboard_actions([_tag(0)])
        copies = [i for i, s in enumerate(calls) if _is_probe(s)]
        assert len(copies) == 2
        assert all(calls[i - 1] == CLEAR for i in copies)


class TestTableShape:
    def test_ragged_rows_are_padded_to_the_column_count(self):
        doc = parse_markdown("| a | b | c |\n|---|---|---|\n| 1 |\n| x | y |")
        table = doc.lines[0].table
        assert table.col_count == 3
        assert table.cells == [["a", "b", "c"], ["1", "", ""], ["x", "y", ""]]

    def test_typed_ragged_table_tabs_through_every_cell(self, scripted):
        """Defensive: a ragged grid off the daemon wire still fills the right
        cells (one Tab between each of rows x cols cells)."""
        ex, calls, answers = scripted
        answers.append("{__NG_TABLE_0__}")
        ex.apply_pending_keyboard_actions([_table([["a", "b"], ["1"], ["x"]], 2)])
        typed = _after_last_probe(calls)
        # 3 rows x 2 columns = 6 cells -> 5 Tabs between them; Tab at the end
        # of a row past the second creates the new row (issue #260).
        plain_tabs = [s for s in typed if TAB in s and "using" not in s]
        assert len(plain_tabs) == 5
        # No column-add keystroke: Notes has no shortcut for it (#260).
        assert not any("key code 124 using" in s for s in typed)

    @pytest.mark.parametrize("cols", [3, 4, 7])
    def test_wide_table_is_refused_before_any_paste(self, cols):
        """Notes inserts a 2x2 table and has no add-column shortcut, so a
        wider table landed as loose cell text beside an empty 2x2 (#260)."""
        cells = [[f"r{r}c{c}" for c in range(cols)] for r in range(3)]
        with pytest.raises(UIScriptingError, match="Nothing was written") as info:
            refuse_untypeable_actions([_table(cells, cols)])
        msg = str(info.value)
        assert f"{cols} columns" in msg
        assert "2 columns" in msg
        # The message never quotes cell text (it may be private).
        assert "r0c0" not in msg

    def test_wide_table_sends_no_keystroke(self, scripted):
        """Defensive: a wide table off the daemon wire is refused before
        Find, so the note is untouched."""
        ex, calls, answers = scripted
        answers.append("{__NG_TABLE_0__}")
        cells = [["a", "b", "c"]] * 3
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            ex.apply_pending_keyboard_actions([_tag(0), _table(cells, 3, 1)])
        assert calls == []

    def test_tall_two_column_table_is_accepted(self):
        refuse_untypeable_actions([_table([["a", "b"]] * 6, 2)])

    def test_one_column_table_is_refused_before_any_paste(self):
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            refuse_untypeable_actions([_table([["only"], ["one"]], 1)])

    def test_one_column_markdown_table_still_converts(self):
        # Refusal is the keyboard layer's job; the converter keeps the shape.
        result = markdown_to_richtext_new("| a |\n|---|\n| 1 |")
        (action,) = result.pending_actions
        assert action.table_col_count == 1


class TestAdjacentTags:
    def test_adjacent_identical_tags_stay_two_tags(self):
        result = markdown_to_richtext_new("{#a}{#a}")
        tags = [a for a in result.pending_actions if a.action_type == "hashtag"]
        assert [a.text_to_type for a in tags] == ["a", "a"]
        assert len({a.placeholder for a in tags}) == 2


class TestMissCollapsesSelection:
    def test_miss_collapses_the_left_over_selection(self, scripted):
        """The user's real text stays selected after a miss, so their next
        key would replace it: collapse it before raising."""
        ex, calls, answers = scripted
        answers.append("some real words")
        with pytest.raises(PlaceholderNotFoundError):
            ex.apply_pending_keyboard_actions([_tag(0)])
        after = _after_last_probe(calls)
        assert any("key code 123" in s for s in after)
        assert not any(DELETE in s for s in after)


ONE_COL = "| solo |\n|---|\n| one |"
THREE_COL = "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |"


@pytest.fixture
def ops():
    """ui_operations with the GUI mocked; ``state.html`` is the note's body
    HTML before the write."""
    from apple_notes import ui_operations

    state = SimpleNamespace(html="<div>T</div><div>body</div>")
    with (
        patch("apple_notes.ui_operations.UIScriptingExecutor") as executor_cls,
        patch("apple_notes.ui_operations.ClipboardManager"),
        patch("apple_notes.ui_operations.edit_lock.edit_lock"),
        patch("apple_notes.ui_operations.session_lock.require_ui_scripting_session"),
        patch("apple_notes.ui_operations.write_indicator"),
        patch("apple_notes.ui_operations._verify_content"),
    ):
        ex = executor_cls.return_value
        ex._note_body_html.side_effect = lambda _n: state.html
        ex._find_replace_via_notes_richtext.return_value = 1
        yield ui_operations, ex, state


def _pasted(ex) -> bool:
    return any(
        m.called
        for m in (
            ex.append_richtext_to_note,
            ex.replace_note_richtext,
            ex._find_replace_via_notes_richtext,
        )
    )


class TestRefusedBeforeAnyPaste:
    """'Nothing was written' must be true on every write path (review of
    #122: replace and find_replace only met the refusal after their paste)."""

    def test_replace_refuses_one_column_table_before_the_paste(self, ops):
        uo, ex, _ = ops
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_replace_note(name="n", body_markdown=ONE_COL, links={})
        assert not _pasted(ex)

    def test_find_replace_refuses_one_column_table_before_the_paste(self, ops):
        uo, ex, _ = ops
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_find_replace_multiline(
                name="n", old_text="body", replace_text=ONE_COL
            )
        assert not _pasted(ex)

    def test_replace_refuses_three_column_table_before_the_paste(self, ops):
        uo, ex, _ = ops
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_replace_note(name="n", body_markdown=THREE_COL, links={})
        assert not _pasted(ex)

    def test_find_replace_refuses_three_column_table_before_the_paste(self, ops):
        uo, ex, _ = ops
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_find_replace_multiline(
                name="n", old_text="body", replace_text=THREE_COL
            )
        assert not _pasted(ex)

    def test_append_refuses_three_column_table_before_the_paste(self, ops):
        uo, ex, _ = ops
        actions = markdown_to_richtext_new(THREE_COL).pending_actions
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_append_to_note(
                name="n", richtext_data=b"", pending_actions=actions
            )
        assert not _pasted(ex)

    def test_create_refuses_three_column_table_before_the_paste(self, ops):
        uo, ex, _ = ops
        actions = markdown_to_richtext_new(THREE_COL).pending_actions
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_create_note(
                name="n", richtext_data=b"", folder="Notes", pending_actions=actions
            )
        assert not ex.mock_calls

    @pytest.mark.parametrize(
        "method, args",
        [
            ("create_note", lambda acts: ("n", b"", "Notes", acts)),
            ("append_to_note", lambda acts: ("n", b"", acts)),
            ("replace_note", lambda acts: ("n", THREE_COL, {})),
            ("find_replace", lambda acts: ("n", "body", THREE_COL, {})),
        ],
    )
    def test_daemon_client_refuses_three_column_table_before_sending(
        self, method, args
    ):
        """The daemon is a long-lived LaunchAgent: after a pull it can still
        run code that types a wide table, so the client refuses first (#260
        review)."""
        from apple_notes.ui_helper_client import UIHelperClient

        actions = markdown_to_richtext_new(THREE_COL).pending_actions
        client = UIHelperClient()
        with patch.object(client, "_send_command") as send:
            with pytest.raises(UIScriptingError, match="Nothing was written"):
                getattr(client, method)(*args(actions))
        send.assert_not_called()

    def test_daemon_client_still_sends_two_column_tables(self):
        from apple_notes.ui_helper_client import UIHelperClient

        two = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        actions = markdown_to_richtext_new(two).pending_actions
        client = UIHelperClient()
        with patch.object(client, "_send_command") as send:
            client.append_to_note("n", b"", actions)
            client.replace_note("n", two, {})
        assert send.call_count == 2

    def test_append_refuses_when_note_already_holds_a_placeholder(self, ops):
        """A literal placeholder left by an earlier failed write would be
        what Find selects first, so the tag would land in the wrong place."""
        uo, ex, state = ops
        state.html = "<div>T</div><div>old {__NG_TAG_0__} here</div>"
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_append_to_note(
                name="n", richtext_data=b"", pending_actions=[_tag(0)]
            )
        assert not _pasted(ex)

    def test_find_replace_refuses_typed_tag_when_note_holds_a_placeholder(
        self, ops
    ):
        uo, ex, state = ops
        state.html = "<div>T</div><div>{__NG_TABLE_0__} body</div>"
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_find_replace_multiline(
                name="n", old_text="body", replace_text="{#todo}"
            )
        assert not _pasted(ex)

    def test_stale_placeholder_is_fine_when_nothing_is_typed(self, ops):
        uo, ex, state = ops
        state.html = "<div>T</div><div>old {__NG_TAG_0__} here</div>"
        uo.execute_append_to_note(name="n", richtext_data=b"", pending_actions=[])
        assert ex.append_richtext_to_note.called

    def test_find_replace_may_repair_the_stale_placeholder_itself(self, ops):
        uo, ex, state = ops
        state.html = "<div>T</div><div>old {__NG_TAG_0__} here</div>"
        uo.execute_find_replace_multiline(
            name="n", old_text="{__NG_TAG_0__}", replace_text="{#todo}"
        )
        assert ex._find_replace_via_notes_richtext.called

    def test_repair_exemption_survives_html_escaping(self, ops):
        """old_text is plain text, the before read is HTML (review round 2)."""
        uo, ex, state = ops
        state.html = "<div>T</div><div>A &amp; {__NG_TAG_0__}</div>"
        uo.execute_find_replace_multiline(
            name="n", old_text="A & {__NG_TAG_0__}", replace_text="{#todo}"
        )
        assert ex._find_replace_via_notes_richtext.called

    def test_repairing_one_of_two_stale_placeholders_is_refused(self, ops):
        uo, ex, state = ops
        state.html = "<div>T</div><div>{__NG_TAG_0__}</div><div>{__NG_TAG_1__}</div>"
        with pytest.raises(UIScriptingError, match="Nothing was written"):
            uo.execute_find_replace_multiline(
                name="n", old_text="{__NG_TAG_0__}", replace_text="{#todo}"
            )
        assert not _pasted(ex)
