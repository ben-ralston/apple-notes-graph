"""Pure content-verification helpers (issues #200, #196).

After a write, the note is copied back out of the editor and compared with
what was pasted. These helpers do the comparing; they touch no UI, so this
module carries no ``macos_gui`` marker and runs in CI.
"""

from apple_notes.richtext import NotesRichtextParser, markdown_to_richtext_new
from apple_notes.write_verify import (
    checklist_states,
    differs,
    leftover_placeholders,
    matches_template,
    normalize,
    unconfirmed_append,
    unconfirmed_find_replace,
    unconfirmed_replace,
)


def rt(markdown: str):
    """Parse the richtext the converter makes for ``markdown``."""
    data = markdown_to_richtext_new(markdown, link_resolver=lambda n: "aaaa-bbbb").data
    return NotesRichtextParser().parse(data)


class TestBasics:
    def test_checklist_states_in_paragraph_order(self):
        assert checklist_states(rt("T\n- [ ] a\n- [x] b\ntext")) == [False, True]

    def test_checklist_states_none(self):
        assert checklist_states(rt("T\nplain")) == []

    def test_normalization_nbsp_and_trailing_newline(self):
        assert normalize("a b\n\n") == "a b"
        assert matches_template("a b\n\n", "a b")

    def test_matches_template_placeholder_wildcard(self):
        assert matches_template("see ￼ now", "see {__NG_LINK_0__} now")

    def test_matches_template_literal_mismatch(self):
        assert not matches_template("see ￼ later", "see {__NG_LINK_0__} now")

    def test_matches_template_escapes_regex_chars(self):
        assert matches_template("cost (a+b)*2?", "cost (a+b)*2?")
        assert not matches_template("cost ab2", "cost (a+b)*2?")

    def test_matches_template_suffix(self):
        assert matches_template("T\nold stuff\nnew line", "new line", suffix=True)
        assert not matches_template("T\nnew line\nold stuff", "new line", suffix=True)

    def test_leftover_tag_placeholder_reported(self):
        expected = "x {__NG_TAG_0__} y {__NG_LINK_1__}"
        assert leftover_placeholders("x {__NG_TAG_0__} y ￼", expected) == [
            "{__NG_TAG_0__}"
        ]

    def test_differs_on_checkbox_only(self):
        assert differs(rt("T\n- [ ] a"), rt("T\n- [x] a"))
        assert not differs(rt("T\n- [ ] a"), rt("T\n- [ ] a"))

    def test_differs_on_text(self):
        assert differs(rt("T\na"), rt("T\nb"))

    def test_differs_none_before_is_false(self):
        assert not differs(None, rt("T\nb"))


class TestAppend:
    def test_unconfirmed_append_ok(self):
        assert unconfirmed_append(rt("T\nold\nmore text"), rt("more text")) == []

    def test_unconfirmed_append_missing_text(self):
        (item,) = unconfirmed_append(rt("T\nold"), rt("more text"))
        assert "appended text" in item

    def test_unconfirmed_append_link_placeholder_is_wildcard(self):
        after = rt("T\nold\nsee X now")  # link became an attachment / its text
        assert unconfirmed_append(after, rt("see [[X]] now")) == []

    def test_unconfirmed_append_quotes_no_note_text(self):
        (item,) = unconfirmed_append(rt("T\nold"), rt("y" * 100))
        assert "y" not in item


class TestReplace:
    def test_unconfirmed_replace_ok(self):
        assert unconfirmed_replace(rt("T\nnew body"), rt("new body")) == []

    def test_unconfirmed_replace_text_mismatch(self):
        assert unconfirmed_replace(rt("T\nold body"), rt("new body"))

    def test_unconfirmed_replace_checkbox_only_ok(self):
        """#196: the checked state landed, the text never changes."""
        assert unconfirmed_replace(rt("T\n- [x] item"), rt("- [x] item")) == []

    def test_unconfirmed_replace_checkbox_mismatch(self):
        (item,) = unconfirmed_replace(rt("T\n- [ ] item"), rt("- [x] item"))
        assert "checkbox" in item

    def test_title_is_not_part_of_the_body(self):
        assert unconfirmed_replace(rt("Title\nbody"), rt("body")) == []


class TestFindReplace:
    def test_unconfirmed_find_replace_ok(self):
        assert (
            unconfirmed_find_replace(rt("T\nnew here"), rt("T\nnew here"), "old") == []
        )

    def test_unconfirmed_find_replace_count(self):
        items = unconfirmed_find_replace(rt("T\nold here"), rt("T\nnew here"), "old")
        assert "old_text appears 1 time(s), expected 0" in items

    def test_count_is_net_of_what_the_replacement_reintroduces(self):
        """old → old+old: the expected note still holds old (twice)."""
        expected = rt("T\nold old here")
        assert unconfirmed_find_replace(rt("T\nold old here"), expected, "old") == []

    def test_unconfirmed_find_replace_bold_only_ok(self):
        """hello → **hello**: text identical, so the content check passes; the
        changed signal (body HTML) decides whether it landed."""
        assert unconfirmed_find_replace(rt("T\nhello"), rt("T\nhello"), "hello") == []

    def test_old_text_in_title_is_not_counted(self):
        assert unconfirmed_find_replace(rt("old\nnew"), rt("old\nnew"), "old") == []

    def test_checkbox_state_compared(self):
        items = unconfirmed_find_replace(rt("T\n- [ ] a"), rt("T\n- [x] a"), "zzz")
        assert any("checkbox" in i for i in items)


class TestPlaceholders:
    def test_unconfirmed_append_leftover_placeholder_is_item(self):
        """The wildcard also matches the literal placeholder, so a placeholder
        left in the note must be reported on its own (plan review #1)."""
        payload = rt("see {#tag}")
        items = unconfirmed_append(rt("T\nold\n" + payload.text), payload)
        assert any("still in the note" in i for i in items)

    def test_table_placeholder_tolerates_extra_newlines(self):
        """Notes keeps a native table as its own block: the newlines around it
        in the copy-back needn't match the payload's (plan review #7)."""
        assert matches_template("a\n￼\n\nb", "a\n{__NG_TABLE_0__}\nb")
        assert matches_template("a￼b", "a\n{__NG_TABLE_0__}\nb")
        assert not matches_template("a\n￼\nc", "a\n{__NG_TABLE_0__}\nb")


class TestLinearMatching:
    def test_many_placeholders_on_a_long_mismatch_is_fast(self):
        """No backtracking regex: k wildcards over a long note must stay
        linear (security review — ReDoS)."""
        import time

        expected = "".join(f"x{{__NG_LINK_{i}__}}" for i in range(40)) + "END"
        actual = "x" * 20000  # never ends with END
        start = time.monotonic()
        assert not matches_template(actual, expected)
        assert not matches_template(actual, expected, suffix=True)
        assert time.monotonic() - start < 0.5

    def test_segments_must_appear_in_order(self):
        assert not matches_template("b a", "a{__NG_LINK_0__}b")
        assert matches_template("a mid b", "a{__NG_LINK_0__}b")

    def test_prefix_and_suffix_cannot_overlap(self):
        assert not matches_template("ab", "ab{__NG_LINK_0__}b")


class TestRoundOneReview:
    def test_items_never_quote_note_text(self):
        """Reasons reach the unified logger and get_write_status, which must
        never carry note contents (review round 1)."""
        secret = "secret words"
        items = (
            unconfirmed_append(rt("T\nold"), rt(secret))
            + unconfirmed_replace(rt("T\nold"), rt(secret))
            + unconfirmed_find_replace(rt(f"T\n{secret}"), rt("T\nx"), secret)
        )
        assert items and not any("secret" in i for i in items)

    def test_find_replace_count_ignores_nbsp_rewrite(self):
        """Codex: Notes rewrites a pasted space as NBSP; the count must agree
        with the normalized template match."""
        after = rt("T\na b c")
        assert unconfirmed_find_replace(after, rt("T\na b c"), "a b") == []

    def test_append_trailing_placeholder_needs_the_last_line(self):
        """'Tagging {#todo}' with the paste lost: an earlier 'Tagging ' must
        not satisfy the suffix (an inline placeholder never spans lines)."""
        payload = rt("Tagging {#todo}")
        assert unconfirmed_append(rt("T\nTagging stuff\nmore"), payload)
        assert unconfirmed_append(rt("T\nold\nTagging #todo"), payload) == []

    def test_mixed_table_then_inline_wildcards_match(self):
        """Codex round 2: leftmost-first with a per-gap newline rule was not
        exact once a multi-line wildcard came first."""
        assert matches_template("x y\ny z", "x{__NG_TABLE_0__}y{__NG_LINK_1__}z")

    def test_placeholder_only_append_verifies(self):
        """Codex round 2: appending just {#todo} landed but was rejected."""
        assert unconfirmed_append(rt("T\nold\n#todo"), rt("{#todo}")) == []

    def test_append_with_prefix_repeated_earlier_verifies(self):
        after = rt("T\nTagging x\nmore\nTagging #todo")
        assert unconfirmed_append(after, rt("Tagging {#todo}")) == []

    def test_multiline_append_anchored_to_its_last_lines(self):
        payload = rt("line one\nsee [[X]] end")
        assert unconfirmed_append(rt("T\nold\nline one\nsee X end"), payload) == []
        assert unconfirmed_append(rt("T\nline one\nsee X end\nold"), payload)
