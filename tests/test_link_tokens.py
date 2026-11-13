"""Both stored forms of a note link read the same everywhere (issue #247).

Notes stores a link as ``applenotes:note/<uuid>?ownerIdentifier=...`` (our tools,
older links) or ``applenotes://showNote?identifier=<uuid>`` (links made by hand).
Headless and CI-visible: real NotesDatabase and NoteContentReader on seeded ZDATA.
"""

import ast
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from apple_notes import folder_scope, get_backlinks
from apple_notes.database import NotesDatabase
from apple_notes.librarian import find_orphan_notes, resolve_note_links
from apple_notes.link_tokens import note_link_uuid, retarget_note_link
from apple_notes.models import Note, RawNoteLink
from apple_notes.note_content import NoteContentReader
from apple_notes.sections import link_uuid, load_snapshot, resolve_section
from section_fakes import LINK_UTI, encode_note, para

U = "5F63212F-86BD-4E1D-B1EA-906CC6E5F7B0"
SYNTH = f"applenotes:note/{U.lower()}?ownerIdentifier=_0123"
HAND = f"applenotes://showNote?identifier={U}"


class TestNoteLinkUuid:
    @pytest.mark.parametrize("url", [SYNTH, HAND, HAND.lower(), SYNTH.upper()])
    def test_reads_both_forms_as_upper_case(self, url):
        assert note_link_uuid(url) == U

    @pytest.mark.parametrize(
        "url",
        [
            "",
            None,
            "https://example.com/note/abc",
            "applenotes://other?x=abc",
            "https://example.com/?r=applenotes:note/abc-1",
            "xapplenotes://showNote?identifier=ABC",
        ],
    )
    def test_anything_else_is_none(self, url):
        assert note_link_uuid(url) is None


class TestRetarget:
    NEW = "7523FA1D-0D79-411E-ADD8-D22FD90453FA"

    def test_synthesized_form_keeps_owner_and_lower_case(self):
        assert retarget_note_link(SYNTH, self.NEW) == (
            f"applenotes:note/{self.NEW.lower()}?ownerIdentifier=_0123"
        )

    def test_hand_made_form_stays_hand_made_and_upper_case(self):
        assert retarget_note_link(HAND, self.NEW.lower()) == (
            f"applenotes://showNote?identifier={self.NEW}"
        )

    def test_case_follows_the_form_not_the_old_uuid(self):
        upper = f"applenotes:note/{U}?ownerIdentifier=_0123"
        assert retarget_note_link(upper, self.NEW) == (
            f"applenotes:note/{self.NEW.lower()}?ownerIdentifier=_0123"
        )
        lower = f"applenotes://showNote?identifier={U.lower()}"
        assert retarget_note_link(lower, self.NEW) == (
            f"applenotes://showNote?identifier={self.NEW}"
        )

    def test_unrelated_url_is_unchanged(self):
        assert (
            retarget_note_link("https://example.com", self.NEW) == "https://example.com"
        )


def _code_strings(source: str) -> list[str]:
    """Every string literal in a module except docstrings, f-string parts included."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            docstrings.add(id(node.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_only_the_shared_module_parses_tokens():
    """One parser: a reader with its own pattern is how 11% of links went missing.
    section_fragment writes the note/ form; nothing else names the scheme."""
    src = Path(__file__).parent.parent / "src" / "apple_notes"
    allowed = {"link_tokens.py", "section_fragment.py"}
    offenders = [
        p.name
        for p in src.rglob("*.py")
        if p.name not in allowed
        and any("applenotes:" in text for text in _code_strings(p.read_text()))
    ]
    assert offenders == []


# ------------------------------------------------------------ graph + librarian

SOURCE = Note(name="Source", uuid="AA-0", folder="Notes", pk=1)
TARGET = Note(name="Target", uuid=U, folder="Notes", pk=2)
LONER = Note(name="Loner", uuid="AA-2", folder="Notes", pk=3)


def test_hand_made_link_is_an_edge():
    links, broken = resolve_note_links(
        [RawNoteLink(1, HAND, "Target")], [SOURCE, TARGET]
    )
    assert [(l.source_pk, l.target_pk) for l in links] == [(1, 2)]
    assert broken == []


def test_hand_made_link_to_a_dead_note_is_broken():
    dead = "applenotes://showNote?identifier=6BFE804F-0000-0000-0000-000000000000"
    links, broken = resolve_note_links([RawNoteLink(1, dead, "Gone")], [SOURCE, TARGET])
    assert links == []
    assert [(b.target_uuid, b.intended_target_title) for b in broken] == [
        ("6BFE804F-0000-0000-0000-000000000000", "Gone")
    ]


def test_hand_made_link_stops_both_ends_being_orphans():
    links, _ = resolve_note_links([RawNoteLink(1, HAND, "T")], [SOURCE, TARGET, LONER])
    assert [n.name for n in find_orphan_notes([SOURCE, TARGET, LONER], links)] == [
        "Loner"
    ]


def test_both_forms_of_one_target_are_two_links():
    links, _ = resolve_note_links(
        [RawNoteLink(1, HAND, "T"), RawNoteLink(1, SYNTH, "T")], [SOURCE, TARGET]
    )
    assert len(links) == 2


def test_sections_link_uuid_stays_lower_case():
    from apple_notes.sections import AttachmentRef

    att = AttachmentRef("id", LINK_UTI, HAND, "T", None)
    assert link_uuid(att) == U.lower()


# ----------------------------------------------------------------- write verify

RICH_FIXTURE = Path(__file__).parent / "fixtures" / "notes_richtext_sample_with_great_sentence.bin"
RICH_TARGET = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"


def _rich_with_hand_made_link():
    from apple_notes.richtext import NotesRichtextParser

    rt = NotesRichtextParser().parse(RICH_FIXTURE.read_bytes())
    link = next(a for a in rt.attachments if a.type_uti == LINK_UTI)
    link.raw_data["34"] = json.dumps(
        {"tokenContentIdentifierKey": f"applenotes://showNote?identifier={RICH_TARGET}"}
    )
    return rt


class TestWriteVerify:
    def test_positional_read_sees_hand_made_link(self):
        from apple_notes.richtext.operations import link_targets_from

        assert link_targets_from(_rich_with_hand_made_link()) == [RICH_TARGET]

    def test_note_holding_a_hand_made_link_verifies_after_a_whole_note_paste(self):
        from apple_notes.write_verify import unconfirmed_links

        rt = _rich_with_hand_made_link()
        assert unconfirmed_links(rt, rt) == []

    def test_notes_rewriting_the_form_on_paste_does_not_fail_verify(self):
        from apple_notes.richtext import NotesRichtextParser
        from apple_notes.write_verify import unconfirmed_links

        pasted = NotesRichtextParser().parse(RICH_FIXTURE.read_bytes())
        assert unconfirmed_links(pasted, _rich_with_hand_made_link()) == []

    def test_an_earlier_hand_made_link_cannot_confirm_an_appended_one(self):
        from apple_notes.write_verify import unconfirmed_links

        rt = _rich_with_hand_made_link()
        assert unconfirmed_links(rt, rt, actual_from=len(rt.text)) == [
            "1 of 1 note link(s) not confirmed"
        ]


# -------------------------------------------------------- reads, on seeded ZDATA


HAND_FORM = "applenotes://showNote?identifier={}"


@pytest.fixture
def hand_db(scoped_db):
    """Note 13 links by hand to Today (live), Secret (live, hidden under an
    allowlist) and a dead note, with "Plan [link]" as a heading of its own."""
    conn = sqlite3.connect(scoped_db)
    paragraphs = [
        para("Archived day"),
        para("Plan ￼", 1, atts=[("X", LINK_UTI)]),
        para("see ￼ now", atts=[("Y", LINK_UTI)]),
        para("hidden ￼", atts=[("Z", LINK_UTI)]),
        para("dead ￼", atts=[("W", LINK_UTI)]),
        para("Unrelated"),
    ]
    conn.execute(
        "INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (13, ?)",
        (encode_note(paragraphs),),
    )
    rows = [
        (50, "X", 13, LINK_UTI, HAND_FORM.format("AAAA-0D02"), "Last year"),
        (51, "Y", 13, LINK_UTI, HAND_FORM.format("AAAA-0D01"), "Today"),
        (52, "Z", 13, LINK_UTI, HAND_FORM.format("AAAA-0B01"), "Secret"),
        (53, "W", 13, LINK_UTI, HAND_FORM.format("AAAA-BEEF"), "Gone"),
    ]
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE1, ZTYPEUTI1,"
        " ZTOKENCONTENTIDENTIFIER, ZALTTEXT) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    yield scoped_db
    folder_scope.reset()


def _note(db_path) -> Note:
    with NotesDatabase(db_path) as db:
        return db.get_note_by_name("Archived day")


def _content(db_path):
    return NoteContentReader(db_path).get_note_content("Archived day")


def test_read_names_the_target_of_a_hand_made_link(hand_db):
    content = _content(hand_db)
    assert "see [[Today]] now" in content.resolved_text
    assert "[[unknown link]]" not in content.resolved_text
    assert "Today" in [n.target_note_name for n in content.note_links]


def test_read_reports_a_dead_hand_made_link_as_broken(hand_db):
    assert "dead [[broken link: Gone]]" in _content(hand_db).resolved_text


def test_backlinks_include_hand_made_links(hand_db):
    assert "Archived day" in get_backlinks("Last year", db_path=hand_db)


def test_link_targets_include_hand_made_links(hand_db):
    with NotesDatabase(hand_db) as db:
        assert db.get_note_link_targets(13) == [
            "AAAA-0D02",
            "AAAA-0D01",
            "AAAA-0B01",
            "AAAA-BEEF",
        ]


class _NoMediaDb(NotesDatabase):
    """The seeded schema has no media columns, and these notes hold no media."""

    def get_block_attachment_sizes(self, note_pk):
        return {}


class TestSectionAddressing:
    """section_text and heading come from a read, so they must keep resolving
    now that a read shows a real name instead of [[unknown link]]."""

    def _snapshot(self, db_path):
        note = _note(db_path)
        reader = NoteContentReader(db_path)
        with _NoMediaDb(db_path) as db:
            return load_snapshot(db, note.pk, reader.paragraph_renderer(note))

    def test_section_text_copied_from_a_read(self, hand_db):
        lines = _content(hand_db).resolved_text.split("\n")
        line = next(ln for ln in lines if ln.startswith("see "))
        assert line == "see [[Today]] now"
        section = resolve_section(
            self._snapshot(hand_db), "Archived day", heading=None, section_text=line
        )
        assert (section.a, section.b) == (2, 3)

    def test_heading_copied_from_a_read(self, hand_db):
        lines = _content(hand_db).resolved_text.split("\n")
        line = next(ln for ln in lines if "Plan" in ln)
        assert line == "## Plan [[Last year]]"
        section = resolve_section(
            self._snapshot(hand_db),
            "Archived day",
            heading=line.lstrip("# "),
            section_text=None,
        )
        assert section.heading_index == 1

    def test_the_old_notation_no_longer_matches(self, hand_db):
        from apple_notes.sections import SectionError

        with pytest.raises(SectionError, match="not found as whole paragraphs"):
            resolve_section(
                self._snapshot(hand_db),
                "Archived day",
                heading=None,
                section_text="see [[unknown link]] now",
            )


class TestFolderScope:
    """A link to a live note in a hidden folder stays dropped, not broken."""

    def test_hidden_live_target_is_not_reported_broken(self, hand_db):
        folder_scope.set_folder_allowlist(["Daily Archive"])
        with NotesDatabase(hand_db) as db:
            kept = [link.target_url for link in db.get_raw_note_links()]
        assert HAND_FORM.format("AAAA-0B01") not in kept
        assert HAND_FORM.format("AAAA-BEEF") in kept

    @pytest.mark.parametrize("form", ["applenotes:note/{}", HAND_FORM])
    def test_read_does_not_call_a_hidden_live_target_broken(self, hand_db, form):
        conn = sqlite3.connect(hand_db)
        conn.execute(
            "UPDATE ZICCLOUDSYNCINGOBJECT SET ZTOKENCONTENTIDENTIFIER = ? WHERE ZIDENTIFIER = 'Z'",
            (form.format("AAAA-0B01"),),
        )
        conn.commit()
        conn.close()
        folder_scope.set_folder_allowlist(["Daily Archive"])
        text = _content(hand_db).resolved_text
        assert "Secret" not in text
        assert "hidden [[unknown link]]" in text
        assert "dead [[broken link: Gone]]" in text

    def test_target_nested_under_the_trash_stays_broken(self, hand_db):
        """A deleted folder's notes sit in a subfolder of Recently Deleted. They
        are gone, not hidden, so real breakage must still be reported."""
        conn = sqlite3.connect(hand_db)
        conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE2, ZPARENT,"
            " ZFOLDERTYPE) VALUES (6, 'F-TRASHED-SUB', 'Deleted folder', 5, 0)"
        )
        conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZTITLE1, ZFOLDER)"
            " VALUES (16, 'AAAA-0E02', 'Nested trashed', 6)"
        )
        conn.execute(
            "UPDATE ZICCLOUDSYNCINGOBJECT SET ZTOKENCONTENTIDENTIFIER = ? WHERE ZIDENTIFIER = 'W'",
            (HAND_FORM.format("AAAA-0E02"),),
        )
        conn.commit()
        conn.close()
        folder_scope.set_folder_allowlist(["Daily Archive"])
        with NotesDatabase(hand_db) as db:
            assert "AAAA-0E02" not in db.hidden_note_uuids()
            kept = [link.target_url for link in db.get_raw_note_links()]
        assert HAND_FORM.format("AAAA-0E02") in kept
        assert "dead [[broken link: Gone]]" in _content(hand_db).resolved_text

    def test_a_read_looks_up_hidden_notes_once(self, hand_db):
        folder_scope.set_folder_allowlist(["Daily Archive"])
        calls = []
        real = NotesDatabase.hidden_note_uuids

        def counted(self, *args):
            calls.append(1)
            return real(self, *args)

        with patch.object(NotesDatabase, "hidden_note_uuids", counted):
            _content(hand_db)
        assert len(calls) == 1
