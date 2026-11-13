"""Verify-after-write tests for ui_operations (issue #90).

Even with the focus guard, a UI-scripting write can complete without error yet
not take effect (keystrokes dropped). Each execute_* must read Notes.app back
and raise WriteVerificationError instead of letting the caller report a false
success. The session-lock preflight is neutralized by the autouse conftest
fixture, so these tests reach the executor.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apple_notes import profiling, ui_operations
from apple_notes.exceptions import (
    EncryptedNoteError,
    UIScriptingError,
    PartialWriteError,
    WriteVerificationError,
)


class TestVerifyAfterWrite:
    @pytest.fixture(autouse=True)
    def mock_clipboard_and_executor(self, verify_clock):
        with (
            patch("apple_notes.ui_operations.ClipboardManager"),
            patch("apple_notes.ui_operations.UIScriptingExecutor") as executor,
        ):
            yield executor.return_value

    # --- create: existence check ---

    def test_create_raises_when_note_absent_afterward(
        self, mock_clipboard_and_executor
    ):
        mock_clipboard_and_executor._note_exists.return_value = False
        with pytest.raises(WriteVerificationError):
            ui_operations.execute_create_note(
                name="n", richtext_data=b"x", folder="Notes", pending_actions=[]
            )

    def test_create_succeeds_when_note_present(self, mock_clipboard_and_executor):
        mock_clipboard_and_executor._note_exists.return_value = True
        ui_operations.execute_create_note(
            name="n", richtext_data=b"x", folder="Notes", pending_actions=[]
        )

    # --- append: body must change ---

    def test_append_raises_when_unchanged(self, mock_clipboard_and_executor):
        mock_clipboard_and_executor._note_body_html.return_value = "<div>same</div>"
        with pytest.raises(WriteVerificationError):
            ui_operations.execute_append_to_note(
                name="n", richtext_data=b"x", pending_actions=[]
            )

    def test_append_changed_but_unreadable_is_partial(
        self, mock_clipboard_and_executor
    ):
        """The mocked clipboard never yields a copy-back: body HTML changed, so
        the write landed but can't be confirmed (#200) — not a success."""
        mock_clipboard_and_executor._note_body_html.side_effect = [
            "<div>before</div>",
            "<div>before</div><div>after</div>",
        ]
        with pytest.raises(PartialWriteError, match="read back"):
            ui_operations.execute_append_to_note(
                name="n", richtext_data=b"x", pending_actions=[]
            )

    # --- replace: body must change ---

    def test_replace_raises_when_unchanged(self, mock_clipboard_and_executor):
        mock_clipboard_and_executor._note_body_html.return_value = "<div>same</div>"
        with pytest.raises(WriteVerificationError):
            ui_operations.execute_replace_note(
                name="n", body_markdown="body", links={}
            )

    def test_replace_changed_but_unreadable_is_partial(
        self, mock_clipboard_and_executor
    ):
        mock_clipboard_and_executor._note_body_html.side_effect = [
            "<div>old</div>",
            "<div>new</div>",
        ]
        with pytest.raises(PartialWriteError, match="read back"):
            ui_operations.execute_replace_note(
                name="n", body_markdown="body", links={}
            )

    # --- find_replace: body must change (in addition to count > 0) ---

    def test_find_replace_raises_when_unchanged(self, mock_clipboard_and_executor):
        mock_clipboard_and_executor._find_replace_via_notes_richtext.return_value = 1
        mock_clipboard_and_executor._note_body_html.return_value = "<div>same</div>"
        with pytest.raises(WriteVerificationError):
            ui_operations.execute_find_replace_multiline(
                name="n", old_text="old", replace_text="new"
            )


FIXTURE = (
    Path(__file__).parent / "fixtures/notes_richtext_sample_with_great_sentence.bin"
)
ITEM = "This needs to happen"  # an unchecked checklist item in FIXTURE


class TestToggleVerify:
    """toggle_checklist_item verifies the item's checked state in the editor
    (issue #177). AppleScript body HTML renders a checklist item as a plain
    `<li>` whatever its checked state, so a body-HTML comparison reported
    every landed toggle as failed — and a retry toggled the item back."""

    @pytest.fixture
    def engine(self, verify_clock):
        """Simulated editor + pasteboard. transform_full_note_richtext puts
        the toggled payload on the pasteboard and (if the paste lands) into
        the editor; select_all_and_copy (if the copy lands) copies the editor
        onto the pasteboard. Flip `paste_lands` / `copy_lands` to model
        keystrokes that silently went nowhere."""
        sim = SimpleNamespace(
            data=FIXTURE.read_bytes(),
            board=None,
            paste_lands=True,
            copy_lands=True,
            paste_lag_copies=0,  # copy-backs that still see the old editor
            events=[],
        )
        sim.editor = sim.data
        with (
            patch("apple_notes.ui_operations.UIScriptingExecutor") as executor_cls,
            patch("apple_notes.ui_operations.ClipboardManager") as clipboard_cls,
        ):
            executor = executor_cls.return_value
            clipboard = clipboard_cls.return_value

            def transform(name, fn):
                payload = fn(sim.editor)
                sim.board = payload
                sim.events.append("paste")
                if sim.paste_lands:
                    sim.pending = payload

            def copy():
                sim.events.append("copy")
                if getattr(sim, "pending", None) is not None:
                    if sim.paste_lag_copies:
                        sim.paste_lag_copies -= 1
                    else:
                        sim.editor, sim.pending = sim.pending, None
                if sim.copy_lands:
                    sim.board = sim.editor

            def clear():
                sim.events.append("clear")
                sim.board = None

            executor.transform_full_note_richtext.side_effect = transform
            executor.select_all_and_copy.side_effect = copy
            # Body HTML never shows checked state — identical before/after.
            executor._note_body_html.return_value = "<ul><li>item</li></ul>"
            clipboard.clear.side_effect = clear
            clipboard.read_notes_richtext.side_effect = lambda: sim.board
            sim.executor, sim.clipboard = executor, clipboard
            yield sim

    def test_landed_toggle_succeeds_though_body_html_unchanged(self, engine):
        """The #177 false failure: the toggle landed, body HTML is identical."""
        ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)
        engine.executor.select_all_and_copy.assert_called_once()
        engine.clipboard.read_notes_richtext.assert_called_once()

    def test_paste_that_did_not_land_raises(self, engine):
        """A real no-op: the editor still holds the old checked state."""
        engine.paste_lands = False
        with pytest.raises(WriteVerificationError, match="checked state"):
            ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)

    def test_stale_pasteboard_is_not_mistaken_for_the_editor(self, engine):
        """Paste AND copy went nowhere: the pasteboard still holds our own
        toggled payload, which must not read as success (review, round 1)."""
        engine.paste_lands = False
        engine.copy_lands = False
        with pytest.raises(WriteVerificationError):
            ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)

    def test_pasteboard_cleared_before_each_copy_back(self, engine):
        engine.paste_lag_copies = 1
        ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)
        assert engine.events == ["paste", "clear", "copy", "clear", "copy"]

    def test_lagging_paste_is_retried_not_failed(self, engine, verify_clock):
        """The editor shows the toggle only on the second copy-back — must
        succeed, not invite a retry that toggles the item back."""
        engine.paste_lag_copies = 1
        ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)
        assert engine.executor.select_all_and_copy.call_count == 2
        assert verify_clock.sleeps == [ui_operations.VERIFY_POLL_INTERVAL]

    def test_copy_back_gives_up_at_deadline(self, engine, verify_clock):
        engine.paste_lands = False
        with pytest.raises(WriteVerificationError):
            ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)
        assert verify_clock.now == pytest.approx(ui_operations.VERIFY_POLL_TIMEOUT)

    def test_copy_back_closes_selection_and_restores_clipboard(self, engine):
        ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)
        engine.executor._send_key_code.assert_called_with(
            engine.executor.ESCAPE_KEY_CODE
        )
        engine.clipboard.restore_state.assert_called_once()

    def test_locked_note_refused_before_keystrokes(self, engine):
        """The pre-write body read is the toggle's locked-note guard (#126)."""
        engine.executor._note_body_html.side_effect = EncryptedNoteError("locked")
        with pytest.raises(EncryptedNoteError):
            ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)
        engine.executor.transform_full_note_richtext.assert_not_called()

    def test_unreadable_toggled_state_aborts_before_paste(self, engine):
        """If the toggled item can't be read back from our own payload, refuse
        inside the transform — before anything is pasted."""
        with patch("apple_notes.ui_operations.checklist_item_checked", return_value=None):
            with pytest.raises(UIScriptingError):
                ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)
        assert "paste" not in engine.events

    def test_records_verify_span(self, engine):
        with profiling.profile("test") as prof:
            ui_operations.execute_toggle_checklist_item(name="n", item_text=ITEM)
        assert "verify" in prof.timing_ms
