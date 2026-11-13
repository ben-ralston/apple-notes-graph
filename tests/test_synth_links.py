"""[[links]] are written as synthesized link attachments (issue #212).

add_note and update_note_content (append, replace, find_replace) used to make
links through the Cmd+K picker, which dropped about half of them, picked fuzzy
matches and left the Add Link sheet open. Links are now built into the pasted
richtext (U+FFFC + a field-6 link attachment, as edit_note_section does), so no
picker runs, and the copy-back verify checks each link by target uuid.

Headless and CI-visible: pure richtext transforms, a mocked pasteboard and a
mocked executor.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apple_notes.exceptions import UIScriptingError
from apple_notes.richtext import (
    NotesRichtextParser,
    PendingKeyboardAction,
    link_target_uuids,
    markdown_to_richtext_new,
)
from apple_notes.richtext.section_fragment import (
    LinkTarget,
    markdown_to_richtext_linked,
    markdown_to_richtext_new_linked,
)
from apple_notes.write_verify import (
    unconfirmed_append,
    unconfirmed_find_replace,
    unconfirmed_replace,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "notes_richtext_sample_with_great_sentence.bin"
)
UUID = "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"
OTHER = "0A1B2C3D-0000-1111-2222-333344445555"
OWNER = "_0123456789abcdef0123456789abcdef"
LINKS = {
    "target": LinkTarget(uuid=UUID, title="Target", owner=OWNER),
    "other": LinkTarget(uuid=OTHER, title="Other", owner=OWNER),
}
LINK_UTI = "com.apple.notes.inlinetextattachment.link"


pytestmark = pytest.mark.usefixtures("stub_note_resolution")



def _parse(data: bytes):
    return NotesRichtextParser().parse(data)


def _link_alts(rt) -> list[str]:
    return [a.raw_data["7"] for a in rt.attachments if a.type_uti == LINK_UTI]


class TestConvertLinked:
    def test_link_becomes_attachment_with_no_pending_action(self):
        result = markdown_to_richtext_linked(
            "See [[Target]] now", FIXTURE.read_bytes(), LINKS
        )
        rt = _parse(result.data)
        assert rt.text == "See ￼ now"
        assert link_target_uuids(rt) == [UUID]
        assert result.pending_actions == []
        (att,) = [a for a in rt.attachments if a.type_uti == LINK_UTI]
        assert json.loads(att.raw_data["34"]) == {
            "tokenContentIdentifierKey": (
                f"applenotes:note/{UUID.lower()}?ownerIdentifier={OWNER}"
            )
        }

    def test_new_note_conversion_synthesizes_too(self):
        result = markdown_to_richtext_new_linked("- [[Target]]\n## Head", LINKS)
        rt = _parse(result.data)
        assert rt.text == "￼\nHead"
        assert link_target_uuids(rt) == [UUID]
        assert result.pending_actions == []

    def test_alt_text_is_the_live_title(self):
        rt = _parse(markdown_to_richtext_new_linked("[[target]]", LINKS).data)
        assert _link_alts(rt) == ["Target"]

    def test_exact_target_for_each_of_several_links(self):
        rt = _parse(
            markdown_to_richtext_new_linked(
                "[[Other]] and [[Target]] and [[Other]]", LINKS
            ).data
        )
        assert rt.text == "￼ and ￼ and ￼"
        assert sorted(link_target_uuids(rt)) == sorted([OTHER, UUID, OTHER])

    def test_unresolved_link_stays_plain_text(self):
        result = markdown_to_richtext_new_linked("see [[Nowhere]]", LINKS)
        assert _parse(result.data).text == "see [[Nowhere]]"
        assert result.pending_actions == []

    def test_hashtags_and_tables_stay_pending(self):
        result = markdown_to_richtext_new_linked(
            "[[Target]] {#todo}\n| a | b |\n| --- | --- |\n| 1 | 2 |", LINKS
        )
        types = [a.action_type for a in result.pending_actions]
        assert types == ["table", "hashtag"] or types == ["hashtag", "table"]
        rt = _parse(result.data)
        assert link_target_uuids(rt) == [UUID]
        assert "{__NG_LINK_" not in rt.text
        assert "{__NG_TAG_0__}" in rt.text


@pytest.fixture
def manager():
    with patch("apple_notes.clipboard.NSPasteboard") as mock_pb_class:
        mock_pb_class.generalPasteboard.return_value = MagicMock()
        from apple_notes.clipboard import ClipboardManager

        yield ClipboardManager()


class TestFindReplaceSynthesizes:
    def test_find_replace_link_is_synthesized(self, manager):
        data = FIXTURE.read_bytes()
        before = _parse(data)
        word = next(w for w in ("great", "sentence") if w in before.text)
        out, count, pending = manager.find_replace_in_notes_richtext_variable(
            data, word, "[[Target]]", links=LINKS
        )
        assert count == 1
        assert pending == []
        rt = _parse(out)
        assert (
            link_target_uuids(rt).count(UUID)
            == link_target_uuids(before).count(UUID) + 1
        )
        assert "{__NG_LINK_" not in rt.text
        assert sum(r.length for r in rt.runs) == len(rt.text)

    def test_find_replace_without_links_leaves_names_as_text(self, manager):
        data = FIXTURE.read_bytes()
        word = next(w for w in ("great", "sentence") if w in _parse(data).text)
        out, count, pending = manager.find_replace_in_notes_richtext_variable(
            data, word, "[[Target]]"
        )
        assert count == 1 and pending == []
        assert "[[Target]]" in _parse(out).text


class TestNoPicker:
    def _executor(self):
        from apple_notes.ui_scripting import UIScriptingExecutor

        with (
            patch("apple_notes.ui_scripting.AppleScriptExecutor"),
            patch("apple_notes.ui_scripting.ClipboardManager"),
        ):
            ex = UIScriptingExecutor()
        ex._find_and_select_text = MagicMock()
        ex._send_keystroke = MagicMock()
        ex._send_key_code = MagicMock()
        return ex

    def test_wiki_link_action_refused_before_any_keystroke(self):
        ex = self._executor()
        action = PendingKeyboardAction(
            placeholder="{__NG_LINK_0__}",
            action_type="wiki_link",
            text_to_type="Target",
            original_markdown="[[Target]]",
            target_uuid=UUID,
        )
        with pytest.raises(UIScriptingError, match="synthesized"):
            ex.apply_pending_keyboard_actions([action])
        ex._find_and_select_text.assert_not_called()
        ex._send_keystroke.assert_not_called()
        ex._send_key_code.assert_not_called()

    def test_cmd_k_link_flow_is_gone(self):
        from apple_notes.ui_scripting import UIScriptingExecutor

        assert not hasattr(UIScriptingExecutor, "_replace_with_wiki_link")


def _linked(md: str):
    return _parse(markdown_to_richtext_new_linked(md, LINKS).data)


def _unlinked(md: str):
    """Same text with a bare U+FFFC where the link was: the link didn't land."""
    return _parse(markdown_to_richtext_new(md).data)


class TestVerifyChecksLinks:
    def test_append_link_missing_is_reported(self):
        items = unconfirmed_append(
            _unlinked("T\nold\nsee ￼"), _linked("see [[Target]]")
        )
        assert items == ["1 of 1 note link(s) not confirmed"]

    def test_append_link_present_confirms(self):
        assert (
            unconfirmed_append(
                _linked("T\nold\nsee [[Target]]"), _linked("see [[Target]]")
            )
            == []
        )

    def test_append_wrong_target_is_reported(self):
        items = unconfirmed_append(
            _linked("T\nsee [[Other]]"), _linked("see [[Target]]")
        )
        assert items == ["1 of 1 note link(s) not confirmed"]

    def test_replace_link_missing_is_reported(self):
        items = unconfirmed_replace(
            _unlinked("T\n￼ and ￼"), _linked("[[Target]] and [[Other]]")
        )
        assert items == ["2 of 2 note link(s) not confirmed"]

    def test_replace_link_present_confirms(self):
        assert (
            unconfirmed_replace(_linked("T\n[[Target]]"), _linked("[[Target]]")) == []
        )

    def test_find_replace_link_missing_is_reported(self):
        expected = _linked("T\nsee [[Target]] and [[Other]]")
        after = _linked("T\nsee ￼ and [[Other]]")
        assert unconfirmed_find_replace(after, expected, "zzz") == [
            "1 of 2 note link(s) not confirmed"
        ]

    def test_find_replace_link_present_confirms(self):
        expected = _linked("T\nsee [[Target]]")
        assert (
            unconfirmed_find_replace(_linked("T\nsee [[Target]]"), expected, "zzz")
            == []
        )

    def test_no_links_no_link_item(self):
        assert unconfirmed_replace(_unlinked("T\nplain"), _unlinked("plain")) == []


# --- Plumbing: every write path carries LinkTargets, not a name -> uuid map ---

TARGET = LINKS["target"]


@pytest.fixture
def server_env():
    """add_note / update_note_content with backup, DB and executors mocked."""
    from types import SimpleNamespace

    db = MagicMock()
    db.link_owner_identifier.return_value = OWNER
    db.get_note_by_uuid.side_effect = lambda u: SimpleNamespace(
        name={UUID: "Target", OTHER: "Other"}[u]
    )
    db_cls = MagicMock()
    db_cls.return_value.__enter__.return_value = db
    with (
        patch("apple_notes_mcp.server.BackupManager") as backup_cls,
        patch("apple_notes_mcp.server.NotesDatabase", db_cls),
        patch(
            "apple_notes_mcp.server._build_name_to_uuid",
            return_value={"target": UUID, "other": OTHER},
        ),
        patch("apple_notes_mcp.server.nudge_icloud_sync", return_value=False),
        patch("apple_notes_mcp.server._is_gui_session", return_value=True) as gui,
        patch("apple_notes_mcp.server.execute_create_note") as create,
        patch("apple_notes_mcp.server.execute_replace_note") as replace,
        patch("apple_notes_mcp.server.execute_find_replace_multiline") as fr,
        patch("apple_notes_mcp.server.UIHelperClient") as client_cls,
        patch("apple_notes.write_service.append_markdown_to_note") as append,
    ):
        backup_cls.return_value.create_backup.return_value = "/b.sqlite"
        yield SimpleNamespace(
            db=db,
            gui=gui,
            create=create,
            replace=replace,
            fr=fr,
            client=client_cls.return_value,
            append=append,
        )


class TestServerPassesLinkTargets:
    def test_add_note_pastes_a_synthesized_link(self, server_env):
        from apple_notes_mcp.server import add_note

        add_note(name="New", body="see [[target]]")
        name, data, folder, pending = server_env.create.call_args.args
        assert link_target_uuids(_parse(data)) == [UUID]
        assert pending == []

    def test_replace_gets_link_targets(self, server_env):
        from apple_notes_mcp.server import update_note_content

        update_note_content(name="N", body="see [[Target]]", operation="replace")
        server_env.replace.assert_called_once_with(
            "N", "see [[Target]]", {"target": TARGET}
        )

    def test_replace_over_the_daemon_gets_link_targets(self, server_env):
        from apple_notes_mcp.server import update_note_content

        server_env.gui.return_value = False
        update_note_content(name="N", body="[[Other]]", operation="replace")
        server_env.client.replace_note.assert_called_once_with(
            "N", "[[Other]]", {"other": LINKS["other"]}
        )

    def test_find_replace_gets_link_targets(self, server_env):
        from types import SimpleNamespace

        from apple_notes_mcp.server import update_note_content

        content = SimpleNamespace(resolved_text="N\nold", raw_text="N\nold")
        with patch("apple_notes_mcp.server.read_note", return_value=content):
            update_note_content(
                name="N", body="[[Target]]", operation="find_replace", old_text="old"
            )
        assert server_env.fr.call_args.kwargs["links"] == {"target": TARGET}

    def test_append_gets_link_targets(self, server_env):
        from apple_notes_mcp.server import update_note_content

        update_note_content(name="N", body="- [[Target]]", operation="append")
        assert server_env.append.call_args.kwargs["links"] == {"target": TARGET}

    def test_unknown_owner_refuses_before_any_write(self, server_env):
        from mcp.server.fastmcp.exceptions import ToolError

        from apple_notes_mcp.server import update_note_content

        server_env.db.link_owner_identifier.return_value = None
        with pytest.raises(ToolError, match="owner id"):
            update_note_content(name="N", body="[[Target]]", operation="replace")
        server_env.replace.assert_not_called()

    def test_no_links_opens_no_database(self, server_env):
        from apple_notes_mcp.server import update_note_content

        update_note_content(name="N", body="plain", operation="replace")
        server_env.replace.assert_called_once_with("N", "plain", {})
        server_env.db.link_owner_identifier.assert_not_called()


class TestDaemonTimeoutMayStillComplete:
    """A daemon that doesn't answer in time may still be writing (#143, #125):
    the caller must re-read the note, not reinstall the daemon and retry."""

    @pytest.mark.parametrize(
        "tool, kwargs, daemon_call",
        [
            ("add_note", {"name": "N", "body": "b"}, "create_note"),
            (
                "update_note_content",
                {"name": "N", "body": "b", "operation": "replace"},
                "replace_note",
            ),
            (
                "update_note_content",
                {
                    "name": "N",
                    "body": "b",
                    "operation": "find_replace",
                    "old_text": "old",
                },
                "find_replace",
            ),
        ],
    )
    def test_server_routed_writes(self, server_env, tool, kwargs, daemon_call):
        from types import SimpleNamespace

        from mcp.server.fastmcp.exceptions import ToolError

        from apple_notes.exceptions import UIHelperTimeoutError
        from apple_notes_mcp import server

        server_env.gui.return_value = False
        getattr(server_env.client, daemon_call).side_effect = UIHelperTimeoutError(
            "t/o"
        )
        content = SimpleNamespace(resolved_text="N\nold", raw_text="N\nold")
        with (
            patch("apple_notes_mcp.server.read_note", return_value=content),
            pytest.raises(ToolError, match="may still complete") as info,
        ):
            getattr(server, tool)(**kwargs)
        assert "Re-read the note before retrying" in str(info.value)
        assert "not available" not in str(info.value)

    def test_append(self, server_env):
        from mcp.server.fastmcp.exceptions import ToolError

        from apple_notes.exceptions import UIHelperTimeoutError
        from apple_notes_mcp.server import update_note_content

        server_env.append.side_effect = UIHelperTimeoutError("t/o")
        with pytest.raises(ToolError, match="may still complete") as info:
            update_note_content(name="N", body="b", operation="append")
        assert "not available" not in str(info.value)


class TestWriteServiceAppend:
    def _run(self, links):
        from apple_notes import write_service

        db = MagicMock()
        db.link_owner_identifier.return_value = OWNER
        db.get_note_by_uuid.return_value = MagicMock()
        db.get_note_by_uuid.return_value.name = "Target"
        db_cls = MagicMock()
        db_cls.return_value.__enter__.return_value = db
        with (
            patch("apple_notes.write_service.is_gui_session", return_value=True),
            patch("apple_notes.write_service.execute_append_to_note") as execute,
            patch(
                "apple_notes.write_service.build_name_to_uuid",
                return_value={"target": UUID},
            ),
            patch("apple_notes.write_service.NotesDatabase", db_cls),
        ):
            write_service.append_markdown_to_note(
                "N", "see [[Target]]", links=links, skip_backup=True
            )
        return execute.call_args.args

    def test_append_builds_link_targets_when_not_given(self):
        _name, data, pending = self._run(None)
        assert link_target_uuids(_parse(data)) == [UUID]
        assert pending == []

    def test_injected_link_targets_win(self):
        _name, data, pending = self._run({})
        assert link_target_uuids(_parse(data)) == []
        assert "[[Target]]" in _parse(data).text


class TestDaemonWire:
    def test_client_sends_link_targets_for_replace_and_find_replace(self):
        from apple_notes.ui_helper_client import UIHelperClient

        client = UIHelperClient.__new__(UIHelperClient)
        client._send_command = MagicMock()
        client.replace_note("N", "[[Target]]", {"target": TARGET})
        client.find_replace("N", "old", "[[Target]]", {"target": TARGET})
        wire = {"uuid": UUID, "title": "Target", "owner": OWNER}
        (cmd1, p1), (cmd2, p2) = [c.args for c in client._send_command.call_args_list]
        assert (cmd1, p1["links"]) == ("replace_note", [wire])
        assert (cmd2, p2["links"]) == ("find_replace", [wire])

    @pytest.mark.parametrize("command", ["replace_note", "find_replace"])
    def test_daemon_rebuilds_link_targets(self, command, tmp_path):
        import asyncio

        from apple_notes.ui_helper_daemon import UIHelperDaemon

        params = {
            "name": "N",
            "body_markdown": "[[Target]]",
            "old_text": "old",
            "replace_text": "[[Target]]",
            "links": [{"uuid": UUID, "title": "Target", "owner": OWNER}],
        }
        target = {
            "replace_note": "apple_notes.ui_helper_daemon.execute_replace_note",
            "find_replace": "apple_notes.ui_helper_daemon.execute_find_replace_multiline",
        }[command]
        with (
            patch("apple_notes.ui_helper_daemon.session_lock"),
            patch(target) as execute,
        ):
            response = asyncio.run(
                UIHelperDaemon(socket_path=tmp_path / "s")._handle_command(
                    {"id": "1", "command": command, "params": params}
                )
            )
        assert response["success"], response
        assert execute.call_args.kwargs["links"] == {"target": TARGET}


# --- Review round 1 (#212) ---


class TestReviewRound1:
    def test_find_replace_link_across_a_run_boundary(self, manager):
        """old_text that crosses formatting runs: the plain replacement
        inherits both runs, so the placeholder straddles them (Codex P2)."""
        from apple_notes.richtext.markdown_converter import markdown_to_richtext

        data = markdown_to_richtext(
            "Title\nHello **World** end", FIXTURE.read_bytes()
        ).data
        out, count, pending = manager.find_replace_in_notes_richtext_variable(
            data, "lo Wor", "[[Target]]", links=LINKS
        )
        rt = _parse(out)
        assert count == 1 and pending == []
        assert rt.text == "Title\nHel￼ld end"
        assert link_target_uuids(rt) == [UUID]
        assert sum(r.length for r in rt.runs) == len(rt.text)

    def test_append_link_check_ignores_links_already_in_the_note(self):
        """The note already links to Target; the appended [[Target]] left a bare
        U+FFFC. The existing link must not confirm it (Codex P2)."""
        after = _parse(
            markdown_to_richtext_new_linked("T\n[[Target]] before\nsee ￼", LINKS).data
        )
        items = unconfirmed_append(after, _linked("see [[Target]]"))
        assert items == ["1 of 1 note link(s) not confirmed"]

    def test_append_refuses_a_link_action_before_pasting(self):
        """Version skew: a payload with a wiki_link action is refused before
        any paste, so nothing lands in the note."""
        from apple_notes import ui_operations

        action = PendingKeyboardAction(
            placeholder="{__NG_LINK_0__}",
            action_type="wiki_link",
            text_to_type="Target",
            original_markdown="[[Target]]",
        )
        with (
            patch("apple_notes.ui_operations.UIScriptingExecutor") as ex_cls,
            patch("apple_notes.ui_operations.ClipboardManager"),
            patch("apple_notes.ui_operations.edit_lock"),
            patch("apple_notes.ui_operations.session_lock"),
        ):
            with pytest.raises(UIScriptingError, match="synthesized"):
                ui_operations.execute_append_to_note("N", b"x", [action])
            with pytest.raises(UIScriptingError, match="synthesized"):
                ui_operations.execute_create_note("N", b"x", "Notes", [action])
        ex_cls.return_value.append_richtext_to_note.assert_not_called()
        ex_cls.return_value.create_note_with_richtext.assert_not_called()

    def test_client_also_sends_name_to_uuid_for_an_older_daemon(self):
        from apple_notes.ui_helper_client import UIHelperClient

        client = UIHelperClient.__new__(UIHelperClient)
        client._send_command = MagicMock()
        client.replace_note("N", "[[Target]]", {"target": TARGET})
        assert client._send_command.call_args.args[1]["name_to_uuid"] == {
            "target": UUID
        }

    def test_daemon_refuses_an_older_client_payload(self, tmp_path):
        """An old client sends name_to_uuid but no links: writing its links as
        plain text and reporting success would lose them silently."""
        import asyncio

        from apple_notes.ui_helper_daemon import UIHelperDaemon

        params = {
            "name": "N",
            "body_markdown": "[[Target]]",
            "name_to_uuid": {"target": UUID},
        }
        with (
            patch("apple_notes.ui_helper_daemon.session_lock"),
            patch("apple_notes.ui_helper_daemon.execute_replace_note") as execute,
        ):
            response = asyncio.run(
                UIHelperDaemon(socket_path=tmp_path / "s")._handle_command(
                    {"id": "1", "command": "replace_note", "params": params}
                )
            )
        assert not response["success"]
        assert "restart" in response["error"]["message"].lower()
        execute.assert_not_called()

    def test_synthesis_failure_is_a_markdown_conversion_error(self):
        from apple_notes.richtext import MarkdownConversionError

        no_owner = {"target": LinkTarget(UUID, "Target", "")}
        with pytest.raises(MarkdownConversionError):
            markdown_to_richtext_new_linked("[[Target]]", no_owner)


class TestReviewRound2:
    TABLE_MD = "see [[Target]]\n| a | b |\n| --- | --- |\n| 1 | 2 |\nb"

    def test_append_with_a_table_finds_its_link(self):
        """Notes adds lines around a native table, so a fixed line-count tail
        can miss an appended link before it (Codex round 2)."""
        after = _linked("T\nold\nsee [[Target]]\n￼\n\nb")
        assert unconfirmed_append(after, _linked(self.TABLE_MD)) == []

    def test_append_with_a_table_still_ignores_earlier_links(self):
        after = _linked("T\n[[Target]] old\nsee ￼\n￼\n\nb")
        assert unconfirmed_append(after, _linked(self.TABLE_MD)) == [
            "1 of 1 note link(s) not confirmed"
        ]


class TestReviewRound3:
    def test_bare_link_before_a_table_is_not_mistaken_for_the_table(self):
        """Segments ["\\ufffc", ""]: the table's own U+FFFC must not be taken
        as the link literal (Codex round 3)."""
        payload = _linked("[[Target]]\n| a | b |\n| --- | --- |\n| 1 | 2 |")
        after = _linked("T\nold\n[[Target]]\n￼")
        assert unconfirmed_append(after, payload) == []
