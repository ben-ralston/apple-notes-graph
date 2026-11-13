"""Re-pointing an existing note link (issue #183).

Headless and CI-visible: the raw link read, the richtext relink operation,
target resolution and guards, and the write engine with mocked UI scripting.
"""

import gzip
import sqlite3

import blackboxprotobuf
import pytest

from apple_notes import folder_scope
from apple_notes.database import NotesDatabase

LINK_UTI = "com.apple.notes.inlinetextattachment.link"
TABLE_UTI = "com.apple.notes.table"
OWNER = "?ownerIdentifier=_0123456789abcdef0123456789abcdef"

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"


def _encode_note(text: str, attachment_ids: list[tuple[str, str]]) -> bytes:
    """ZDATA whose text holds one U+FFFC run per (identifier, uti), in order."""
    run_td = {
        "1": {"type": "int", "name": ""},
        "12": {
            "type": "message",
            "message_typedef": {
                "1": {"type": "string", "name": ""},
                "2": {"type": "string", "name": ""},
            },
            "name": "",
        },
    }
    typedef = {
        "2": {
            "type": "message",
            "message_typedef": {
                "3": {
                    "type": "message",
                    "message_typedef": {
                        "2": {"type": "bytes", "name": ""},
                        "5": {"type": "message", "message_typedef": run_td, "name": ""},
                    },
                    "name": "",
                },
            },
            "name": "",
        },
    }
    runs = [{"1": len(text)}]
    for identifier, uti in attachment_ids:
        runs.append({"1": 1, "12": {"1": identifier, "2": uti}})
    full = text + "￼" * len(attachment_ids)
    message = {"2": {"3": {"2": full.encode("utf-8"), "5": runs}}}
    return gzip.compress(blackboxprotobuf.encode_message(message, typedef))


@pytest.fixture
def link_db(scoped_db):
    """scoped_db plus note 13 ("Archived day") with raw link attachments."""
    conn = sqlite3.connect(scoped_db)
    conn.execute(
        "INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (13, ?)",
        (
            _encode_note(
                "Archived day\nlinks ",
                [("X", LINK_UTI), ("T", TABLE_UTI), ("Y", LINK_UTI), ("X2", LINK_UTI)],
            ),
        ),
    )
    rows = [
        (40, "X", 13, LINK_UTI, f"applenotes:note/{U1.lower()}{OWNER}", "One", 0),
        (41, "T", 13, TABLE_UTI, None, None, 0),
        (42, "Y", 13, LINK_UTI, f"applenotes:note/{U2.lower()}{OWNER}", "Two", 0),
        (43, "X2", 13, LINK_UTI, f"applenotes:note/{U1.lower()}{OWNER}", "One", 0),
        # stale row from an earlier paste: id not in ZDATA, different target
        (44, "OLD", 13, LINK_UTI, "applenotes:note/aaaa-0d01", "Today", 1),
    ]
    conn.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZIDENTIFIER, ZNOTE1, ZTYPEUTI1,"
        " ZTOKENCONTENTIDENTIFIER, ZALTTEXT, ZMARKEDFORDELETION) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return scoped_db


class TestGetNoteLinkTargets:
    def test_link_targets_in_text_order_with_duplicates(self, link_db):
        with NotesDatabase(link_db) as db:
            assert db.get_note_link_targets(13) == [U1, U2, U1]

    def test_link_targets_pair_by_identifier_not_stale_rows(self, link_db):
        with NotesDatabase(link_db) as db:
            assert "AAAA-0D01" not in db.get_note_link_targets(13)

    def test_link_targets_include_targets_of_deleted_notes(self, link_db):
        # U1/U2 are no note's uuid: links to deleted notes are still reported.
        with NotesDatabase(link_db) as db:
            assert db.get_note_by_uuid(U1) is None
            assert U1 in db.get_note_link_targets(13)

    def test_link_targets_out_of_scope_returns_empty(self, link_db):
        folder_scope.set_folder_allowlist(["Daily"])
        try:
            with NotesDatabase(link_db) as db:
                assert db.get_note_link_targets(13) == []
        finally:
            folder_scope.reset()

    def test_link_targets_read_hand_made_form(self, link_db):
        conn = sqlite3.connect(link_db)
        conn.execute(
            "UPDATE ZICCLOUDSYNCINGOBJECT SET ZTOKENCONTENTIDENTIFIER = ? WHERE ZIDENTIFIER = 'Y'",
            (f"applenotes://showNote?identifier={U2}",),
        )
        conn.commit()
        conn.close()
        with NotesDatabase(link_db) as db:
            assert db.get_note_link_targets(13) == [U1, U2, U1]

    def test_link_targets_no_data_returns_empty(self, link_db):
        with NotesDatabase(link_db) as db:
            assert db.get_note_link_targets(11) == []


# ---------------------------------------------------------------- richtext op

FIXTURE = "tests/fixtures/notes_richtext_sample_with_great_sentence.bin"
FIXTURE_TARGET = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"
NEW = "7523FA1D-0D79-411E-ADD8-D22FD90453FA"


def _fixture_richtext():
    from apple_notes.richtext import NotesRichtextParser

    with open(FIXTURE, "rb") as f:
        return NotesRichtextParser().parse(f.read())


def _link_entries(rt):
    return [a.raw_data for a in rt.attachments if a.type_uti == LINK_UTI]


def _first_link(rt):
    return next(a for a in rt.attachments if a.type_uti == LINK_UTI)


def _set_token(attachment, url):
    import json

    attachment.raw_data["34"] = json.dumps({"tokenContentIdentifierKey": url})


def _with_both_forms(target):
    """The fixture note with one link to ``target`` in each stored form."""
    import copy

    rt = _fixture_richtext()
    twin = copy.deepcopy(_first_link(rt))
    twin.raw_data["2"] = "HAND-MADE-ID"
    _set_token(twin, f"applenotes://showNote?identifier={target}")
    rt.attachments.append(twin)
    return rt


class TestRichtextRelink:
    def test_relink_rewrites_alt_and_token_uuid(self):
        import json

        from apple_notes.richtext import relink

        rt = _fixture_richtext()
        old_token = json.loads(_link_entries(rt)[0]["34"])
        result, count = relink(rt, FIXTURE_TARGET, NEW, "New Title")
        assert count == 1
        entry = _link_entries(result)[0]
        assert entry["7"] == "New Title"
        token = json.loads(entry["34"])
        assert token["tokenContentIdentifierKey"] == (
            f"applenotes:note/{NEW.lower()}?ownerIdentifier=_0123456789abcdef0123456789abcdef"
        )
        assert token["creationDateKey"] == old_token["creationDateKey"]

    def test_relink_preserves_everything_else(self):
        from apple_notes.richtext import relink

        rt = _fixture_richtext()
        result, _ = relink(rt, FIXTURE_TARGET, NEW, "New Title")
        assert result.text == rt.text
        assert [r.raw_data for r in result.runs] == [r.raw_data for r in rt.runs]
        others = [a.raw_data for a in rt.attachments if a.type_uti != LINK_UTI]
        assert [
            a.raw_data for a in result.attachments if a.type_uti != LINK_UTI
        ] == others
        before, after = _link_entries(rt)[0], _link_entries(result)[0]
        assert {k: v for k, v in after.items() if k not in ("7", "34")} == {
            k: v for k, v in before.items() if k not in ("7", "34")
        }

    def test_relink_does_not_mutate_input(self):
        from apple_notes.richtext import relink

        rt = _fixture_richtext()
        relink(rt, FIXTURE_TARGET, NEW, "New Title")
        assert _link_entries(rt)[0]["7"] == "★ Sample"

    def test_relink_no_match_returns_zero_and_equal_content(self):
        from apple_notes.richtext import relink

        rt = _fixture_richtext()
        result, count = relink(rt, U1, NEW, "New Title")
        assert count == 0
        assert _link_entries(result) == _link_entries(rt)

    def test_relink_matches_uuid_case_insensitively(self):
        from apple_notes.richtext import relink

        _, count = relink(_fixture_richtext(), FIXTURE_TARGET.lower(), NEW, "X")
        assert count == 1

    def test_relink_round_trips_through_serializer(self):
        from apple_notes.richtext import (
            NotesRichtextParser,
            NotesRichtextSerializer,
            link_target_uuids,
            relink,
        )

        rt = _fixture_richtext()
        assert link_target_uuids(rt) == [FIXTURE_TARGET]
        result, _ = relink(rt, FIXTURE_TARGET, NEW, "New Title")
        reparsed = NotesRichtextParser().parse(
            NotesRichtextSerializer().serialize(result)
        )
        assert link_target_uuids(reparsed) == [NEW]
        assert reparsed.text == rt.text

    def test_relink_rewrites_all_matching_entries(self):
        import copy

        from apple_notes.richtext import link_target_uuids, relink

        rt = _fixture_richtext()
        dup = copy.deepcopy(next(a for a in rt.attachments if a.type_uti == LINK_UTI))
        dup.raw_data["2"] = "DUPLICATE-ID"
        rt.attachments.append(dup)
        result, count = relink(rt, FIXTURE_TARGET, NEW, "New Title")
        assert count == 2
        assert link_target_uuids(result) == [NEW, NEW]

    def test_link_target_uuids_reads_hand_made_token(self):
        from apple_notes.richtext import link_target_uuids

        rt = _fixture_richtext()
        _set_token(_first_link(rt), f"applenotes://showNote?identifier={U1}")
        assert link_target_uuids(rt) == [U1]

    def test_relink_rewrites_hand_made_form_in_place(self):
        import json

        from apple_notes.richtext import link_target_uuids, relink

        rt = _fixture_richtext()
        _set_token(_first_link(rt), f"applenotes://showNote?identifier={U1}")
        result, count = relink(rt, U1, NEW, "New Title")
        assert count == 1
        assert link_target_uuids(result) == [NEW]
        entry = _link_entries(result)[0]
        assert entry["7"] == "New Title"
        assert json.loads(entry["34"])["tokenContentIdentifierKey"] == (
            f"applenotes://showNote?identifier={NEW}"
        )

    def test_relink_rewrites_both_forms_of_one_target(self):
        """Guard and engine read the same forms now, so this can't fail "found 2,
        expected 1"."""
        from apple_notes.richtext import link_target_uuids, relink

        rt = _with_both_forms(FIXTURE_TARGET)
        assert link_target_uuids(rt) == [FIXTURE_TARGET, FIXTURE_TARGET]
        result, count = relink(rt, FIXTURE_TARGET, NEW, "New Title")
        assert count == 2
        assert link_target_uuids(result) == [NEW, NEW]

    def test_relink_skips_entry_with_unparseable_token(self):
        from apple_notes.richtext import link_target_uuids, relink

        rt = _fixture_richtext()
        next(a for a in rt.attachments if a.type_uti == LINK_UTI).raw_data["34"] = (
            "not json"
        )
        result, count = relink(rt, FIXTURE_TARGET, NEW, "X")
        assert count == 0
        assert link_target_uuids(result) == [""]


# ---------------------------------------------------------- resolution/guards

from apple_notes.models import Note  # noqa: E402

HOME = "AAAAAAAA-0000-0000-0000-000000000001"
PLANS = "AAAAAAAA-0000-0000-0000-000000000002"
DUP1 = "AAAAAAAA-0000-0000-0000-000000000003"
DUP2 = "AAAAAAAA-0000-0000-0000-000000000004"
GONE = "AAAAAAAA-0000-0000-0000-00000000DEAD"
SRC = Note(
    name="Oct 28 Journal:",
    uuid="AAAAAAAA-0000-0000-0000-0000000000FF",
    folder="Notes",
    pk=99,
)


class FakeDB:
    def __init__(self, targets, notes):
        self._targets = targets
        self._notes = notes

    def get_note_link_targets(self, pk):
        assert pk == SRC.pk
        return list(self._targets)

    def build_uuid_lookup(self):
        lookup = {}
        for n in self._notes:
            lookup[n.uuid.upper()] = n
            lookup[n.uuid.lower()] = n
        return lookup

    def get_all_notes(self):
        return list(self._notes)


NOTES = [
    Note(name="★ Index", uuid=HOME, folder="Notes", pk=1),
    Note(name="★ Reading List", uuid=PLANS, folder="Notes", pk=2),
    Note(name="Twin", uuid=DUP1, folder="Notes", pk=3),
    Note(name="twin", uuid=DUP2, folder="Other", pk=4),
    SRC,
]


def _plan(targets, old, new, replace_all=False, notes=NOTES):
    from apple_notes.relink import plan_relink

    return plan_relink(FakeDB(targets, notes), SRC, old, new, replace_all)


class TestPlanRelink:
    def test_plan_by_titles_single_link(self):
        plan = _plan([HOME, PLANS.replace("2", "5")], "★ Index", "★ Reading List")
        assert (plan.old_uuid, plan.new_uuid, plan.new_title, plan.count) == (
            HOME,
            PLANS,
            "★ Reading List",
            1,
        )
        assert plan.prior_new_count == 0

    def test_plan_title_match_is_case_insensitive(self):
        assert _plan([HOME], "★ INDEX", "★ reading list").new_uuid == PLANS

    def test_plan_by_uuids(self):
        plan = _plan([HOME], HOME.lower(), PLANS.lower())
        assert (plan.old_uuid, plan.new_uuid) == (HOME, PLANS)

    def test_old_target_uuid_matches_broken_link(self):
        plan = _plan([GONE], GONE, "★ Reading List")
        assert (plan.old_uuid, plan.count) == (GONE, 1)

    def test_old_target_title_ignores_broken_link(self):
        from apple_notes.relink import RelinkError

        with pytest.raises(RelinkError, match="No link to"):
            _plan([GONE], "Gone note", "★ Reading List")

    def test_old_target_title_ambiguous_refused(self):
        from apple_notes.relink import RelinkError

        with pytest.raises(RelinkError) as exc:
            _plan([DUP1, DUP2], "Twin", "★ Reading List")
        assert DUP1 in str(exc.value) and DUP2 in str(exc.value)

    def test_zero_links_refused(self):
        from apple_notes.relink import RelinkError

        with pytest.raises(
            RelinkError, match="No link to '★ Index' in note 'Oct 28 Journal:'"
        ):
            _plan([PLANS], "★ Index", "Twin")

    def test_multiple_links_need_replace_all(self):
        from apple_notes.relink import RelinkError

        with pytest.raises(
            RelinkError, match="Found 2 links to '★ Index'.*replace_all=true"
        ):
            _plan([HOME, HOME], "★ Index", "★ Reading List")

    def test_replace_all_counts_all(self):
        assert (
            _plan(
                [HOME, PLANS, HOME], "★ Index", "★ Reading List", replace_all=True
            ).count
            == 2
        )

    def test_new_target_duplicate_title_refused_lists_uuids(self):
        from apple_notes.relink import RelinkError

        with pytest.raises(RelinkError) as exc:
            _plan([HOME], "★ Index", "TWIN")
        assert DUP1 in str(exc.value) and DUP2 in str(exc.value)

    def test_new_target_uuid_in_trash_refused(self):
        from apple_notes.relink import RelinkError

        # A trashed note is absent from build_uuid_lookup (get_all_notes).
        with pytest.raises(RelinkError, match="not found"):
            _plan([HOME], "★ Index", GONE)

    def test_new_target_not_found(self):
        from apple_notes.relink import RelinkError

        with pytest.raises(RelinkError, match="not found"):
            _plan([HOME], "★ Index", "No Such Note")

    def test_same_target_refused(self):
        from apple_notes.relink import RelinkError

        with pytest.raises(RelinkError, match="already"):
            _plan([HOME], "★ Index", HOME)

    def test_prior_new_count_reported(self):
        plan = _plan([PLANS, HOME], "★ Index", "★ Reading List")
        assert plan.prior_new_count == 1

    def test_is_uuid(self):
        from apple_notes.relink import is_uuid

        assert is_uuid(HOME) and is_uuid(HOME.lower())
        assert not is_uuid("★ Index") and not is_uuid(HOME + "0") and not is_uuid("")


# ------------------------------------------------------------ engine + verify

from contextlib import contextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402


@pytest.fixture
def engine():
    """Mocked UI layer: transform_full_note_richtext feeds the fixture bytes."""
    with open(FIXTURE, "rb") as f:
        data = f.read()
    written: list[bytes] = []
    with (
        patch("apple_notes.ui_operations.UIScriptingExecutor") as executor,
        patch("apple_notes.ui_operations.ClipboardManager") as clipboard,
    ):
        executor.return_value.transform_full_note_richtext.side_effect = (
            lambda name, fn: written.append(fn(data))
        )
        # The post-paste read-back sees whatever was pasted (or the original
        # note if nothing was).
        clipboard.return_value.read_notes_richtext.side_effect = lambda: (
            written[-1] if written else data
        )
        yield written, executor, clipboard


class TestExecuteReplaceNoteLink:
    def test_engine_rewrites_and_pastes(self, engine):
        from apple_notes.richtext import NotesRichtextParser, link_target_uuids
        from apple_notes.ui_operations import execute_replace_note_link

        written, executor, _ = engine
        execute_replace_note_link("Note", FIXTURE_TARGET, NEW, "New Title", 1)
        assert len(written) == 1
        assert link_target_uuids(NotesRichtextParser().parse(written[0])) == [NEW]
        assert (
            executor.return_value.transform_full_note_richtext.call_args[0][0] == "Note"
        )

    def test_engine_rewrites_both_forms_of_one_target(self, engine):
        from apple_notes.richtext import (
            NotesRichtextParser,
            NotesRichtextSerializer,
            link_target_uuids,
        )
        from apple_notes.ui_operations import execute_replace_note_link

        written, executor, clipboard = engine
        data = NotesRichtextSerializer().serialize(_with_both_forms(FIXTURE_TARGET))
        executor.return_value.transform_full_note_richtext.side_effect = (
            lambda name, fn: written.append(fn(data))
        )
        clipboard.return_value.read_notes_richtext.side_effect = lambda: written[-1]
        execute_replace_note_link("Note", FIXTURE_TARGET, NEW, "New Title", 2)
        assert link_target_uuids(NotesRichtextParser().parse(written[0])) == [NEW, NEW]

    def test_engine_count_mismatch_aborts_before_paste(self, engine):
        from apple_notes.exceptions import UIScriptingError
        from apple_notes.ui_operations import execute_replace_note_link

        written, _, _ = engine
        with pytest.raises(UIScriptingError, match="changed since it was checked"):
            execute_replace_note_link("Note", FIXTURE_TARGET, NEW, "New Title", 2)
        assert written == []

    def test_engine_restores_clipboard_on_error(self, engine):
        from apple_notes.exceptions import UIScriptingError
        from apple_notes.ui_operations import execute_replace_note_link

        _, _, clipboard = engine
        with pytest.raises(UIScriptingError):
            execute_replace_note_link("Note", U1, NEW, "New Title", 1)
        clipboard.return_value.restore_state.assert_called_once()

    def test_engine_reads_back_after_paste(self, engine):
        from apple_notes.ui_operations import execute_replace_note_link

        _, executor, clipboard = engine
        execute_replace_note_link("Note", FIXTURE_TARGET, NEW, "New Title", 1)
        executor.return_value.select_all_and_copy.assert_called_once()
        clipboard.return_value.read_notes_richtext.assert_called_once()

    def test_engine_detects_paste_that_did_not_land(self, engine):
        from apple_notes.exceptions import WriteVerificationError
        from apple_notes.ui_operations import execute_replace_note_link

        written, _, clipboard = engine
        with open(FIXTURE, "rb") as f:
            original = f.read()
        # Paste "ran" but the editor still holds the old note.
        clipboard.return_value.read_notes_richtext.side_effect = lambda: original
        with pytest.raises(WriteVerificationError, match="did not re-point"):
            execute_replace_note_link("Note", FIXTURE_TARGET, NEW, "New Title", 1)

    def test_engine_clears_pasteboard_before_copy_back(self, engine):
        """Otherwise a copy that went nowhere reads back our own pasted
        payload as if it were the editor (#177 review)."""
        from unittest.mock import MagicMock

        from apple_notes.ui_operations import execute_replace_note_link

        _, executor, clipboard = engine
        order = MagicMock()
        order.attach_mock(clipboard.return_value.clear, "clear")
        order.attach_mock(executor.return_value.select_all_and_copy, "copy")
        execute_replace_note_link("Note", FIXTURE_TARGET, NEW, "New Title", 1)
        assert [c[0] for c in order.mock_calls] == ["clear", "copy"]

    def test_engine_never_uses_body_html_verification(self, engine):
        from apple_notes.ui_operations import execute_replace_note_link

        _, executor, _ = engine
        execute_replace_note_link("Note", FIXTURE_TARGET, NEW, "New Title", 1)
        executor.return_value._note_body_html.assert_not_called()


class FakeVerifyDB:
    """Each `with db_factory()` pops the next (fingerprint, targets) state."""

    def __init__(self, states):
        self.states = list(states)
        self.current = None

    def factory(self):
        @contextmanager
        def cm():
            self.current = (
                self.states.pop(0) if len(self.states) > 1 else self.states[0]
            )
            yield self

        return cm()

    def note_fingerprint(self, pk):
        return self.current[0]

    def get_note_link_targets(self, pk):
        return list(self.current[1])


def _verify(states, plan, before="fp0"):
    from apple_notes.relink import await_relinked

    db = FakeVerifyDB(states)
    return await_relinked(
        db.factory, 99, plan, before_fingerprint=before, tries=4, sleep=lambda s: None
    )


def _rplan(count=1, prior=0):
    from apple_notes.relink import RelinkPlan

    return RelinkPlan(
        old_uuid=HOME,
        new_uuid=PLANS,
        new_title="★ Reading List",
        count=count,
        prior_new_count=prior,
    )


class TestAwaitRelinked:
    def test_verify_confirms(self):
        assert _verify([("fp0", [HOME]), ("fp1", [PLANS])], _rplan()) is True

    def test_verify_counts_rise_by_expected(self):
        # The note already linked PLANS once: one surviving PLANS link is not enough.
        plan = _rplan(prior=1)
        assert _verify([("fp1", [PLANS, PLANS])], plan) is True
        assert _verify([("fp1", [PLANS])], plan) is False

    def test_verify_unflushed_returns_false(self):
        assert _verify([("fp0", [HOME])], _rplan()) is False

    def test_verify_other_save_first_is_unconfirmed_not_error(self):
        # An unrelated edit saved before the relink changes the fingerprint
        # while the old link is still in NoteStore: unconfirmed, not a failure
        # (the engine's editor read-back is the error gate).
        assert _verify([("fp1", [HOME])], _rplan()) is False

    def test_verify_confirms_after_late_flush(self):
        assert (
            _verify([("fp1", [HOME]), ("fp1", [HOME]), ("fp2", [PLANS])], _rplan())
            is True
        )


# ------------------------------------------------------------- MCP tool + IPC

from unittest.mock import MagicMock  # noqa: E402

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402


class FakeServerDB(FakeDB):
    """NotesDatabase stand-in: the write flips the fingerprint and the targets."""

    instances: list = []

    def __init__(self, state):
        super().__init__(state["targets"], NOTES)
        self.state = state

    def __enter__(self):
        self._targets = self.state["targets"]
        return self

    def __exit__(self, *exc):
        return False

    def count_notes_titled(self, name):
        return self.state.get("title_count", 1)

    def get_note_by_name(self, name):
        return SRC

    def note_fingerprint(self, pk):
        return self.state["fp"]


@pytest.fixture
def server_env():
    state = {"targets": [HOME], "fp": "fp0"}

    def apply_write(*args, **kwargs):
        state["targets"] = [PLANS]
        state["fp"] = "fp1"

    backup = MagicMock()
    backup.create_backup.return_value = "/path/backup.sqlite"
    with (
        patch(
            "apple_notes_mcp.server.NotesDatabase",
            side_effect=lambda *a: FakeServerDB(state),
        ),
        patch("apple_notes_mcp.server.BackupManager", return_value=backup),
        patch("apple_notes_mcp.server.nudge_icloud_sync", return_value=False),
        patch("apple_notes_mcp.server._is_gui_session", return_value=True),
        patch(
            "apple_notes_mcp.server.execute_replace_note_link", side_effect=apply_write
        ) as execute,
        patch("apple_notes_mcp.server.UIHelperClient") as client,
        patch("apple_notes.relink.time.sleep"),
    ):
        client.return_value.replace_note_link.side_effect = apply_write
        yield state, execute, client


class TestReplaceNoteLinkTool:
    def test_replace_note_link_local_success(self, server_env):
        from apple_notes_mcp.server import replace_note_link

        _, execute, client = server_env
        result = replace_note_link(
            name="Oct 28 Journal:", old_target="★ Index", new_target="★ Reading List"
        )
        assert result["success"] is True
        assert result["verified"] is True
        assert result["links_replaced"] == 1
        assert result["old_target_uuid"] == HOME
        assert result["new_target_uuid"] == PLANS
        assert result["new_target"] == "★ Reading List"
        assert result["backup_path"] == "/path/backup.sqlite"
        execute.assert_called_once_with(
            "Oct 28 Journal:", HOME, PLANS, "★ Reading List", 1
        )
        client.return_value.replace_note_link.assert_not_called()

    def test_replace_note_link_remote_routes_daemon(self, server_env):
        from apple_notes_mcp.server import replace_note_link

        _, execute, client = server_env
        with patch("apple_notes_mcp.server._is_gui_session", return_value=False):
            result = replace_note_link(
                name="Oct 28 Journal:", old_target=HOME, new_target=PLANS
            )
        assert result["verified"] is True
        execute.assert_not_called()
        client.return_value.replace_note_link.assert_called_once_with(
            "Oct 28 Journal:", HOME, PLANS, "★ Reading List", 1
        )

    def test_replace_note_link_daemon_timeout_may_still_complete(self, server_env):
        """A timed-out daemon may still be relinking (#143, #125 NTG-012)."""
        from apple_notes.exceptions import UIHelperTimeoutError
        from apple_notes_mcp.server import replace_note_link

        _, _, client = server_env
        client.return_value.replace_note_link.side_effect = UIHelperTimeoutError("t/o")
        with patch("apple_notes_mcp.server._is_gui_session", return_value=False):
            with pytest.raises(ToolError, match="may still complete") as info:
                replace_note_link(
                    name="Oct 28 Journal:", old_target=HOME, new_target=PLANS
                )
        assert "not available" not in str(info.value)

    def test_replace_note_link_refusal_is_tool_error(self, server_env):
        from apple_notes_mcp.server import replace_note_link

        _, execute, _ = server_env
        with pytest.raises(ToolError, match="No link to 'Twin'"):
            replace_note_link(
                name="Oct 28 Journal:", old_target="Twin", new_target="★ Reading List"
            )
        execute.assert_not_called()

    def test_replace_note_link_duplicate_source_title_refused(self, server_env):
        from apple_notes_mcp.server import replace_note_link

        state, execute, _ = server_env
        state["title_count"] = 2
        with pytest.raises(ToolError, match="2 notes are titled"):
            replace_note_link(
                name="Oct 28 Journal:", old_target="★ Index", new_target="★ Reading List"
            )
        execute.assert_not_called()

    def test_replace_note_link_missing_source_refused(self, server_env):
        from apple_notes_mcp.server import replace_note_link

        state, _, _ = server_env
        state["title_count"] = 0
        with pytest.raises(ToolError, match="not found"):
            replace_note_link(
                name="Nope", old_target="★ Index", new_target="★ Reading List"
            )

    def test_replace_note_link_case_mismatched_title_says_exact_title(self, server_env):
        from apple_notes.exceptions import NoteNotFoundError
        from apple_notes_mcp.server import replace_note_link

        with patch.object(
            FakeServerDB, "get_note_by_name", side_effect=NoteNotFoundError("x")
        ):
            with pytest.raises(ToolError, match="exact title"):
                replace_note_link(
                    name="oct 28 journal:",
                    old_target="★ Index",
                    new_target="★ Reading List",
                )

    def test_replace_note_link_empty_args_refused(self, server_env):
        from apple_notes_mcp.server import replace_note_link

        for kwargs in (
            {"name": " ", "old_target": "a", "new_target": "b"},
            {"name": "n", "old_target": "", "new_target": "b"},
            {"name": "n", "old_target": "a", "new_target": "  "},
        ):
            with pytest.raises(ToolError, match="cannot be empty"):
                replace_note_link(**kwargs)

    def test_replace_note_link_unverified_reports_false(self, server_env):
        from apple_notes_mcp.server import replace_note_link

        _, execute, _ = server_env
        execute.side_effect = None  # paste "ran" but NoteStore never changed
        result = replace_note_link(
            name="Oct 28 Journal:", old_target="★ Index", new_target="★ Reading List"
        )
        assert result["success"] is True
        assert result["verified"] is False


class TestReplaceNoteLinkIPC:
    def test_client_sends_replace_note_link(self):
        from apple_notes.ui_helper_client import UIHelperClient

        client = UIHelperClient()
        with patch.object(client, "_send_command") as send:
            client.replace_note_link("N", HOME, PLANS, "★ Reading List", 2)
        send.assert_called_once_with(
            "replace_note_link",
            {
                "name": "N",
                "old_uuid": HOME,
                "new_uuid": PLANS,
                "new_alt": "★ Reading List",
                "expected_count": 2,
            },
        )

    def test_daemon_dispatches_replace_note_link(self):
        import asyncio

        from apple_notes.ui_helper_daemon import UI_COMMANDS, UIHelperDaemon

        assert "replace_note_link" in UI_COMMANDS
        daemon = UIHelperDaemon()
        params = {
            "name": "N",
            "old_uuid": HOME,
            "new_uuid": PLANS,
            "new_alt": "P",
            "expected_count": 1,
        }
        with patch("apple_notes.ui_helper_daemon.execute_replace_note_link") as execute:
            response = asyncio.run(daemon._dispatch("replace_note_link", params, "r1"))
        assert response["success"] is True
        execute.assert_called_once_with(
            name="N", old_uuid=HOME, new_uuid=PLANS, new_alt="P", expected_count=1
        )
