"""Section paste-fragment builder with synthesized links (issue #184).

Headless and CI-visible: pure richtext transforms plus one seeded-DB read.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from apple_notes.database import NotesDatabase
from apple_notes.richtext import (
    NotesRichtextParser,
    NotesRichtextSerializer,
    link_target_uuids,
)
from apple_notes.richtext.markdown_converter import markdown_to_richtext
from apple_notes.richtext.section_fragment import (
    FragmentError,
    LinkTarget,
    build_append,
    build_find_replace,
    build_replacement,
    first_paragraph,
    synthesize_link,
)

LINK_UTI = "com.apple.notes.inlinetextattachment.link"
FIXTURE = (
    Path(__file__).parent / "fixtures" / "notes_richtext_sample_with_great_sentence.bin"
)
UUID = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"
OWNER = "_0123456789abcdef0123456789abcdef"
TARGET = LinkTarget(uuid=UUID, title="Target", owner=OWNER)
LINKS = {"target": TARGET}


def _template() -> bytes:
    return FIXTURE.read_bytes()


def _copied(text: str = "Kept paragraph") -> bytes:
    """A one-paragraph copied fragment built on the fixture's archive."""
    return markdown_to_richtext(text, _template()).data


def _parse(data: bytes):
    return NotesRichtextParser().parse(data)


def _links(rt):
    return [a for a in rt.attachments if a.type_uti == LINK_UTI]


class TestReplace:
    def test_replace_synthesizes_link(self):
        rt = _parse(build_replacement(_template(), "See [[Target]] now", LINKS))
        assert rt.text == "See ￼ now"
        (att,) = _links(rt)
        token = json.loads(att.raw_data["34"])
        assert token == {
            "tokenContentIdentifierKey": f"applenotes:note/{UUID.lower()}?ownerIdentifier={OWNER}"
        }
        assert att.raw_data["7"] == "Target"
        assert att.raw_data["25"] == 13
        assert att.raw_data["8"] == LINK_UTI
        assert link_target_uuids(rt) == [UUID]
        pos = rt.text.index("￼")
        run = rt.run_at_position(pos)[1]
        att_id = att.raw_data["2"]
        assert run.raw_data["12"] == {"1": att_id, "2": LINK_UTI}
        assert run.length == 1
        assert att_id == att_id.upper() and len(att_id) == 36

    def test_replace_unresolved_link_stays_text(self):
        rt = _parse(build_replacement(_template(), "See [[Nope]] now", LINKS))
        assert rt.text == "See [[Nope]] now"
        assert _links(rt) == []

    def test_replace_with_tag_refused(self):
        with pytest.raises(FragmentError, match="and tables can.t be added"):
            build_replacement(_template(), "hello {#todo}", LINKS)

    def test_replace_with_table_refused(self):
        with pytest.raises(FragmentError, match="and tables can.t be added"):
            build_replacement(_template(), "| a | b |\n| - | - |\n| 1 | 2 |", LINKS)

    def test_replace_keeps_paragraph_style_of_heading(self):
        rt = _parse(build_replacement(_template(), "## Head [[Target]] tail", LINKS))
        first = rt.runs[0]
        assert first.raw_data["2"]["1"] == 1  # heading kept on the first run
        assert sum(r.length for r in rt.runs) == len(rt.text)

    def test_fragment_round_trips_through_serializer(self):
        data = build_replacement(_template(), "a [[Target]] b [[Target]] c", LINKS)
        rt = _parse(data)
        assert len(_links(rt)) == 2
        assert NotesRichtextSerializer().serialize(rt)
        again = _parse(NotesRichtextSerializer().serialize(rt))
        assert again.text == rt.text
        assert link_target_uuids(again) == [UUID, UUID]
        assert len({a.raw_data["2"] for a in _links(again)}) == 2

    def test_synthesize_link_missing_placeholder(self):
        rt = _parse(_copied())
        with pytest.raises(FragmentError):
            synthesize_link(rt, "{__NG_LINK_9__}", TARGET)

    def test_synthesize_link_needs_owner(self):
        rt = _parse(_copied("x {__NG_LINK_0__} y"))
        with pytest.raises(FragmentError, match="owner"):
            synthesize_link(rt, "{__NG_LINK_0__}", LinkTarget(UUID, "Target", ""))


class TestAppend:
    def test_append_keeps_existing_paragraph_identical(self):
        copied = _copied("Kept paragraph")
        before = _parse(copied)
        out = _parse(build_append(copied, "New line with [[Target]]", LINKS))
        assert out.text == "Kept paragraph" + "\n" + "New line with ￼"
        n = len(before.runs)
        assert [r.raw_data for r in out.runs[:n]] == [r.raw_data for r in before.runs]
        assert [a.raw_data for a in out.attachments[: len(before.attachments)]] == [
            a.raw_data for a in before.attachments
        ]
        assert link_target_uuids(out) == [UUID]
        assert sum(r.length for r in out.runs) == len(out.text)

    def test_append_after_heading_paragraph(self):
        copied = markdown_to_richtext("## Section head", _template()).data
        out = _parse(build_append(copied, "body text", LINKS))
        assert out.text == "Section head\nbody text"
        assert out.runs[0].raw_data["2"]["1"] == 1
        body_run = out.run_at_position(len("Section head\n"))[1]
        assert body_run.raw_data.get("2", {}).get("1", 0) == 0

    def test_append_refuses_tag(self):
        with pytest.raises(FragmentError, match="and tables can.t be added"):
            build_append(_copied(), "x {#t}", LINKS)

    def test_append_round_trips(self):
        data = build_append(_copied(), "more [[Target]]", LINKS)
        assert _parse(NotesRichtextSerializer().serialize(_parse(data))).text.endswith(
            "more ￼"
        )

    def test_append_keeps_multi_paragraph_copy_verbatim(self):
        copied = markdown_to_richtext("## Head\n- [ ] todo item", _template()).data
        before = _parse(copied)
        out = _parse(build_append(copied, "new body", LINKS))
        assert out.text == "Head\ntodo item\nnew body"
        n = len(before.runs)
        assert [r.raw_data for r in out.runs[:n]] == [r.raw_data for r in before.runs]
        joining = out.run_at_position(len("Head\ntodo item"))[1]
        assert joining.raw_data["2"]["1"] == 103  # the last copied paragraph's style

    def test_append_after_empty_last_paragraph_has_no_style(self):
        # Copied "Head\n" = a heading plus an empty last paragraph (a widened
        # blank section): the joining newline ends that blank paragraph.
        copied = markdown_to_richtext("## Head\n", _template()).data
        assert _parse(copied).text == "Head\n"
        out = _parse(build_append(copied, "new body", LINKS))
        assert out.text == "Head\n\nnew body"
        joining = out.run_at_position(len("Head\n"))[1]
        assert "2" not in joining.raw_data
        assert joining.paragraph is None
        assert out.runs[0].raw_data["2"]["1"] == 1


class TestFirstParagraph:
    def test_trims_to_first_paragraph(self):
        copied = markdown_to_richtext("## Head\n", _template()).data
        out = _parse(first_paragraph(copied))
        assert out.text == "Head"
        assert out.runs[0].raw_data["2"]["1"] == 1
        assert all(r.length > 0 for r in out.runs)
        assert sum(r.length for r in out.runs) == len(out.text)

    def test_single_paragraph_unchanged(self):
        copied = _copied("Kept paragraph")
        assert _parse(first_paragraph(copied)).text == "Kept paragraph"


class TestFindReplace:
    def _frag(self):
        return markdown_to_richtext("alpha beta\ngamma delta", _template()).data

    def test_find_replace_within_fragment(self):
        out = _parse(build_find_replace(self._frag(), "beta", "BETA", LINKS))
        assert out.text == "alpha BETA\ngamma delta"
        assert sum(r.length for r in out.runs) == len(out.text)

    def test_find_replace_not_found_refused(self):
        with pytest.raises(FragmentError, match="not found in the section"):
            build_find_replace(self._frag(), "zzz", "x", LINKS)

    def test_find_replace_ambiguous_refused(self):
        frag = markdown_to_richtext("aa\naa", _template()).data
        with pytest.raises(FragmentError, match="found 2 times in the section"):
            build_find_replace(frag, "aa", "b", LINKS)

    def test_find_replace_new_text_link_synthesized(self):
        out = _parse(build_find_replace(self._frag(), "beta", "see [[Target]]", LINKS))
        assert out.text == "alpha see ￼\ngamma delta"
        assert link_target_uuids(out) == [UUID]
        pos = out.text.index("￼")
        assert out.run_at_position(pos)[1].raw_data["12"]["2"] == LINK_UTI
        assert sum(r.length for r in out.runs) == len(out.text)

    def test_find_replace_refuses_tag(self):
        with pytest.raises(FragmentError, match="and tables can.t be added"):
            build_find_replace(self._frag(), "beta", "{#t}", LINKS)

    def test_find_replace_formatted_new_text_with_link(self):
        out = _parse(
            build_find_replace(self._frag(), "beta", "**bold** [[Target]]", LINKS)
        )
        assert out.text == "alpha bold ￼\ngamma delta"
        assert link_target_uuids(out) == [UUID]


@pytest.fixture
def owner_db(scoped_db):
    conn = sqlite3.connect(scoped_db)
    for col in (
        "ZZONEOWNERNAME VARCHAR",
        "ZUSERRECORDNAME VARCHAR",
        "ZACCOUNTTYPE INTEGER",
    ):
        conn.execute(f"ALTER TABLE ZICCLOUDSYNCINGOBJECT ADD COLUMN {col}")
    conn.commit()
    conn.close()
    return scoped_db


def _exec(path, sql, *args):
    conn = sqlite3.connect(path)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


class TestLinkOwnerIdentifier:
    def test_link_owner_shared_note_uses_zone_owner(self, owner_db):
        _exec(
            owner_db,
            "UPDATE ZICCLOUDSYNCINGOBJECT SET ZZONEOWNERNAME='_602e' WHERE Z_PK=10",
        )
        _exec(
            owner_db,
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZACCOUNTTYPE, ZUSERRECORDNAME) VALUES (90, 1, '_51e1')",
        )
        with NotesDatabase(owner_db) as db:
            assert db.link_owner_identifier("AAAA-0D01") == "_602e"

    def test_link_owner_falls_back_to_account(self, owner_db):
        _exec(
            owner_db,
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZACCOUNTTYPE, ZUSERRECORDNAME) VALUES (90, 1, '_51e1')",
        )
        _exec(
            owner_db,
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZACCOUNTTYPE, ZUSERRECORDNAME) VALUES (91, 3, '_other')",
        )
        with NotesDatabase(owner_db) as db:
            assert db.link_owner_identifier("aaaa-0d01") == "_51e1"
            assert db.link_owner_identifier("NO-SUCH-NOTE") == "_51e1"

    def test_link_owner_none_without_account(self, owner_db):
        with NotesDatabase(owner_db) as db:
            assert db.link_owner_identifier("AAAA-0D01") is None

    def test_link_owner_ignores_folder_scope(self, owner_db):
        from apple_notes import folder_scope

        _exec(
            owner_db,
            "UPDATE ZICCLOUDSYNCINGOBJECT SET ZZONEOWNERNAME='_602e' WHERE Z_PK=12",
        )
        folder_scope.set_folder_allowlist(["Daily"])
        try:
            with NotesDatabase(owner_db) as db:
                assert db.link_owner_identifier("AAAA-0B01") == "_602e"
        finally:
            folder_scope.reset()


def test_append_checklist_onto_plain_paragraph():
    copied = markdown_to_richtext_new_plain("Gamma last line.")
    out = _parse(build_append(copied, "- [ ] gamma task", LINKS))
    assert out.text == "Gamma last line.\ngamma task"
    task = out.run_at_position(len("Gamma last line.\n"))[1]
    assert task.raw_data["2"]["1"] == 103


def markdown_to_richtext_new_plain(text: str) -> bytes:
    from apple_notes.richtext.markdown_converter import markdown_to_richtext_new

    return markdown_to_richtext_new(text).data
