"""Tests for checklist + paragraph style interaction (issue #68).

Checklist items in Apple Notes are stored with paragraph style 104 (bullet).
The bullet prefix (* ) should NOT be applied to checklist lines, since they
get their own prefix (- [ ] / - [x] ) from _apply_checklist_prefixes.
"""

from apple_notes.note_content import NoteContentReader


class TestChecklistBulletInteraction:
    """Checklist lines with bullet paragraph style should not get '* ' prefix."""

    def setup_method(self):
        self.reader = NoteContentReader.__new__(NoteContentReader)

    def test_checklist_lines_skip_bullet_prefix(self):
        """Checklist items with style 104 should NOT get '* ' prefix."""
        text = "Title\nCall parents\nCancel daycare\nClean up"
        checklist_lines = {1: False, 2: False, 3: True}
        paragraph_style_lines = {1: 104, 2: 104, 3: 104}

        result = self.reader._apply_paragraph_styles(
            text, paragraph_style_lines, checklist_lines
        )

        lines = result.split("\n")
        assert lines[0] == "Title"
        # These should NOT have "* " prefix since they're checklist items
        assert lines[1] == "Call parents"
        assert lines[2] == "Cancel daycare"
        assert lines[3] == "Clean up"

    def test_non_checklist_bullets_still_get_prefix(self):
        """Non-checklist bullet lines should still get '* ' prefix."""
        text = "Title\nBullet item one\nBullet item two"
        checklist_lines = {}
        paragraph_style_lines = {1: 104, 2: 104}

        result = self.reader._apply_paragraph_styles(
            text, paragraph_style_lines, checklist_lines
        )

        lines = result.split("\n")
        assert lines[0] == "Title"
        assert lines[1] == "* Bullet item one"
        assert lines[2] == "* Bullet item two"

    def test_mixed_checklist_and_bullet_lines(self):
        """Mix of checklist and plain bullet lines."""
        text = "Title\nChecklist item\nBullet item\nAnother checklist"
        checklist_lines = {1: False, 3: True}
        paragraph_style_lines = {1: 104, 2: 104, 3: 104}

        result = self.reader._apply_paragraph_styles(
            text, paragraph_style_lines, checklist_lines
        )

        lines = result.split("\n")
        assert lines[0] == "Title"
        assert lines[1] == "Checklist item"  # checklist: no bullet prefix
        assert lines[2] == "* Bullet item"  # not checklist: gets bullet prefix
        assert lines[3] == "Another checklist"  # checklist: no bullet prefix

    def test_non_bullet_styles_unaffected(self):
        """Headings and other styles should work regardless of checklist_lines."""
        text = "My Title\nA heading\nSome text"
        checklist_lines = {}
        paragraph_style_lines = {0: 0, 1: 1}

        result = self.reader._apply_paragraph_styles(
            text, paragraph_style_lines, checklist_lines
        )

        lines = result.split("\n")
        assert lines[0] == "# My Title"
        assert lines[1] == "## A heading"
        assert lines[2] == "Some text"

    def test_default_checklist_lines_is_empty(self):
        """When no checklist_lines passed, behavior unchanged (backward compat)."""
        text = "Title\nBullet item"
        paragraph_style_lines = {1: 104}

        # Call without checklist_lines argument
        result = self.reader._apply_paragraph_styles(text, paragraph_style_lines)

        lines = result.split("\n")
        assert lines[1] == "* Bullet item"
