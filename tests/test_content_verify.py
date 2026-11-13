"""Content-aware verification for append / replace / find_replace (#200, #196).

A write is a success only when what was pasted is confirmed in the editor
copy-back. Nothing changed → WriteVerificationError (safe to retry). Changed
but not fully confirmed → PartialWriteError (don't retry: it would duplicate
what landed).

The editor and pasteboard are simulated, with the note held as markdown and
converted to real Notes richtext, so this module has no ``macos_gui`` marker
and runs in CI.
"""

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apple_notes import profiling, ui_operations
from apple_notes.exceptions import (
    LinksNotConfirmedError,
    NotesNotFrontmostError,
    PartialWriteError,
    WriteVerificationError,
)
from apple_notes.richtext import NotesRichtextParser, link_target_uuids
from apple_notes.richtext.section_fragment import (
    LinkTarget,
    markdown_to_richtext_new_linked,
)

X_UUID = "AAAAAAAA-1111-2222-3333-444444444444"
LINKS = {"x": LinkTarget(X_UUID, "X", "_owner")}
DROPPED = "\ufffc"  # what a link that didn't land leaves: U+FFFC, no link


def mk(markdown: str) -> bytes:
    """Real richtext; [[X]] is a synthesized link to X_UUID (issue #212)."""
    return markdown_to_richtext_new_linked(markdown, LINKS).data


def html_of(markdown: str) -> str:
    """Body HTML as AppleScript renders it: no checked state, no bold."""
    text = re.sub(r"- \[[ x]\] ", "<li>", markdown).replace("**", "")
    return f"<div>{text}</div>"


@pytest.fixture
def sim(verify_clock):
    """The note lives in ``s.editor`` (markdown). A write that lands sets the
    editor to ``s.after`` and the body HTML to ``s.after_html`` (default
    ``html_of(s.after)``). Knobs: ``paste_lands``, ``copy_lands``,
    ``copy_refused``, ``lag`` (copy-backs that still see the old editor).
    ``s.pasted`` is what a replace transform produced, ``s.fr_links`` the
    links find_replace was given."""
    s = SimpleNamespace(
        editor="T\nold",
        after="T\nold",
        after_html=None,
        paste_lands=True,
        copy_lands=True,
        copy_refused=False,
        lag=0,
        pasted=None,
        fr_links=None,
        board=None,
        pending=None,
        events=[],
        html=None,
        count=1,
    )
    with (
        patch("apple_notes.ui_operations.UIScriptingExecutor") as executor_cls,
        patch("apple_notes.ui_operations.ClipboardManager") as clipboard_cls,
    ):
        ex = executor_cls.return_value
        cb = clipboard_cls.return_value

        def land():
            s.events.append("paste")
            if s.paste_lands:
                s.pending = s.after

        def body_html(_name):
            # Body HTML shows a landed paste whether or not anything copies.
            if s.pending is not None and not s.lag:
                s.editor, s.pending = s.pending, None
                s.html = s.after_html or html_of(s.editor)
            if s.html is None:
                s.html = html_of(s.editor)
            return s.html

        def append(_name, _data):
            land()

        def replace(_name, transform):
            s.pasted = transform(mk(s.editor))
            land()

        def find_replace(_name, _old, _new, links=None, capture=None):
            s.fr_links = links
            capture["before"] = mk(s.editor)
            capture["expected"] = mk(s.after)
            land()
            return s.count

        def pending(_actions):
            s.events.append("pending")

        def copy():
            s.events.append("copy")
            if s.copy_refused:
                raise NotesNotFrontmostError("Notes is not frontmost")
            if s.pending is not None:
                if s.lag:
                    s.lag -= 1
                else:
                    s.editor, s.pending = s.pending, None
                    s.html = s.after_html or html_of(s.editor)
            if s.copy_lands:
                s.board = mk(s.editor)

        def clear():
            s.board = None

        ex._note_body_html.side_effect = body_html
        ex.append_richtext_to_note.side_effect = append
        ex.replace_note_richtext.side_effect = replace
        ex.create_note_with_richtext.side_effect = lambda *_a: land()
        ex._find_replace_via_notes_richtext.side_effect = find_replace
        ex.apply_pending_keyboard_actions.side_effect = pending
        ex.select_all_and_copy.side_effect = copy
        ex._note_exists.return_value = True
        cb.clear.side_effect = clear
        cb.read_notes_richtext.side_effect = lambda: s.board
        cb.restore_state.side_effect = lambda _st: s.events.append("restore")
        s.ex, s.cb = ex, cb
        yield s


def append(md="more text", actions=()):
    ui_operations.execute_append_to_note(
        name="n", richtext_data=mk(md), pending_actions=list(actions)
    )


def replace(md, links=None):
    ui_operations.execute_replace_note(name="n", body_markdown=md, links=links or {})


def find_replace(old="old", new="new", links=None):
    ui_operations.execute_find_replace_multiline(
        name="n", old_text=old, replace_text=new, links=links
    )


def create(md):
    ui_operations.execute_create_note(
        name="n", richtext_data=mk(md), folder="Notes", pending_actions=[]
    )


class TestAppend:
    def test_append_landed_verifies(self, sim):
        sim.after = "T\nold\nmore text"
        append()

    def test_append_text_missing_is_partial(self, sim):
        sim.after = "T\nold\nmore"  # half the paste
        with pytest.raises(PartialWriteError, match="appended text"):
            append()

    def test_append_unchanged_raises_not_partial(self, sim):
        sim.paste_lands = False
        with pytest.raises(WriteVerificationError, match="unchanged") as err:
            append()
        assert type(err.value) is WriteVerificationError

    def test_append_same_text_twice_needs_html_change(self, sim):
        """The note already ends with the text: the suffix check passes before
        the paste lands, so 'changed' must come from body HTML."""
        sim.editor = "T\nmore text"
        sim.paste_lands = False
        with pytest.raises(WriteVerificationError, match="unchanged"):
            append()

    def test_partial_message_says_do_not_retry(self, sim):
        sim.after = "T\nold\nmore"
        with pytest.raises(PartialWriteError, match="Don't retry"):
            append()


class TestReplace:
    def test_replace_landed_verifies(self, sim):
        sim.after = "T\nnew body"
        replace("new body")

    def test_replace_checkbox_only_succeeds(self, sim):
        """#196: body HTML is identical; the checked state flipped."""
        sim.editor = "T\n- [ ] item"
        sim.after = "T\n- [x] item"
        assert html_of(sim.editor) == html_of(sim.after)
        replace("- [x] item")

    def test_replace_checkbox_not_landed_raises(self, sim):
        sim.editor = sim.after = "T\n- [ ] item"
        sim.paste_lands = False
        with pytest.raises(WriteVerificationError) as err:
            replace("- [x] item")
        assert type(err.value) is WriteVerificationError

    def test_replace_not_landed_raises(self, sim):
        sim.after = "T\nnew body"
        sim.paste_lands = False
        with pytest.raises(WriteVerificationError, match="unchanged") as err:
            replace("new body")
        assert type(err.value) is WriteVerificationError

    def test_replace_wrong_text_is_partial(self, sim):
        sim.after = "T\nnew bo"
        with pytest.raises(PartialWriteError, match="body doesn't match"):
            replace("new body")

    def test_replace_bold_only_landed_verifies_via_html(self, sim):
        sim.editor = sim.after = "T\nhello"
        sim.after_html = "<div><b>hello</b></div>"
        replace("**hello**")

    def test_replace_bold_only_not_landed_raises(self, sim):
        sim.editor = sim.after = "T\nhello"
        sim.paste_lands = False
        with pytest.raises(WriteVerificationError, match="unchanged"):
            replace("**hello**")


class TestFindReplace:
    def test_find_replace_count_drop_verifies(self, sim):
        sim.after = "T\nnew"
        find_replace()

    def test_find_replace_old_text_left_is_partial(self, sim):
        """The editor shows a different change than the one we pasted."""
        sim.after = "T\nold new"

        def find_replace_fn(_n, _o, _r, links=None, capture=None):
            capture["before"] = mk("T\nold")
            capture["expected"] = mk("T\nnew")
            sim.pending = sim.after
            return 1

        sim.ex._find_replace_via_notes_richtext.side_effect = find_replace_fn
        with pytest.raises(PartialWriteError, match="old_text appears 1"):
            find_replace()

    def test_find_replace_not_landed_raises(self, sim):
        sim.after = "T\nnew"
        sim.paste_lands = False
        with pytest.raises(WriteVerificationError, match="unchanged") as err:
            find_replace()
        assert type(err.value) is WriteVerificationError

    def test_find_replace_bold_only_landed(self, sim):
        sim.editor = sim.after = "T\nhello"
        sim.after_html = "<div><b>hello</b></div>"
        find_replace("hello", "**hello**")


class TestSynthesizedLinks:
    """Links are pasted as attachments and confirmed by target uuid (#212)."""

    def test_append_link_landed_verifies(self, sim):
        sim.after = "T\nold\nsee [[X]]"
        append("see [[X]]")

    def test_append_link_dropped_is_partial(self, sim):
        sim.after = f"T\nold\nsee {DROPPED}"
        with pytest.raises(PartialWriteError, match="1 of 1 note link"):
            append("see [[X]]")

    def test_replace_pastes_synthesized_link_with_no_keyboard_action(self, sim):
        sim.after = "T\nsee [[X]]"
        replace("see [[X]]", LINKS)
        assert link_target_uuids(NotesRichtextParser().parse(sim.pasted)) == [X_UUID]
        assert "pending" not in sim.events

    def test_replace_link_dropped_is_partial(self, sim):
        sim.after = f"T\nsee {DROPPED}"
        with pytest.raises(PartialWriteError, match="note link"):
            replace("see [[X]]", LINKS)

    def test_find_replace_passes_links_to_the_executor(self, sim):
        sim.after = "T\nnew"
        find_replace(links=LINKS)
        assert sim.fr_links == LINKS

    def test_find_replace_link_dropped_is_partial(self, sim):
        sim.after = f"T\nsee {DROPPED}"

        def fr(_n, _o, _r, links=None, capture=None):
            capture["before"] = mk("T\nold")
            capture["expected"] = mk("T\nsee [[X]]")
            sim.pending = sim.after
            return 1

        sim.ex._find_replace_via_notes_richtext.side_effect = fr
        with pytest.raises(PartialWriteError, match="note link"):
            find_replace("old", "see [[X]]", LINKS)

    def test_create_link_landed_verifies(self, sim):
        sim.after = "n\nsee [[X]]"
        create("see [[X]]")
        assert "copy" in sim.events

    def test_create_link_dropped_is_partial(self, sim):
        sim.after = f"n\nsee {DROPPED}"
        with pytest.raises(LinksNotConfirmedError, match="1 of 1 note link"):
            create("see [[X]]")

    def test_create_link_unreadable_is_partial(self, sim):
        sim.after = "n\nsee [[X]]"
        sim.copy_lands = False
        with pytest.raises(LinksNotConfirmedError, match="read back"):
            create("see [[X]]")

    def test_create_without_links_skips_copy_back(self, sim):
        sim.after = "n\nplain"
        create("plain")
        assert "copy" not in sim.events

    def test_create_note_absent_is_not_partial(self, sim):
        sim.ex._note_exists.return_value = False
        with pytest.raises(WriteVerificationError, match="does not exist") as err:
            create("see [[X]]")
        assert not isinstance(err.value, PartialWriteError)


class TestReadBack:
    def test_focus_refusal_falls_back_to_html(self, sim):
        """Losing the front after the paste landed must not read as a
        retryable focus error: the note changed, so it's partial."""
        sim.after = "T\nold\nmore text"
        sim.copy_refused = True
        sim.html = None
        sim.ex._note_body_html.side_effect = [
            "<div>before</div>",
            "<div>after</div>",
        ]
        with pytest.raises(PartialWriteError, match="read back"):
            append()

    def test_focus_refusal_stops_polling(self, sim):
        sim.copy_refused = True
        with pytest.raises(WriteVerificationError):
            append()
        assert sim.events.count("copy") == 1

    def test_unreadable_copy_back_html_unchanged_raises(self, sim):
        sim.copy_lands = False
        sim.paste_lands = False
        with pytest.raises(WriteVerificationError, match="unchanged") as err:
            append()
        assert type(err.value) is WriteVerificationError

    def test_unreadable_copy_back_html_changed_is_partial(self, sim):
        sim.after = "T\nold\nmore text"
        sim.copy_lands = False
        sim.ex._note_body_html.side_effect = [
            "<div>before</div>",
            "<div>after</div>",
        ]
        with pytest.raises(PartialWriteError, match="read back"):
            append()

    def test_verify_runs_before_clipboard_restore(self, sim):
        sim.after = "T\nold\nmore text"
        append()
        assert sim.events.index("copy") < sim.events.index("restore")

    def test_lagging_editor_retried(self, sim, verify_clock):
        sim.after = "T\nold\nmore text"
        sim.lag = 1
        append()
        assert verify_clock.sleeps == [ui_operations.VERIFY_POLL_INTERVAL]

    def test_gives_up_at_deadline(self, sim, verify_clock):
        sim.after = "T\nold\nmore"
        with pytest.raises(PartialWriteError):
            append()
        assert verify_clock.now == pytest.approx(ui_operations.VERIFY_POLL_TIMEOUT)
        assert all(s <= ui_operations.VERIFY_POLL_INTERVAL for s in verify_clock.sleeps)

    def test_records_verify_span(self, sim):
        sim.after = "T\nold\nmore text"
        with profiling.profile("test") as prof:
            append()
        assert "verify" in prof.timing_ms

    def test_records_verify_span_on_failure(self, sim):
        sim.paste_lands = False
        with profiling.profile("test") as prof:
            with pytest.raises(WriteVerificationError):
                append()
        assert "verify" in prof.timing_ms

    def test_deadline_is_wall_clock_not_attempts(self, sim, verify_clock):
        """A copy-back that itself eats the whole budget gets no retry."""
        sim.after = "T\nold\nmore"

        def slow_copy():
            verify_clock.advance(ui_operations.VERIFY_POLL_TIMEOUT + 1)
            sim.events.append("copy")
            sim.editor = sim.after
            sim.board = mk(sim.editor)
            sim.html = html_of(sim.editor)

        sim.ex.select_all_and_copy.side_effect = slow_copy
        with pytest.raises(PartialWriteError):
            append()
        assert sim.events.count("copy") == 1
        assert verify_clock.sleeps == []

    def test_failed_escape_keeps_the_copied_data(self, sim):
        """Losing the front on the trailing Escape, after the copy landed, must
        not throw away good data (plan review #5)."""
        sim.after = "T\nold\nmore text"
        sim.ex._send_key_code.side_effect = NotesNotFrontmostError("lost front")
        append()

    def test_other_ui_errors_after_the_paste_are_not_retryable(self, sim):
        """Any UI scripting failure in the copy-back reads as 'couldn't read
        back', never as a retryable UIScriptingError (plan review #5)."""
        from apple_notes.exceptions import UIScriptingError

        sim.after = "T\nold\nmore text"
        sim.ex.select_all_and_copy.side_effect = UIScriptingError("timed out")
        sim.ex._note_body_html.side_effect = ["<div>a</div>", "<div>b</div>"]
        with pytest.raises(PartialWriteError, match="read back"):
            append()


class TestRoundOneReview:
    def test_confirmed_content_with_lagging_html_is_success(self, sim):
        """Read 1 confirms the content but body HTML hasn't caught up; read 2
        is refused (focus loss) so the poll stops; the post-loop HTML check
        then shows the change. A confirmed write - not a partial with an
        empty item list (review round 1)."""
        sim.after = "T\nold\nmore text"
        copies = []

        def copy():
            copies.append(1)
            if len(copies) > 1:
                raise NotesNotFrontmostError("lost front")
            sim.editor = sim.after
            sim.board = mk(sim.editor)

        sim.ex.select_all_and_copy.side_effect = copy
        sim.ex._note_body_html.side_effect = ["<div>a</div>", "<div>a</div>", "<div>b</div>"]
        append()
